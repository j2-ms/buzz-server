import json
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import time

from .config import current, require_current, validate_relay_transport
from .errors import Error
from . import dependencies
from .identity import public
from .storage import atomic, private_dir, read_private, write_json


STARTUP_EXIT = {"profile_unconfirmed": 75, "local_runtime": 76, "local_setup": 77,
                "local_setup_refused": 78, "profile_refused": 79}


def executable(path):
    p = Path(path)
    if not p.is_file() or not os.access(p, os.X_OK):
        raise Error("prerequisite_missing", "A required executable is missing or not executable. Install buzz-acp, buzz, OpenSSL and Goose; use an absolute Goose command in configuration.")
    return str(p)


def tool(name, root=None):
    path = dependencies.resolve(name, root)
    if not path:
        raise Error("prerequisite_missing", "Install compatible buzz, buzz-acp and Goose in fixed system paths or retry install --install-dependencies; configure an absolute Goose path.")
    return executable(path)


def help_check(path, flags):
    try:
        result = subprocess.run([path, "--help"], capture_output=True, timeout=5, env={"PATH": "/usr/bin:/bin"})
        if result.returncode or any(flag.encode() not in result.stdout for flag in flags):
            raise Error("unsupported_runtime", "Installed buzz-acp does not expose the supported headless interface. Check the documented required flags and install a compatible release.")
    except (OSError, subprocess.TimeoutExpired):
        raise Error("unsupported_runtime", "Cannot verify runtime help. Check the installed buzz-acp and buzz executables locally.")


def goose_context(store, doc, *, tools=None):
    """Deterministic per-agent environment shared by setup and ACP; no ambient keys."""
    directory = store.directory(doc["id"])
    work = private_dir(directory / "work")
    home = private_dir(directory / "home")
    config_home = home / ".config"
    data_home = home / ".local/share"
    private_dir(config_home)
    private_dir(home / ".local")
    private_dir(data_home)
    path = str(tools) + ":/usr/bin:/bin" if tools is not None else dependencies.child_path(store.root)
    env = {"PATH": path, "HOME": str(home),
           "XDG_CONFIG_HOME": str(config_home), "XDG_DATA_HOME": str(data_home),
           "LANG": "C.UTF-8"}
    env["GOOSE_DISABLE_KEYRING"] = "1"
    return env, work


def configure_goose(store, doc, *, tools=None):
    require_current(doc["config"])
    env, work = goose_context(store, doc, tools=tools)
    for key in ("TERM", "COLORTERM"):
        if key in os.environ:
            env[key] = os.environ[key]
    goose = tools / "goose" if tools is not None else doc["config"]["harness"]["command"]
    command = [executable(goose), "configure"]
    child = None
    previous = signal.getsignal(signal.SIGTERM)

    def interrupted(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    try:
        # Inherit the terminal, never capture wizard output or put it in logs.
        child = subprocess.Popen(command, env=env, cwd=work, umask=0o077)
        code = child.wait()
    except KeyboardInterrupt:
        raise Error("inference_cancelled", "Goose setup was interrupted; existing setup may have been changed by Goose. Buzz reply remains unverified.") from None
    except OSError:
        raise Error("inference_failed", "Cannot run Goose setup. Buzz reply remains unverified.") from None
    finally:
        # Keep the caller's Store and prefix locks until the wizard is reaped.
        # Repeated interrupts cannot release mutation exclusion during cleanup.
        old_int = signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        try:
            if child is not None and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
        finally:
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, previous)
    if code:
        raise Error("inference_failed", "Goose setup exited without success; existing setup may have been changed by Goose. Buzz reply remains unverified.")
    return {"id": doc["id"], "setup_exited": True, "buzz_response_verified": False}


