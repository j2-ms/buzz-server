import argparse
import copy
import contextlib
import json
import os
from pathlib import Path
import pwd
import shutil
import sys

from . import __version__, config, release, dependencies
from .errors import Error
from .identity import generate
from .runtime import configure_goose, prepare
from .storage import Store, agent_id, atomic, private_dir, read_json, read_private
from .supervisor import Systemd


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Error("usage", "Invalid command arguments. Run buzz-server --help or buzz-server COMMAND --help.")


def parser():
    p = Parser(description="Manage headless Buzz agents with systemd user services.")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--json", action="store_true", help="Versioned JSON response, including errors")
    p.add_argument("--state-dir", default=str(Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "buzz-server"))
    p.add_argument("--unit-dir", default=str(Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "systemd/user"))
    commands = p.add_subparsers(dest="operation", required=True, parser_class=Parser)
    commands.add_parser("version", help="Show the embedded version offline")
    sub = commands.add_parser("install", help="Install this verified release artifact in a private prefix")
    sub.add_argument("--prefix", required=True)
    choice = sub.add_mutually_exclusive_group()
    choice.add_argument("--install-dependencies", action="store_true", help="Acquire missing pinned runtime tools privately")
    choice.add_argument("--skip-dependencies", action="store_true", help="Suppress optional runtime tool prompt")
    for op in ("update", "uninstall"):
        sub = commands.add_parser(op)
        sub.add_argument("--prefix", help="Installed prefix; defaults to this installation")
        if op == "update":
            sub.add_argument("--auto", choices=("on", "off"), help="Enable/disable interactive update offers; does not check now")
    doctor = commands.add_parser("doctor", help="Verify the user manager and installed runtime interface without connecting to a relay")
    doctor.add_argument("--config", help="Private agent JSON; check its configured absolute Goose path")
    commands.add_parser("list", help="List managed agents and observed service state")
    for op in ("create", "configure", "show", "status", "start", "restart", "stop", "logs", "delete"):
        sub = commands.add_parser(op)
        sub.add_argument("id", type=agent_id)
        if op in ("create", "configure"):
            sub.add_argument("--config", required=True, help="Private JSON configuration file; configure replaces the complete config")
        if op == "logs":
            sub.add_argument("--lines", type=int, default=30)
    sub = commands.add_parser("inference", help="Manage inference through upstream Goose")
    inference = sub.add_subparsers(dest="inference_operation", required=True, parser_class=Parser)
    setup = inference.add_parser("configure", help="Run Goose setup interactively for a stopped agent")
    setup.add_argument("id", type=agent_id)
    sub = commands.add_parser("account-plan", help="Render the optional pre-existing service-account administration plan; changes nothing")
    sub.add_argument("--account", required=True)
    return p


def is_ready(store, doc, state):
    try:
        r = read_json(store.directory(doc["id"]) / "ready.json")
        return (isinstance(r, dict) and r.get("worker_pid") == int(state["MainPID"]) and
                r.get("revision") == doc["revision"] and type(r.get("child_pid")) is int and r["child_pid"] > 1)
    except (Error, ValueError, KeyError):
        return False


def view(store, supervisor, doc):
    s = supervisor.inspect(doc["id"])
    ready = s["ActiveState"] == "active" and is_ready(store, doc, s)
    return {"id": doc["id"], "identity": doc["identity"], "revision": doc["revision"],
            "desired": doc["desired"], "service_state": s["ActiveState"], "service_result": s.get("Result", "unknown"),
            "runtime_started": ready, "buzz_response_verified": False,
            "service_account": {"uid": os.getuid(), "name": pwd.getpwuid(os.getuid()).pw_name},
            "configuration": {"harness": "goose", "provider": "goose-managed" if config.current(doc["config"]) else "obsolete", "model": None,
                              "profile_configured": True, "prompt_configured": True},
            "unit": supervisor.name(doc["id"])}


