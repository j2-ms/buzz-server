"""Separate-account orchestration; operator state alone authorizes effects."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import stat
import sys
import tempfile
import time

from . import config, dependencies, worker_payload
from .accounts import Bindings
from .account_commands import Commands
from .account_supervisor import AccountSystemd
from .errors import Error
from .storage import read_json, read_private, write_json


def manifest_path(bindings, ident):
    return bindings.root / (ident + ".agent.json")


def load(bindings, ident):
    value = read_json(manifest_path(bindings, ident))
    if (not isinstance(value, dict) or set(value) != {"version", "id", "phase", "identity", "revision", "desired"}
            or type(value["version"]) is not int or value["version"] != 1 or value["id"] != ident
            or value["phase"] not in {"creating", "ready", "deleting", "unit_removed", "deleted"}
            or value["desired"] not in {"running", "stopped"}
            or type(value["revision"]) is not int or value["revision"] < 1
            or not (value["identity"] is None and value["phase"] == "creating" or
                    isinstance(value["identity"], str) and re.fullmatch(r"[0-9a-f]{64}", value["identity"]))):
        raise Error("unsafe_state", "The operator lifecycle record is corrupt. No action was authorized.")
    return value


def require_detached(store):
    """Called with installation-exclusive and state-mutation locks held."""
    directory = store.root / "accounts"
    if not directory.exists() and not directory.is_symlink():
        return
    try:
        bindings = Bindings(store)
        paths = list(bindings.root.glob("*.json"))
        sources = list(bindings.root.glob("*.unit"))
        if not paths and not sources:
            return
        bindings.trusted_layout()
        ids = {p.stem for p in paths if not p.name.endswith((".agent.json", ".unit-operation.json"))}
        if any(p.stem not in ids for p in sources):
            raise ValueError()
        # Orphan lifecycle/journal files cannot make uninstall appear safe.
        for path in paths:
            ident = path.name.removesuffix(".agent.json").removesuffix(".unit-operation.json")
            if path.name.endswith((".agent.json", ".unit-operation.json")) and ident not in ids:
                raise ValueError()
        for ident in sorted(ids):
            binding = bindings.load(ident)
            doc = load(bindings, ident)
            if doc["phase"] != "deleted" or doc["desired"] != "stopped":
                raise ValueError()
            if not Lifecycle(store, binding).supervisor.detached():
                raise ValueError()
    except (Error, OSError, ValueError, TypeError, KeyError):
        raise Error("dedicated_attached", "Uninstall refused: dedicated-account bindings may still own system units. Finish or retry create/delete with this installed CLI, delete each attached agent, then retry uninstall. Keep the operator state and bindings; inspect corrupt records offline.") from None


class Lifecycle:
    def __init__(self, store, binding, *, interactive=False):
        self.bindings = Bindings(store)
        self.binding = binding
        self.commands = Commands(self.bindings, binding, interactive=interactive)
        self.supervisor = AccountSystemd(self.commands)

    def save(self, doc):
        write_json(manifest_path(self.bindings, self.binding.id), doc)

    def target(self, operation, request=None, *, stdin=None, terminal=False):
        if operation not in {"install", "apply", "desired", "status", "inference", "delete"}:
            raise ValueError("unknown target operation")
        # Private prompts/configuration travel on stdin, never process argv.
        request_source = repr(request or {}) if terminal or operation == "install" else "json.loads(sys.stdin.buffer.read(131073))"
        program = (worker_payload.source("account_target") + "\nBINDING = " + repr(self.binding.document()) +
                   "\nOPERATION = " + repr(operation) + "\nREQUEST = " + request_source + "\nmain()\n")
        raw_request = None if terminal or operation == "install" else json.dumps(request or {}, ensure_ascii=False).encode()
        result = self.commands.target(program, input=raw_request, stdin=stdin, terminal=terminal)
        if terminal:
            return None
        try:
            if len(result) > 65536:
                raise ValueError()
            data = json.loads(result)
            if not isinstance(data, dict):
                raise ValueError()
            if operation in {"install", "desired", "delete"}:
                field = "installed" if operation == "install" else "applied"
                if set(data) != {field} or data[field] is not True:
                    raise ValueError()
            return data
        except (ValueError, UnicodeError):
            raise Error("target_report", "The target returned an invalid report. It does not authorize system-service actions.") from None

    def install_payload(self, *, structured=False):
        # Acquisition uses the existing pinned transport and exact extraction.
        # Nothing from the operator's install, state or home is copied wholesale.
        acquired_names = []
        with tempfile.TemporaryDirectory(prefix=".account-tools-", dir=self.bindings.root) as temp:
            stage = Path(temp)
            files = {"worker.pyz": stage / "worker.pyz"}
            files["worker.pyz"].write_bytes(worker_payload.build())
            installed = dependencies.bin_dir(self.bindings.store.root)
            for package in dependencies.PACKAGES:
                sub = stage / package["name"]
                sub.mkdir(mode=0o700)
                missing = []
                for name, size in package["members"].values():
                    path = installed / name
                    if not path.exists():
                        missing.append(name)
                        continue
                    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    with os.fdopen(fd, "rb") as source:
                        s = os.fstat(source.fileno())
                        if (not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or s.st_nlink != 1
                                or stat.S_IMODE(s.st_mode) != 0o700 or s.st_size != size):
                            dependencies.refused()
                        with (sub / name).open("xb") as destination:
                            shutil.copyfileobj(source, destination, 65536)
                if missing:
                    if not structured:
                        print("Acquiring missing pinned dedicated-worker tools: " + ", ".join(missing) + ".", file=sys.stderr)
                    acquired = sub / "acquired"
                    acquired.mkdir(mode=0o700)
                    dependencies.acquire(package, acquired)
                    for name in missing:
                        os.replace(acquired / name, sub / name)
                    acquired_names.extend(missing)
                for name, _ in package["members"].values():
                    files[name] = sub / name
            header = {}
            for name, path in files.items():
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    for block in iter(lambda: stream.read(65536), b""):
                        digest.update(block)
                header[name] = {"size": path.stat().st_size, "sha256": digest.hexdigest()}
            raw = json.dumps(header, sort_keys=True).encode()
            # A private file supplies stdin so the pinned ~250 MiB binaries are
            # never materialized together in Python memory or command arguments.
            with tempfile.TemporaryFile(dir=stage) as stream:
                stream.write(struct.pack("!I", len(raw)))
                stream.write(raw)
                for name in ("worker.pyz", "buzz", "buzz-acp", "goose"):
                    with files[name].open("rb") as source:
                        shutil.copyfileobj(source, stream, 65536)
                stream.seek(0)
                self.commands.authenticate()
                self.target("install", stdin=stream)
        return acquired_names

    def apply(self, doc, path, *, prepared=None):
        candidate, prompt = prepared if prepared is not None else (
            config.load(path), None)
        if prompt is None:
            prompt = read_private(candidate["prompt_file"], 65536).decode("utf-8")
        if not config.text(prompt, 65536, True):
            config.invalid()
        result = self.target("apply", {"config": candidate, "prompt": prompt,
                                       "revision": doc["revision"], "identity": doc["identity"]})
        if (set(result) != {"identity", "revision"} or not isinstance(result["identity"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", result["identity"])
                or type(result["revision"]) is not int or result["revision"] != doc["revision"]
                or doc["identity"] is not None and doc["identity"] != result["identity"]):
            raise Error("target_report", "The target configuration report was invalid; no identity replacement was accepted.")
        doc["identity"] = result["identity"]
        self.save(doc)

    def create(self, doc, path, *, prepared=None, structured=False):
        # Validate input before downloads, target execution or root unit creation.
        if prepared is None:
            candidate = config.load(path)
            read_private(candidate["prompt_file"], 65536).decode("utf-8")
        acquired = self.install_payload(structured=structured)
        self.apply(doc, path, prepared=prepared)
        self.supervisor.install()
        doc["phase"] = "ready"
        self.save(doc)
        return {**self.view(doc), "tools_acquired": acquired}

    def view(self, doc):
        state = self.supervisor.inspect()
        return {"id": doc["id"], "identity": doc["identity"], "revision": doc["revision"],
                "desired": doc["desired"], "service_state": state["ActiveState"], "service_result": state.get("Result", "unknown"),
                "runtime_started": False, "buzz_response_verified": False,
                "service_account": {"uid": self.binding.uid, "name": self.binding.account},
                "configuration": {"harness": "goose", "provider": "goose-managed", "model": None,
                                  "profile_configured": True, "prompt_configured": True},
                "unit": self.binding.service, "service_scope": "system", "phase": doc["phase"]}

    def desired(self, doc, value):
        doc["desired"] = value
        self.save(doc)
        self.target("desired", {"identity": doc["identity"], "desired": value})

    def start(self, doc):
        self.supervisor.inspect()
        self.desired(doc, "running")
        self.supervisor.start()
        for _ in range(100):
            state = self.supervisor.inspect()
            if state["ActiveState"] == "active" and int(state["MainPID"]) > 1 and state["UnitFileState"] == "enabled":
                return self.view(doc)
            time.sleep(0.1)
        raise Error("start_unverified", "The system unit did not reach active state in ten seconds. Desired state remains running; stop cancels retries. No runtime or Buzz reply was verified.")

    def stop(self, doc):
        doc["desired"] = "stopped"
        self.save(doc)
        # Trusted system proof comes first. An agent can neither prevent nor
        # authorize the stop by forging or withholding its target-side report.
        self.supervisor.stop()
        self.target("desired", {"identity": doc["identity"], "desired": "stopped"})

    def execute(self, args, doc):
        op = args.operation
        if op in {"show", "status"}:
            return self.view(doc)
        if op == "start":
            self.commands.authenticate()
            return self.start(doc)
        if op == "stop":
            self.commands.authenticate()
            self.stop(doc)
            return self.view(doc)
        if op == "configure":
            config.load(args.config)
            self.commands.authenticate()
            running = doc["desired"] == "running"
            self.stop(doc)
            doc["revision"] += 1
            self.save(doc)
            self.apply(doc, args.config)
            return self.start(doc) if running else self.view(doc)
        if op == "inference":
            if doc["desired"] != "stopped":
                raise Error("stop_required", "Explicitly stop and verify this system unit before Goose setup.")
            self.commands.authenticate()
            if not self.supervisor.stopped():
                raise Error("stop_required", "Explicitly stop and verify this system unit before Goose setup.")
            self.target("inference", {"identity": doc["identity"]}, terminal=True)
            return {"id": doc["id"], "setup_exited": True, "buzz_response_verified": False}
        if op == "delete":
            self.commands.authenticate()
            if doc["phase"] != "unit_removed":
                doc["phase"] = "deleting"
                doc["desired"] = "stopped"
                self.save(doc)
                # remove() also handles its own journaled post-unlink retry.
                if not self.supervisor.journal.exists():
                    self.supervisor.stop()
                self.supervisor.remove()
                doc["phase"] = "unit_removed"
                self.save(doc)
            else:
                self.supervisor.remove()
            self.target("delete", {"identity": doc["identity"]})
            doc["phase"] = "deleted"
            self.save(doc)
            return {"id": doc["id"], "deleted": True, "worker_stopped": True, "data_retained": True,
                    "linux_account_deleted": False, "buzz_history_deleted": False}
        raise Error("usage", "This command is not available for a dedicated-account agent.")
