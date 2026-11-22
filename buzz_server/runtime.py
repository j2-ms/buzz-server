import json
import os
from pathlib import Path
import signal
import subprocess
import time

from .config import current, require_current, validate_relay_transport
from .errors import Error
from . import dependencies
from .identity import public
from .storage import atomic, private_dir, read_private, write_json


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


def goose_context(store, doc):
    """Deterministic per-agent environment shared by setup and ACP; no ambient keys."""
    directory = store.directory(doc["id"])
    work = private_dir(directory / "work")
    home = private_dir(directory / "home")
    config_home = home / ".config"
    data_home = home / ".local/share"
    private_dir(config_home)
    private_dir(home / ".local")
    private_dir(data_home)
    env = {"PATH": dependencies.child_path(store.root), "HOME": str(home),
           "XDG_CONFIG_HOME": str(config_home), "XDG_DATA_HOME": str(data_home),
           "LANG": "C.UTF-8"}
    env["GOOSE_DISABLE_KEYRING"] = "1"
    return env, work


def configure_goose(store, doc):
    require_current(doc["config"])
    env, work = goose_context(store, doc)
    for key in ("TERM", "COLORTERM"):
        if key in os.environ:
            env[key] = os.environ[key]
    command = [executable(doc["config"]["harness"]["command"]), "configure"]
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


def prepare(store, doc, snapshot=True):
    directory = store.directory(doc["id"])
    c = doc["config"]
    require_current(c)
    # Enforce on every launch too. Draft definitions with an insecure relay
    # remain readable by stop/delete so they can still be taken out of service.
    validate_relay_transport(c["relay_url"])
    key = read_private(directory / "identity.key", 64).decode("ascii")
    if public(key) != doc["identity"]:
        raise Error("identity_mismatch", "Identity file does not match the managed public identity; restore the correct key before starting.")
    prompt = read_private(c["prompt_file"], 65536)
    try:
        prompt.decode("utf-8")
    except UnicodeError:
        raise Error("invalid_config", "Prompt file must be UTF-8 text, at most 64 KiB, owned by the service user with mode 0600.")
    goose = executable(c["harness"]["command"])
    acp, buzz = tool("buzz-acp", store.root), tool("buzz", store.root)
    help_check(acp, ["--system-prompt-file", "--agent-command", "--agent-args", "--model", "--permission-mode", "--respond-to", "--session-title", "--config"])
    env, work = goose_context(store, doc)
    if snapshot:
        atomic(directory / "prompt.txt", prompt)
    # Empty explicit config prevents ambient buzz-acp.toml from changing execution.
    if snapshot:
        atomic(directory / "buzz-acp.toml", b"")
    env["BUZZ_PRIVATE_KEY"] = key
    command = [acp, "--relay-url", c["relay_url"], "--agent-owner", c["owner"],
               "--agent-command", goose, "--agent-args", "acp",
               "--system-prompt-file", str(directory / "prompt.txt"), "--session-title", c["profile"]["name"],
               "--config", str(directory / "buzz-acp.toml"), "--respond-to", "owner-only", "--permission-mode", "dont-ask"]
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


def worker(store, ident):
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
    try:
        doc = store.load(ident)
        if doc["desired"] != "running" or not current(doc["config"]):
            # An obsolete definition needs an operator, not a systemd restart loop.
            return 0
        event(directory, "worker_starting")
        command, profile, env, work = prepare(store, doc)
        if stopping:
            return 0
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
            raise Error("profile_failed", "Profile update failed.")
        event(directory, "profile_applied")
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
        code = child.wait()
        event(directory, "worker_stopped" if stopping else "runtime_exited")
        return 0 if stopping else (code or 1)
    except Exception:
        event(directory, "worker_failed")
        if child is not None and child.poll() is None:
            terminate_child()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        return 1
    finally:
        (directory / "ready.json").unlink(missing_ok=True)