def credential_prompt():
    credentials = os.environ.get("CREDENTIALS_DIRECTORY")
    try:
        if not credentials or not Path(credentials).is_absolute():
            raise ValueError()
        path = Path(credentials) / "prompt"
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            # systemd uses a root-owned file with a UID ACL or a UID-owned
            # file on a read-only mount. The ACL mask may set the group read bit.
            if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid())
                    or stat.S_IMODE(info.st_mode) not in (0o400, 0o440)):
                raise ValueError()
            prompt = stream.read(65537)
        if len(prompt) > 65536:
            raise ValueError()
        return path, prompt
    except (OSError, ValueError):
        raise Error("invalid_config", "The managed prompt credential is unavailable or invalid.") from None


def prepare(store, doc, snapshot=True, *, tools=None, managed_prompt=False):
    directory = store.directory(doc["id"])
    c = doc["config"]
    require_current(c)
    # Enforce on every launch too. Draft definitions with an insecure relay
    # remain readable by stop/delete so they can still be taken out of service.
    validate_relay_transport(c["relay_url"])
    key = read_private(directory / "identity.key", 64).decode("ascii")
    if public(key) != doc["identity"]:
        raise Error("identity_mismatch", "Identity file does not match the managed public identity; restore the correct key before starting.")
    prompt_path = directory / "prompt.txt"
    if managed_prompt:
        # systemd passes a per-unit, read-only credential mount to the worker.
        # The agent's configuration and writable snapshot never select this path.
        prompt_path, prompt = credential_prompt()
    else:
        prompt = read_private(c["prompt_file"], 65536)
    try:
        prompt.decode("utf-8")
    except UnicodeError:
        raise Error("invalid_config", "Prompt file must be UTF-8 text, at most 64 KiB, owned by the service user with mode 0600.")
    if tools is None:
        goose = executable(c["harness"]["command"])
        acp, buzz = tool("buzz-acp", store.root), tool("buzz", store.root)
    else:
        goose, acp, buzz = (executable(tools / name) for name in ("goose", "buzz-acp", "buzz"))
    help_check(acp, ["--system-prompt-file", "--agent-command", "--agent-args", "--model", "--permission-mode", "--respond-to", "--session-title", "--config"])
    env, work = goose_context(store, doc, tools=tools)
    if snapshot and not managed_prompt:
        atomic(prompt_path, prompt)
    # Deployed workers use the packaged empty config from the read-only payload.
    acp_config = tools / "buzz-acp.toml" if tools is not None else directory / "buzz-acp.toml"
    if snapshot and tools is None:
        atomic(acp_config, b"")
    env["BUZZ_PRIVATE_KEY"] = key
    command = [acp, "--relay-url", c["relay_url"], "--agent-owner", c["owner"],
               "--agent-command", goose, "--agent-args", "acp",
               "--system-prompt-file", str(prompt_path), "--session-title", c["profile"]["name"],
               "--config", str(acp_config), "--respond-to", "owner-only", "--permission-mode", "dont-ask"]
    # buzz's profile operation is headless; secret goes only through the environment.
    relay = c["relay_url"].replace("wss://", "https://", 1).replace("ws://", "http://", 1)
    profile = [buzz, "--relay", relay, "users", "set-profile", "--name", c["profile"]["name"], "--about", c["profile"]["about"], "--avatar", c["profile"]["avatar"]]
    return command, profile, env, work


def event(directory, code):
    # Fixed vocabulary only. No child stdout/stderr, prompts, config or exceptions.
    allowed = {"worker_starting", "profile_applied", "runtime_started", "runtime_exited", "worker_failed", "worker_stopped"}
    if code not in allowed:
        raise ValueError("unknown event")
    path = directory / "events.jsonl"
    import fcntl
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        if os.fstat(f.fileno()).st_size > 524288:
            os.ftruncate(f.fileno(), 0)
        f.write(json.dumps({"time": int(time.time()), "event": code}) + "\n")


def profile_accepted(output):
    try:
        result = json.loads(output)
    except (ValueError, UnicodeError):
        return False
    return isinstance(result, dict) and result.get("accepted") is True


