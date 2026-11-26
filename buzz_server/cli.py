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
from .identity import generate, operator_public_key
from .runtime import configure_goose, prepare
from .storage import Store, agent_id, atomic, private_dir, read_json, read_private
from .supervisor import PreexistingUnit, Systemd


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Error("usage", "Invalid command arguments. Run buzz-server --help or buzz-server COMMAND --help.")


UPDATE_OFFERS = (
    "When enabled, commands like 'buzz-server list' can offer a verified update before running. "
    "Offers require terminal stdin, stdout and stderr and your consent. The first eligible command "
    "checks when due, at most once every 24 hours even if the check fails or you decline. Help and "
    "version stay offline for quick use. JSON and redirected runs skip offers to avoid interrupting "
    "scripts. Run 'buzz-server update' to check now, regardless of the offer preference or daily "
    "limit. 'buzz-server update --auto on' enables offers without checking now."
)
DOCTOR_SCOPE = "Worker startup, provider access, credentials, relay access and Buzz replies were not checked."


def parser():
    p = Parser(prog="buzz-server", description="Manage headless Buzz agents with native systemd services.",
               epilog=UPDATE_OFFERS)
    p.add_argument("-v", "--version", action="version", version=__version__,
                   help="Show the installed version offline, as when invoked without arguments")
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
    sub = commands.add_parser("update", help="Check now and apply a verified stable CLI upgrade",
                              description="Update the management CLI without restarting workers.", epilog=UPDATE_OFFERS)
    sub.add_argument("--prefix", help="Installed prefix; defaults to this installation")
    sub.add_argument("--auto", choices=("on", "off"), help="Change the interactive offer preference without checking now")
    sub = commands.add_parser("uninstall", help="Stop and remove same-account units and uninstall the CLI, retaining private data")
    sub.add_argument("--prefix", help="Installed prefix; defaults to this installation")
    doctor = commands.add_parser("doctor", help="Check local runtime prerequisites or preflight a dedicated account")
    checks = doctor.add_mutually_exclusive_group()
    checks.add_argument("--config", help="Private agent JSON; check its configured absolute Goose path")
    checks.add_argument("--account", help="Check an existing dedicated account and system manager instead of the operator user manager")
    commands.add_parser("list", help="List managed agents and observed service state")
    agent_commands = {
        "create": "Create an agent with a new identity and leave it stopped",
        "configure": "Replace an agent's configuration, restarting it if running",
        "show": "Show an agent's identity, configuration summary and service state",
        "status": "Show an agent's requested and observed service state",
        "start": "Start an agent worker",
        "restart": "Stop and start an agent worker",
        "stop": "Stop an agent worker",
        "logs": "Show sanitized lifecycle events",
        "delete": "Stop and remove an agent from management, retaining private data",
    }
    for op, help_text in agent_commands.items():
        sub = commands.add_parser(op, help=help_text)
        sub.add_argument("id", type=agent_id)
        if op in ("create", "configure"):
            sub.add_argument("--config", required=(op == "configure"), help="Private JSON configuration file; configure replaces the complete config")
        if op == "create":
            sub.add_argument("--account", help="Existing dedicated unprivileged account; authorize fixed system tools through explicit sudo")
        if op == "logs":
            sub.add_argument("--lines", type=int, default=30)
    sub = commands.add_parser("inference", help="Manage inference through upstream Goose")
    inference = sub.add_subparsers(dest="inference_operation", required=True, parser_class=Parser)
    setup = inference.add_parser("configure", help="Run Goose setup interactively for a stopped agent")
    setup.add_argument("id", type=agent_id)
    worker = commands.add_parser("worker", help="Manage the worker payload of a dedicated agent")
    worker_ops = worker.add_subparsers(dest="deployment_operation", required=True, parser_class=Parser)
    worker_ops.add_parser("upgrade", help="Install this CLI's pinned worker and tools for one agent").add_argument("id", type=agent_id)
    worker_ops.add_parser("rollback", help="Restore the previous worker payload").add_argument("id", type=agent_id)
    prompt = commands.add_parser("prompt", help="Manage approved prompt revisions for a dedicated agent")
    prompt_ops = prompt.add_subparsers(dest="deployment_operation", required=True, parser_class=Parser)
    activate = prompt_ops.add_parser("activate", help="Approve and activate a snapshot of an operator-owned prompt file")
    activate.add_argument("id", type=agent_id)
    activate.add_argument("--file", required=True, help="Path to a private, operator-owned UTF-8 prompt file")
    prompt_ops.add_parser("rollback", help="Restore the previous approved prompt revision").add_argument("id", type=agent_id)
    prompt_ops.add_parser("clear", help="Use the prompt from the agent configuration again").add_argument("id", type=agent_id)
    tools = commands.add_parser("dependencies", help="Manage the operator's private dependency set")
    tools_ops = tools.add_subparsers(dest="dependencies_operation", required=True, parser_class=Parser)
    tools_ops.add_parser("refresh", help="Verify and replace the operator's private tools with this CLI's pins")
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
        data = release.install(Path(sys.argv[0]).absolute(), args.prefix, args.state_dir, args.unit_dir,
                               offer_repair=not args.json and dependencies.interactive())
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
        data = release.update(prefix, show_progress=not args.json and dependencies.interactive())
        if args.json:
            data.pop("previous_version", None)
        return data
    if args.operation == "inference" and (args.json or not all(stream.isatty() for stream in (sys.stdin, sys.stdout, sys.stderr))):
        raise Error("interactive_required", "Goose setup requires an interactive terminal on stdin/stdout/stderr and cannot run with --json.")
    if args.operation == "create" and not args.config and (args.json or not all(stream.isatty() for stream in (sys.stdin, sys.stdout, sys.stderr))):
        raise Error("interactive_required", "Guided creation requires a terminal on stdin/stdout/stderr; use --config for JSON or non-interactive creation.")
    doctor_progress = args.operation == "doctor" and not args.json and dependencies.interactive()
    if doctor_progress:
        check = "dedicated account layout" if args.account else "systemd user manager"
        print(f"Checking the {check}...", file=sys.stderr, flush=True)
    store = Store(args.state_dir)
    if args.operation == "dependencies":
        return dependencies.refresh(store, structured=args.json)
    from . import account_lifecycle
    from .accounts import Bindings, account
    bindings = Bindings(store)
    dedicated_guided = None
    selected = None
    if args.operation == "create" and args.account:
        selected = bindings.expected(args.id, account(args.account))
        if not args.config:
            dedicated_guided = guided_create(args.id, Path(selected.data) / "state/agents" / args.id,
                                              Path(selected.data) / "state", selected)
    if args.operation == "doctor" and args.account:
        entry = account(args.account, diagnose=True)
        if doctor_progress:
            print("Checking the systemd system manager...", file=sys.stderr, flush=True)
        from .account_process import run
        result = run(["/usr/bin/systemctl", "--system", "show", "--property=Version", "--value"],
                     env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}, cwd="/", umask=0o077)
        if result.returncode or not result.stdout.strip():
            raise Error("supervisor_unavailable", "Cannot verify the system manager; inspect systemctl --system locally.")
        # Recheck after the observation; no account readiness/live reply claim.
        if account(args.account, diagnose=True) != entry:
            raise Error("account_binding", "The selected account changed during the check.")
        return {"service_account": {"name": entry.pw_name, "uid": entry.pw_uid}, "systemd_system": "verified",
                "runtime_started": False, "buzz_response_verified": False, "credentials_checked": False}
    account_guard = (contextlib.nullcontext() if args.operation == "create" and not args.account
                     and not bindings.path(args.id).exists() and not bindings.path(args.id).is_symlink()
                     else store.lock())
    with account_guard:
        if args.operation == "create" and args.account:
            if bindings.expected(args.id, account(args.account)) != selected:
                raise Error("account_binding", "The selected account changed while collecting configuration; no action was authorized.")
            if args.config:
                config.load(args.config)
            path = bindings.path(args.id)
            if path.exists() or path.is_symlink():
                bound = bindings.load(args.id)
                if bound.account != args.account:
                    raise Error("account_binding", "This ID is bound to a different account; no action was authorized.")
            else:
                bound = bindings.reserve(args.id, args.account)
            record = account_lifecycle.manifest_path(bindings, args.id)
            if record.exists() or record.is_symlink():
                doc = account_lifecycle.load(bindings, args.id)
                if doc["phase"] != "creating":
                    raise Error("already_exists", "This ID has managed or retained data. Use configure or choose another ID.")
            else:
                doc = {"version": 1, "id": args.id, "phase": "creating", "identity": None,
                       "revision": 1, "desired": "stopped"}
            lifecycle = account_lifecycle.Lifecycle(store, bound, interactive=not args.json)
            lifecycle.save(doc)
            return lifecycle.create(doc, args.config, prepared=dedicated_guided, structured=args.json)
        if args.operation == "list":
            separate = []
            for path in sorted(bindings.root.glob("*.json")):
                # Only the exact binding filename grammar, never journal files.
                if path.name.endswith(account_lifecycle.RECORD_SUFFIXES):
                    continue
                ident = agent_id(path.stem)
                bound = bindings.load(ident)
                doc = account_lifecycle.load(bindings, ident)
                if doc["phase"] == "deleted":
                    continue
                lifecycle = account_lifecycle.Lifecycle(store, bound)
                if doc["phase"] != "ready":
                    raise Error("account_incomplete", "A dedicated-account lifecycle is incomplete. Retry the original create/delete command; private state and the binding were retained.")
                separate.append(lifecycle.view(doc))
            if not store.ids():
                return {"agents": separate}
        elif hasattr(args, "id") and (bindings.path(args.id).exists() or bindings.path(args.id).is_symlink()):
            bound = bindings.load(args.id)
            doc = account_lifecycle.load(bindings, args.id)
            if doc["phase"] == "deleted":
                raise Error("not_found", "This agent was removed from management. Private state and its binding are retained.")
            if doc["phase"] != "ready" and not (args.operation == "delete" and doc["phase"] in {"deleting", "unit_removed"}):
                raise Error("account_incomplete", "The dedicated-account lifecycle is incomplete. Retry the original create/delete command.")
            return account_lifecycle.Lifecycle(store, bound, interactive=not args.json).execute(args, doc)
    if args.operation in {"worker", "prompt"}:
        raise Error("dedicated_required", "This command requires an existing dedicated-account agent.")
    supervisor = Systemd(store, args.unit_dir)
    guided = None
    if args.operation == "create" and not args.config:
        # Do not hold the lifecycle lock while the operator is at a prompt.
        supervisor.check()
        guided = guided_create(args.id, store.directory(args.id), store.root)
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
            if doctor_progress:
                print("Checking local Buzz, buzz-acp, Goose and OpenSSL interfaces...", file=sys.stderr, flush=True)
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
            return {"agents": separate + [view(store, supervisor, store.load(ident)) for ident in store.ids()]}
        if args.operation == "create":
            if bindings.path(args.id).exists() or bindings.path(args.id).is_symlink():
                raise Error("already_exists", "This ID is reserved by a dedicated-account binding.")
            directory = store.directory(args.id)
            if directory.exists() or directory.is_symlink():
                raise Error("already_exists", "This ID already has managed or retained data. Choose another ID; delete retains private data and identity.")
            prompt = None
            if guided is None:
                c = config.load(args.config)
            else:
                c, prompt = guided
            key, identity = generate()
            doc = {"version": 1, "id": args.id, "identity": identity, "config": c, "desired": "stopped", "revision": 1}
            try:
                private_dir(directory)
                atomic(directory / "identity.key", key.encode())
                if prompt is not None:
                    atomic(directory / "user-prompt.txt", prompt.encode("utf-8"))
                prepare(store, doc, snapshot=False)
                store.save(doc)
            except BaseException:
                if directory.exists():
                    shutil.rmtree(directory)
                raise
            try:
                supervisor.install(args.id, new=True)
            except PreexistingUnit:
                # This conflict is detected before the new unit is written.
                shutil.rmtree(directory)
                raise
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