def start(store, supervisor, doc):
    prepare(store, doc, snapshot=False)
    supervisor.install(doc["id"])
    state = supervisor.inspect(doc["id"])
    if doc["desired"] == "running" and state["ActiveState"] == "active" and is_ready(store, doc, state):
        return view(store, supervisor, doc)
    if state["ActiveState"] not in {"inactive", "failed"}:
        supervisor.stop(doc["id"])
    doc["desired"] = "running"
    store.save(doc)
    (store.directory(doc["id"]) / "ready.json").unlink(missing_ok=True)
    supervisor.start(doc["id"], lambda state: is_ready(store, doc, state))
    return view(store, supervisor, doc)


def stop(store, supervisor, doc):
    # Persist intention before any service operation. Even if stop fails, future
    # launches refuse to run. Never claim the existing process stopped on failure.
    doc["desired"] = "stopped"
    store.save(doc)
    supervisor.stop(doc["id"])


def execute(args):
    if args.operation == "version":
        return {"version": __version__}
    if os.getuid() == 0:
        raise Error("root_denied", "Run Buzz Server as an unprivileged service user. For a dedicated account, use the documented, explicitly authorized host-admin procedure.")
    if args.operation == "account-plan":
        return account_plan(args.account)
    if args.operation == "install":
        data = release.install(Path(sys.argv[0]).absolute(), args.prefix, args.state_dir, args.unit_dir)
        result = dependencies.install(Store(args.state_dir), opted=args.install_dependencies,
                                      skip=args.skip_dependencies, structured=args.json)
        return {**data, **result}
    if args.operation in ("update", "uninstall"):
        prefix = args.prefix or release.location()
        if prefix is None:
            raise Error("not_installed", "Run this command from a release installation or specify --prefix.")
        if args.operation == "uninstall":
            return release.uninstall(prefix)
        if args.auto:
            return release.set_auto(prefix, args.auto == "on")
        return release.update(prefix)
    if args.operation == "inference" and (args.json or not all(stream.isatty() for stream in (sys.stdin, sys.stdout, sys.stderr))):
        raise Error("interactive_required", "Goose setup requires an interactive terminal on stdin/stdout/stderr and cannot run with --json.")
    store = Store(args.state_dir)
    supervisor = Systemd(store, args.unit_dir)
    with store.lock():
        if args.operation == "logs":
            store.load(args.id)
            if not 1 <= args.lines <= 200:
                raise Error("usage", "Use --lines between 1 and 200.")
            path = store.directory(args.id) / "events.jsonl"
            events = []
            if path.exists():
                try:
                    for line in read_private(path, 1048576).splitlines():
                        event = json.loads(line)
                        if (not isinstance(event, dict) or set(event) != {"time", "event"} or type(event["time"]) is not int or
                                event["event"] not in {"worker_starting", "profile_applied", "runtime_started", "runtime_exited", "worker_failed", "worker_stopped"}):
                            raise ValueError()
                        events.append(event)
                except (ValueError, UnicodeError):
                    raise Error("unsafe_state", "Lifecycle log is corrupt; inspect the private state offline. No raw log content was returned.")
            return {"id": args.id, "events": events[-args.lines:], "source": "sanitized_lifecycle"}
        supervisor.check()
        if args.operation == "doctor":
            from .runtime import tool, executable
            buzz = tool("buzz", store.root)
            acp = tool("buzz-acp", store.root)
            goose = (executable(config.load(args.config)["harness"]["command"]) if args.config
                     else tool("goose", store.root))
            dependencies.bin_dir(store.root)
            # Bounded offline checks, with no provider or relay invocation.
            import tempfile
            with tempfile.TemporaryDirectory(prefix=".doctor-", dir=store.root) as temp:
                home = private_dir(temp)
                dependencies.probe(buzz, ["users", "set-profile", "--help"], ["--name", "--about", "--avatar"], home)
                dependencies.probe(acp, ["--help"], dependencies.ACP_FLAGS, home)
                dependencies.probe(goose, ["acp", "--help"], [], home)
                dependencies.probe(goose, ["configure", "--help"], [], home)
            from .identity import public
            public("0" * 63 + "1")
            return {"goose_path": str(Path(goose).resolve()), "systemd_user": "verified", "buzz_acp_interface": "verified", "openssl_secp256k1": "verified", "live_buzz_tested": False,
                    "provider": "Checked for each agent during create/start; no provider or relay contacted by doctor."}
        if args.operation == "list":
            return {"agents": [view(store, supervisor, store.load(ident)) for ident in store.ids()]}
        if args.operation == "create":
            directory = store.directory(args.id)
            if directory.exists() or directory.is_symlink():
                raise Error("already_exists", "This ID already has managed or retained data. Choose another ID; delete retains private data and identity.")
            c = config.load(args.config)
            key, identity = generate()
            private_dir(directory)
            doc = {"version": 1, "id": args.id, "identity": identity, "config": c, "desired": "stopped", "revision": 1}
            try:
                atomic(directory / "identity.key", key.encode())
                prepare(store, doc, snapshot=False)
            except Exception:
                shutil.rmtree(directory)
                raise
            store.save(doc)
            supervisor.install(args.id, new=True)
            return view(store, supervisor, doc)
        doc = store.load(args.id)
        if args.operation in ("start", "restart", "inference"):
            config.require_current(doc["config"])
        if args.operation == "inference":
            require_stopped(supervisor, doc)
            return configure_goose(store, doc)
        if args.operation in ("show", "status"):
            return view(store, supervisor, doc)
        if args.operation == "start":
            return start(store, supervisor, doc)
        if args.operation == "configure":
            candidate = copy.deepcopy(doc)
            candidate["config"] = config.load(args.config)
            prepare(store, candidate, snapshot=False)
            # Stop is verified before replacing configuration. A failed stop
            # leaves the original definition intact, marked intentionally stopped.
            running = doc["desired"] == "running" and config.current(doc["config"])
            stop(store, supervisor, doc)
            candidate["revision"] += 1
            candidate["desired"] = "stopped"
            store.save(candidate)
            if running:
                return start(store, supervisor, candidate)
            return view(store, supervisor, candidate)
        if args.operation == "restart":
            prepare(store, doc, snapshot=False)
            stop(store, supervisor, doc)
            return start(store, supervisor, doc)
        if args.operation == "stop":
            stop(store, supervisor, doc)
            return view(store, supervisor, doc)
        if args.operation == "delete":
            stop(store, supervisor, doc)
            supervisor.remove(args.id)
            (store.directory(args.id) / "definition.json").unlink()
            return {"id": args.id, "deleted": True, "worker_stopped": True, "data_retained": True,
                    "linux_account_deleted": False, "buzz_history_deleted": False}
        raise Error("usage", "Unknown operation. Run buzz-server --help.")


