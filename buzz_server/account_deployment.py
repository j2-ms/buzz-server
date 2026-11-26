"""Operator-side selection of dedicated worker payloads and approved prompts."""
from contextlib import contextmanager
import hashlib
from pathlib import Path
import re
import secrets
import tempfile

from . import __version__, config, dependencies, worker_payload
from .account_commands import PAYLOAD_MODES
from .account_supervisor import AccountSystemd, conflict
from .errors import Error
from .storage import read_json, read_private, write_json


SUFFIX = ".deployment.json"
SLOTS = (0, 1, 2)
ACTIONS = {"worker upgrade", "worker rollback", "prompt activate", "prompt rollback", "prompt clear"}


def valid_slot(slot):
    return type(slot) is int and slot in SLOTS


def digest(value, size):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]+", value) is not None and len(value) == size


def valid_worker(value):
    if not isinstance(value, dict):
        return False
    if set(value) == {"legacy"}:
        return value["legacy"] in ("exec", "notify")
    if (set(value) != {"slot", "version", "sha256", "tools"} or not valid_slot(value["slot"])
            or not isinstance(value["version"], str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value["version"])
            or not digest(value["sha256"], 64) or not isinstance(value["tools"], dict)
            or set(value["tools"]) != {"buzz", "goose"}):
        return False
    return all(isinstance(pin, dict) and set(pin) == {"version", "sha256"}
               and isinstance(pin["version"], str) and re.fullmatch(r"[A-Za-z0-9.-]{1,40}", pin["version"])
               and digest(pin["sha256"], 64) for pin in value["tools"].values())


def valid_prompt(value):
    return (value is None or isinstance(value, dict) and set(value) == {"slot", "revision"}
            and valid_slot(value["slot"]) and digest(value["revision"], 32))


def valid_selection(value):
    return (isinstance(value, dict) and set(value) == {"worker", "prompt"} and valid_worker(value["worker"])
            and valid_prompt(value["prompt"]) and (value["prompt"] is None or "slot" in value["worker"]))


def slot_for(active, previous):
    used = {entry.get("slot") for entry in (active, previous) if entry is not None}
    return next(slot for slot in SLOTS if slot not in used)


def same_slot(left, right):
    return left is not None and right is not None and left.get("slot") == right.get("slot")


def worker_metadata(payload):
    return {"version": __version__, "sha256": hashlib.sha256(payload).hexdigest(),
            "tools": {p["name"]: {"version": p["version"], "sha256": p["sha256"]} for p in dependencies.PACKAGES}}


def matches_worker(candidate, metadata):
    return all(candidate.get(key) == value for key, value in metadata.items())


def read_prompt(source):
    prompt = read_private(source, 65536)
    try:
        if not config.text(prompt.decode("utf-8"), 65536, True):
            raise ValueError()
    except ValueError:
        raise Error("invalid_config", "Use an operator-owned private UTF-8 prompt file of at most 64 KiB.") from None
    return prompt