def guided_create(ident, directory, root, dedicated=None):
    """Collect choices only; all validation and confirmation precede agent state."""
    from .runtime import tool

    def ask(label, default=None):
        suffix = f" [{default}]" if default is not None else ""
        try:
            value = input(f"{label}{suffix}: ")
        except EOFError:
            raise Error("creation_cancelled", "Creation cancelled; no agent was created.") from None
        return value if value else (default if default is not None else "")

    print("Create an agent. Press Ctrl+C or Ctrl+D to discard. Enter only the operator's public Buzz key; never a private key.")
    name = ask("Agent name", ident)
    about = ask("Profile description (optional)", "")
    avatar = ask("Avatar HTTPS URL (optional)", "")
    while True:
        owner = operator_public_key(ask("Operator Buzz public key (64 lowercase hex characters or npub)"))
        if owner is not None:
            break
        print("Enter a valid public key in lowercase hex (64 characters) or npub format.", file=sys.stderr)
    relay = ask("Relay URL (wss://)")
    prompt = ask("System prompt (single line)", "You are a helpful assistant.")
    if len(prompt.encode("utf-8")) > 65536:
        raise Error("invalid_config", "System prompt must be UTF-8 text of at most 64 KiB.")
    c = {"relay_url": relay, "owner": owner, "profile": {"name": name, "about": about, "avatar": avatar},
         "prompt_file": str(directory / "user-prompt.txt"), "provider": {"kind": "goose-managed"},
         "harness": {"kind": "goose", "command": str(Path(root) / "tools/bin/goose") if dedicated else tool("goose", root)}}
    config.validate(c)
    config.validate_relay_transport(relay)
    if not config.text(prompt, 65536, True):
        raise Error("invalid_config", "System prompt contains invalid characters.")
    if dedicated:
        print(f"The agent will be created stopped as {dedicated.account} (UID {dedicated.uid}). Authorize fixed system tools through ordinary sudo; the agent receives no management CLI or sudo grant.")
    else:
        print("The agent will be created stopped under the current Linux account. Configure provider credentials later with inference configure.")
    if ask("Create agent? Enter yes to confirm", "no").lower() != "yes":
        raise Error("creation_cancelled", "Creation discarded; no agent was created.")
    return c, prompt


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
            "procedure": "docs/service-accounts.md", "authorization_required": "The operator must explicitly authorize fixed system-unit tools and target-UID operations through ordinary sudo. No account creation/deletion, permission grant or linger change."}