def profile_refused(output):
    try:
        result = json.loads(output)
    except (ValueError, UnicodeError):
        return False
    # Only a structured Nostr write refusal with an explicit policy prefix is
    # terminal. HTTP errors, generic rejection and missing responses may recur
    # for unrelated reasons and receive the ordinary retry budget.
    return (isinstance(result, dict) and result.get("accepted") is False
            and isinstance(result.get("message"), str)
            and result["message"].startswith(("blocked: ", "restricted: ", "auth-required: ")))


def notify_startup(status, *, ready=False):
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as connection:
            connection.settimeout(0.5)
            connection.sendto(("STATUS=buzz-server:" + status + ("\nREADY=1" if ready else "")).encode("ascii"), address)
    except OSError:
        # With Type=notify, systemd's startup timer handles an undelivered READY.
        pass


def worker(store, ident, *, dedicated=False, tools=None, managed_prompt=False):
    directory = store.directory(ident)
    child = None
    stopping = False

    def terminate_child():
        if child is not None and child.poll() is None:
            try:
                child.terminate()
            except ProcessLookupError:
                pass

    def stop(*_):
        nonlocal stopping
        stopping = True
        terminate_child()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    phase = "local_setup"
    refusal = None
    try:
        if dedicated:
            notify_startup(phase)
        doc = store.load(ident)
        if doc["desired"] != "running":
            return 0
        if not current(doc["config"]):
            # An obsolete definition needs an operator, not a systemd restart loop.
            return STARTUP_EXIT["local_setup_refused"] if dedicated else 0
        event(directory, "worker_starting")
        try:
            command, profile, env, work = prepare(store, doc, tools=tools, managed_prompt=managed_prompt)
        except Error as error:
            if error.code in {"identity_mismatch", "invalid_config", "obsolete_provider", "prerequisite_missing"}:
                refusal = "local_setup_refused"
            raise
        if stopping:
            return 0
        phase = "profile_unconfirmed"
        if dedicated:
            notify_startup(phase)
        profile_env = {key: env[key] for key in ("PATH", "HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "LANG", "BUZZ_PRIVATE_KEY")}
        child = subprocess.Popen(profile, env=profile_env, cwd=work, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if stopping:
            terminate_child()
        try:
            output, _ = child.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate()
            raise Error("profile_failed", "Profile update timed out.")
        if stopping:
            return 0
        if child.returncode or not profile_accepted(output):
            if dedicated and child.returncode == 0 and profile_refused(output):
                refusal = "profile_refused"
            raise Error("profile_failed", "Profile update failed.")
        event(directory, "profile_applied")
        phase = "local_runtime"
        if dedicated:
            notify_startup(phase)
        if stopping:
            return 0
        child = subprocess.Popen(command, env=env, cwd=work, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if stopping:
            terminate_child()
            child.wait(timeout=5)
            return 0
        time.sleep(0.5)
        if stopping:
            child.wait(timeout=5)
            return 0
        if child.poll() is not None:
            raise Error("runtime_failed", "Runtime exited during startup.")
        write_json(directory / "ready.json", {"worker_pid": os.getpid(), "child_pid": child.pid, "revision": doc["revision"]})
        event(directory, "runtime_started")
        if dedicated:
            notify_startup("ready", ready=True)
        code = child.wait()
        event(directory, "worker_stopped" if stopping else "runtime_exited")
        return 0 if stopping else (STARTUP_EXIT[phase] if dedicated else code or 1)
    except Exception:
        if dedicated and not stopping:
            notify_startup(phase)
        event(directory, "worker_failed")
        if child is not None and child.poll() is None:
            terminate_child()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        return (0 if stopping else STARTUP_EXIT[refusal or phase]) if dedicated else 1
    finally:
        (directory / "ready.json").unlink(missing_ok=True)