class Deployment:
    def __init__(self, lifecycle):
        self.lifecycle = lifecycle
        self.commands = lifecycle.commands
        self.path = lifecycle.bindings.root / (lifecycle.binding.id + SUFFIX)
        self.record = None
        if self.path.exists() or self.path.is_symlink():
            self.record = self.load()

    def load(self):
        value = read_json(self.path)
        if (not isinstance(value, dict) or set(value) != {"version", "active", "previous", "pending"}
                or type(value["version"]) is not int or value["version"] != 1
                or not valid_selection(value["active"]) or not isinstance(value["previous"], dict)
                or set(value["previous"]) != {"worker", "prompt"} or not valid_prompt(value["previous"]["prompt"])
                or value["previous"]["worker"] is not None and not valid_worker(value["previous"]["worker"])):
            conflict()
        active, previous, pending = value["active"], value["previous"], value["pending"]
        if any(same_slot(active[field], previous[field]) for field in ("worker", "prompt")):
            conflict()
        if pending is not None:
            if (not isinstance(pending, dict) or set(pending) != {"action", "desired", "next"}
                    or not isinstance(pending["action"], str) or pending["action"] not in ACTIONS
                    or pending["desired"] not in ("stopped", "running") or not valid_selection(pending["next"])):
                conflict()
            field, verb = pending["action"].split()
            other = "prompt" if field == "worker" else "worker"
            candidate = pending["next"][field]
            if (pending["next"][other] != active[other] or candidate == active[field]
                    or same_slot(candidate, active[field])
                    or verb == "rollback" and (candidate is None or candidate != previous[field])
                    or verb == "clear" and candidate is not None
                    or verb in {"upgrade", "activate"} and (candidate is None or "slot" not in candidate
                                                            or same_slot(candidate, previous[field]))):
                conflict()
        return value

    def supervisor(self, selected):
        return AccountSystemd(self.commands, worker_slot=selected["worker"].get("slot"),
                              prompt_slot=selected["prompt"]["slot"] if selected["prompt"] else None,
                              legacy_type=selected["worker"].get("legacy"))

    def select(self):
        if self.record:
            self.lifecycle.supervisor = self.supervisor(self.record["active"])

    def validate(self, supervisor):
        if supervisor.payload is not None:
            for name, mode in PAYLOAD_MODES.items():
                self.commands.deployment_file(supervisor.payload / name, mode)
        if supervisor.credential is not None:
            self.commands.deployment_file(supervisor.credential, 0o600)

    def report(self, started):
        if self.record is None:
            return {"selected_worker": None, "running_worker": None, "previous_worker": None,
                    "worker_rollback_available": False, "prompt_revision": None, "previous_prompt_revision": None}
        active, previous = self.record["active"], self.record["previous"]

        def worker(entry):
            if entry is None or "legacy" in entry:
                return None
            return {key: entry[key] for key in ("version", "sha256", "tools")}

        def prompt(entry):
            return entry["revision"] if entry is not None else None

        selected = worker(active["worker"])
        return {"selected_worker": selected, "running_worker": selected if started else None,
                "previous_worker": worker(previous["worker"]), "worker_rollback_available": previous["worker"] is not None,
                "prompt_revision": prompt(active["prompt"]), "previous_prompt_revision": prompt(previous["prompt"])}

    def guard(self, action):
        if self.record and self.record["pending"]:
            pending = self.record["pending"]["action"]
            if action != pending:
                command = f"buzz-server {pending} {self.lifecycle.binding.id}"
                if pending == "prompt activate":
                    command += " --file PATH"
                raise Error("deployment_incomplete", f"Retry the original {command} command to finish the interrupted deployment.")

    def stage_worker(self, active, previous, *, structured):
        payload = worker_payload.build()
        version = worker_metadata(payload)
        if matches_worker(active, version):
            return active
        slot = slot_for(active, previous)
        with tempfile.TemporaryDirectory(prefix=".deployment-", dir=self.path.parent) as temp:
            stage = Path(temp)
            (stage / "worker.pyz").write_bytes(payload)
            (stage / "buzz-acp.toml").touch()
            dependencies.stage_pins(stage, show_progress=not structured and dependencies.interactive())
            self.commands.authenticate()
            self.commands.stage_deployment(slot, {name: stage / name for name in PAYLOAD_MODES})
        return {"slot": slot, **version}

    @contextmanager
    def prompt_source(self, prompt):
        with tempfile.TemporaryDirectory(prefix=".deployment-", dir=self.path.parent) as temp:
            staged = Path(temp) / "prompt"
            staged.touch(mode=0o600)
            staged.write_bytes(prompt)
            yield staged

    def stage_prompt(self, source, active, previous):
        prompt = read_prompt(source)
        slot = slot_for(active, previous)
        with self.prompt_source(prompt) as staged:
            self.commands.authenticate()
            self.commands.stage_deployment(slot, {"prompt": staged}, prompt=True)
        return {"slot": slot, "revision": secrets.token_hex(16)}

    def change(self, args, doc, action):
        lifecycle = self.lifecycle
        self.guard(action)
        if self.record and self.record["pending"]:
            selected = self.record["pending"]["next"]
            if action == "worker upgrade" and not matches_worker(selected["worker"], worker_metadata(worker_payload.build())):
                raise Error("deployment_incomplete", f"The CLI builds a different worker from the staged payload. Retry with the original CLI or run buzz-server stop {doc['id']} before upgrading again.")
            prompt = read_prompt(args.file) if action == "prompt activate" else None
            self.commands.authenticate()
            if prompt is not None:
                with self.prompt_source(prompt) as staged:
                    matches = self.commands.prompt_matches(selected["prompt"]["slot"], staged)
                if not matches:
                    raise Error("deployment_incomplete", f"The supplied prompt could not be matched to the staged revision. Verify operator sudo and retry with the original bytes, or run buzz-server stop {doc['id']} before activating a different revision.")
            return self.finish(doc)

        state = lifecycle.supervisor.inspect()
        if self.record is None:
            self.record = {"version": 1, "active": {"worker": {"legacy": state["Type"]}, "prompt": None},
                           "previous": {"worker": None, "prompt": None}, "pending": None}
        active, previous = self.record["active"], self.record["previous"]
        field = args.operation
        if field == "prompt" and "legacy" in active["worker"]:
            raise Error("worker_upgrade_required", f"Run buzz-server worker upgrade {doc['id']} before managing prompt revisions.")
        if lifecycle.supervisor.root_file(self.commands.stage_path, exact=False, missing=True):
            conflict()
        if action == "worker upgrade":
            candidate = self.stage_worker(active[field], previous[field], structured=args.json)
        elif action == "prompt activate":
            candidate = self.stage_prompt(args.file, active[field], previous[field])
        elif action == "prompt clear":
            candidate = None
        else:
            candidate = previous[field]
            if candidate is None:
                raise Error("rollback_unavailable", f"No previous {field} revision is available.")
            if field == "worker" and "legacy" in candidate and active["prompt"] is not None:
                raise Error("prompt_active", f"Run buzz-server prompt clear {doc['id']} before rolling back to the original worker.")

        if candidate == active[field]:
            self.validate(lifecycle.supervisor)
            return lifecycle.view(doc, state)
        if action not in ("worker upgrade", "prompt activate"):
            self.commands.authenticate()
        self.record["pending"] = {"action": action, "desired": doc["desired"], "next": {**active, field: candidate}}
        write_json(self.path, self.record)
        return self.finish(doc)

    def finish(self, doc):
        lifecycle = self.lifecycle
        active, pending = self.record["active"], self.record["pending"]
        previous, selected = self.supervisor(active), self.supervisor(pending["next"])
        self.validate(selected)
        if not selected.replacement_installed():
            lifecycle.supervisor = previous
            lifecycle.stop(doc)
        selected.replace(previous)
        lifecycle.supervisor = selected
        field = pending["action"].split()[0]
        self.record["previous"][field] = active[field]
        try:
            if pending["desired"] == "running":
                lifecycle.start(doc, report=False)
        except Error as error:
            # The new selection is installed. Keep both revisions available
            # even if local readiness failed or is still pending.
            self.commit(pending)
            rollback = "rollback" if self.record["previous"][field] is not None else "clear"
            raise Error(error.code, f"{error} The new selection is installed. Restore the previous selection with buzz-server {field} {rollback} {doc['id']}.") from None
        self.commit(pending)
        return lifecycle.view(doc)

    def commit(self, pending):
        self.record["active"] = pending["next"]
        self.record["pending"] = None
        write_json(self.path, self.record)

    def stop_pending(self, doc):
        """An explicit stop cancels an unpublished switch or stops its installed unit."""
        lifecycle = self.lifecycle
        active, pending = self.record["active"], self.record["pending"]
        pending["desired"] = "stopped"
        write_json(self.path, self.record)
        previous, selected = self.supervisor(active), self.supervisor(pending["next"])
        installed = selected.replacement_installed()
        if installed:
            selected.replace(previous)
        lifecycle.supervisor = selected if installed else previous
        lifecycle.stop(doc)
        if installed:
            field = pending["action"].split()[0]
            self.record["previous"][field] = active[field]
            self.commit(pending)
        else:
            if previous.root_file(self.commands.stage_path, exact=False, missing=True):
                self.commands.unlink_unit(stage=True)
            self.record["pending"] = None
            write_json(self.path, self.record)
        return lifecycle.view(doc)