def render(operation, data):
    if operation == "list":
        if not data["agents"]:
            return "No managed agents."
        return "\n".join(f"{a['id']}  observed {a['service_state']}  operator requested {a['desired']}  identity={a['identity']}" for a in data["agents"])
    if operation == "logs":
        return "\n".join(f"{e['time']} {e['event']}" for e in data["events"]) or "No lifecycle events yet."
    if operation == "update":
        if "updated" not in data:
            if data["auto_update"]:
                return "Interactive update offers enabled. Run buzz-server update to check now."
            return "Interactive update offers disabled. Run buzz-server update to check manually."
        if data["updated"]:
            return (f"Updated Buzz Server from {data['previous_version']} to {data['version']}. "
                    "Workers were not restarted.")
        return f"Buzz Server {data['version']} is already current. No newer stable release was found."
    if operation == "doctor":
        if "service_account" in data:
            name = data["service_account"]["name"]
            return (f"Dedicated-account preflight passed for {name}. Account layout and system manager reachability verified.\n"
                    f"Runtime tools were not checked. {DOCTOR_SCOPE}\n"
                    f"Next, run buzz-server create AGENT --account {name} and follow docs/quickstart.md.")
        return ("Local preflight passed. The systemd user manager and local Buzz, buzz-acp, Goose and "
                f"OpenSSL secp256k1 interfaces were verified.\nGoose executable: {data['goose_path']}\n{DOCTOR_SCOPE}")
    if "service_state" in data:
        heading = (f"{data['id']}: created and left stopped." if operation == "create" else
                   f"{data['id']}: systemd reports {data['service_state']}; operator requested {data['desired']}.")
        lines = [heading, f"Buzz identity: {data['identity']}", f"Service account: {data['service_account']['name']}"]
        if operation == "create":
            lines.append(f"Next, run buzz-server inference configure {data['id']}. Follow docs/quickstart.md for relay admission before first start.")
        elif "startup" in data:
            from .account_lifecycle import render_startup
            lines.extend(render_startup(data))
            deployment = data["deployment"]
            selected = deployment["selected_worker"]
            if selected is None:
                lines.append(f"Worker version unrecorded. Run buzz-server worker upgrade {data['id']} to install a verified payload.")
            else:
                pins = ", ".join(f"{name} {pin['version']}" for name, pin in sorted(selected["tools"].items()))
                lines.append(f"Selected worker {selected['version']} ({pins}).")
                if deployment["running_worker"] is not None:
                    lines.append(f"Running worker {selected['version']}.")
            if deployment["prompt_revision"]:
                lines.append(f"Approved prompt revision {deployment['prompt_revision']}.")
        else:
            lines.append("Local runtime process observed." if data["runtime_started"] else "Local runtime readiness unverified.")
            lines.append("Buzz reply untested. See First reply in docs/quickstart.md.")
        return "\n".join(lines)
    if operation == "inference":
        return (f"{data['id']}: Goose exited successfully. A clean exit can also mean setup was cancelled.\n"
                "Provider access and Buzz replies remain unverified.\n"
                f"If you completed setup, follow docs/quickstart.md to admit the key if required, start the agent and test a reply.\n"
                f"If you cancelled, rerun buzz-server inference configure {data['id']}.")
    if operation == "delete":
        return f"{data['id']}: stopped and removed from management. Private data, Linux account and Buzz history retained."
    if operation == "dependencies":
        return "Refreshed private tools: " + ", ".join(data["dependencies_refreshed"]) + "."
    if operation == "install":
        lines = [f"Installed Buzz Server {data['version']} at {data['prefix']}."]
        if data.get("dependencies_installed"):
            lines.append("Installed private tools: " + ", ".join(data["dependencies_installed"]) + ".")
        if data.get("dependency_error"):
            lines.append(data["dependency_error"])
        if data["missing_runtime_tools"]:
            lines.append("Missing runtime tools: " + ", ".join(data["missing_runtime_tools"]) + ".")
            lines.append("Install prerequisites yourself: https://github.com/block/buzz/releases/tag/desktop-v0.5.30 and https://github.com/aaif-goose/goose/releases/tag/v1.60.2; see docs/releases.md.")
        lines.append("Next: run buzz-server doctor, then follow docs/quickstart.md. No services were started.")
        if Path(data["prefix"]) != Path.home().absolute() / ".local/buzz-server-cli":
            lines.append("For a custom prefix, use its bin/buzz-server launcher; see docs/releases.md.")
        return "\n".join(lines)
    return json.dumps(data, indent=2)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print(__version__)
        return 0
    structured = "--json" in argv
    operation = "unknown"
    try:
        args = parser().parse_args(argv)
        operation = args.operation
        structured = args.json
        prefix = release.location()
        ordinary = operation not in ("install", "update", "uninstall", "version", "account-plan")
        if operation != "doctor" or not args.account:
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
        heading = "Error"
        if operation == "doctor":
            heading = "Dedicated-account preflight failed" if args.account else "Local preflight failed"
            message += f"\nPreflight did not complete. {DOCTOR_SCOPE}"
        elif operation == "update":
            heading = "Could not change interactive update offers" if args.auto else "Buzz Server update failed"
        print(f"{heading} [{code}]: {message}", file=sys.stderr)
    return 1