def require_stopped(supervisor, doc):
    if doc["desired"] != "stopped" or not supervisor.stopped(doc["id"]):
        raise Error("stop_required", "Explicitly stop this agent and verify no active worker before running Goose setup.")


def account_plan(name):
    import re
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", name):
        raise Error("invalid_account", "Use the name of a pre-existing unprivileged Linux service account.")
    try:
        account = pwd.getpwnam(name)
    except KeyError:
        raise Error("invalid_account", "The service account does not exist. Buzz Server never creates Linux accounts.")
    if account.pw_uid == 0 or account.pw_uid == os.getuid():
        raise Error("invalid_account", "Choose a pre-existing, dedicated unprivileged account distinct from the operator.")
    return {"account": name, "uid": account.pw_uid, "changes_applied": False,
            "procedure": "docs/service-accounts.md", "authorization_required": "Host administrator must explicitly authorize account-scoped CLI execution and user-manager persistence. No blanket sudo or account creation/deletion."}


def render(operation, data):
    if operation == "list":
        if not data["agents"]:
            return "No managed agents."
        return "\n".join(f"{a['id']}  {a['service_state']}  desired={a['desired']}  identity={a['identity']}" for a in data["agents"])
    if operation == "logs":
        return "\n".join(f"{e['time']} {e['event']}" for e in data["events"]) or "No lifecycle events yet."
    if "service_state" in data:
        return (f"{data['id']}: {data['service_state']} (desired {data['desired']}, revision {data['revision']})\n"
                f"Buzz identity: {data['identity']}\nService account: {data['service_account']['name']}\n"
                f"Runtime process verified: {str(data['runtime_started']).lower()}; Buzz response unverified")
    if operation == "inference":
        return f"{data['id']}: Goose setup exited. Buzz reply remains unverified."
    if operation == "delete":
        return f"{data['id']}: stopped and removed from management. Private data, Linux account and Buzz history retained."
    if operation == "install":
        lines = [f"Installed Buzz Server {data['version']} at {data['prefix']}."]
        if data.get("dependencies_installed"):
            lines.append("Installed private tools: " + ", ".join(data["dependencies_installed"]) + ".")
        if data.get("dependency_error"):
            lines.append(data["dependency_error"])
        if data["missing_runtime_tools"]:
            lines.append("Missing runtime tools: " + ", ".join(data["missing_runtime_tools"]) + ".")
            lines.append("Install prerequisites yourself: https://github.com/block/buzz/releases/tag/desktop-v0.5.30 and https://github.com/aaif-goose/goose/releases/tag/v1.60.2; see docs/releases.md.")
        goose = data["runtime_tool_paths"]["goose"]
        if goose:
            lines.append(f"Goose found at {goose}; use this exact path as harness.command in agent JSON passed with --config (also doctor --config).")
        else:
            lines.append("Install Goose, then set harness.command in agent JSON to its exact absolute executable path.")
        lines.append("buzz-server update does not update privately installed runtime tools.")
        lines.append("Presence is not a compatibility check. Next: run this prefix's bin/buzz-server doctor, then follow docs/quickstart.md. No services started; install status does not prove relay/provider readiness.")
        return "\n".join(lines)
    return json.dumps(data, indent=2)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    structured = "--json" in argv
    operation = "unknown"
    try:
        args = parser().parse_args(argv)
        operation = args.operation
        prefix = release.location()
        ordinary = operation not in ("install", "update", "uninstall", "version", "account-plan")
        release.before_command(prefix, operation, structured,
                               sys.stdin.isatty() and sys.stdout.isatty() and sys.stderr.isatty(),
                               args.state_dir, args.unit_dir, argv)
        guard = release.locked(prefix, shared=True) if prefix and ordinary else contextlib.nullcontext()
        with guard:
            if prefix and ordinary:
                m = release.manifest(prefix)
                release.binding(m, args.state_dir, args.unit_dir)
            data = execute(args)
        if structured:
            print(json.dumps({"schema_version": 1, "operation": operation, "ok": True, "data": data}, sort_keys=True))
        else:
            print(render(operation, data))
        return 0
    except KeyboardInterrupt:
        code, message = "interrupted", "Operation interrupted. No successful setup or Buzz reply is claimed."
    except Error as e:
        code, message = e.code, str(e)
    except (OSError, UnicodeError, ValueError, TypeError, KeyError):
        code, message = "local_failure", "Local state or prerequisite operation failed. Check private file ownership, permissions, free space and the documented configuration; no raw diagnostic content is returned."
    if structured:
        print(json.dumps({"schema_version": 1, "operation": operation, "ok": False, "error": {"code": code, "message": message}}, sort_keys=True), file=sys.stderr)
    else:
        print(f"Error [{code}]: {message}", file=sys.stderr)
    return 1
