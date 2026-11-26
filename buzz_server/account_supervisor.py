"""Root-owned native system units, controlled by an unprivileged operator.

Only root-owned system tools run through sudo. The generated service drops to
the bound numeric UID/GID before invoking the fixed Python worker command.
"""
import re
import stat
from typing import NoReturn

from .errors import Error
from .runtime import STARTUP_EXIT
from .storage import atomic, read_json, write_json
from .supervisor import cgroup_empty


def conflict() -> NoReturn:
    raise Error("unit_conflict", "The trusted system manager or root-owned unit does not match the operator binding. No service effect was authorized. Inspect locally; keep private state.")


class AccountSystemd:
    PROPERTIES = ("Id", "LoadState", "ActiveState", "SubState", "MainPID", "FragmentPath",
                  "UnitFileState", "Result", "DropInPaths", "NeedDaemonReload", "KillMode", "ControlGroup",
                  "User", "Group", "ExecStart", "ExecCondition", "ExecStartPre", "ExecStartPost", "ExecStop", "ExecStopPost",
                  "ExecReload", "Environment", "EnvironmentFiles", "PassEnvironment", "WorkingDirectory",
                  "RootDirectory", "RootImage", "NoNewPrivileges", "Type", "NotifyAccess", "ExecMainCode", "ExecMainStatus")

    def __init__(self, commands, *, worker_slot=None, prompt_slot=None, legacy_type=None):
        self.commands = commands
        self.binding = commands.binding
        self.worker_slot = worker_slot
        self.prompt_slot = prompt_slot
        self.legacy_type = legacy_type
        self.source = commands.bindings.root / (self.binding.id + ".unit")
        self.journal = commands.bindings.root / (self.binding.id + ".unit-operation.json")

    @property
    def argv(self):
        b = self.binding
        if self.worker_slot is None:
            return ["/usr/bin/python3", "-I", b.data + "/worker.pyz", b.data + "/state", b.id]
        mode = "local" if self.prompt_slot is None else f"prompt-{self.prompt_slot}"
        return ["/usr/bin/python3", "-I", str(self.payload / "worker.pyz"), b.data + "/state", b.id, mode]

    @property
    def payload(self):
        return None if self.worker_slot is None else self.commands.deployment_root / f"worker-{self.worker_slot}"

    @property
    def credential(self):
        if self.prompt_slot is None:
            return None
        return self.commands.deployment_root / f"prompt-{self.prompt_slot}" / "prompt"

    def content(self, *, legacy=None):
        b = self.binding
        # Every substituted value comes from the validated operator binding,
        # with a restricted alphabet. No target config, paths or command hooks.
        # Even four full start timeouts, stop timeouts and delays fit inside this window.
        legacy = self.legacy_type == "exec" if legacy is None else legacy
        limits = "StartLimitIntervalSec=0\n" if legacy else "StartLimitIntervalSec=300\nStartLimitBurst=4\n"
        prevent = f"{STARTUP_EXIT['local_setup_refused']} {STARTUP_EXIT['profile_refused']}"
        startup = ("Type=exec\n" if legacy else "Type=notify\nNotifyAccess=main\nTimeoutStartSec=30\n"
                   f"RestartPreventExitStatus={prevent}\n")
        return ("[Unit]\nDescription=Buzz Server dedicated-account agent\n" + limits +
                "[Service]\n" + startup + "User=" + str(b.uid) + "\nGroup=" + str(b.gid) +
                "\nExecStart=:" + " ".join(self.argv) +
                (f"\nLoadCredential=prompt:{self.credential}" if self.credential else "") +
                "\nWorkingDirectory=/\nEnvironment=PATH=/usr/bin:/bin LANG=C.UTF-8 HOME=" + b.home +
                "\nUMask=0077\nNoNewPrivileges=yes\nKillMode=control-group\nTimeoutStopSec=10\n"
                "Restart=on-failure\nRestartSec=5\nStandardOutput=null\nStandardError=null\n"
                "[Install]\nWantedBy=multi-user.target\n").encode("ascii")

    def journal_operation(self):
        value = read_json(self.journal)
        if (not isinstance(value, dict) or set(value) != {"version", "operation"}
                or type(value["version"]) is not int or value["version"] != 1
                or value["operation"] not in ("install", "remove")):
            conflict()
        return value["operation"]

    def root_file(self, path, *, exact=True, missing=False):
        try:
            for parent in path.parents:
                s = parent.lstat()
                if not stat.S_ISDIR(s.st_mode) or s.st_uid != 0 or stat.S_IMODE(s.st_mode) & 0o022:
                    conflict()
            s = path.lstat()
        except FileNotFoundError:
            if missing:
                return False
            conflict()
        modes = {0o644} if exact else {0o600, 0o644}
        if not stat.S_ISREG(s.st_mode) or s.st_uid != 0 or s.st_gid != 0 or stat.S_IMODE(s.st_mode) not in modes:
            conflict()
        if exact:
            current = self.content()
            legacy = self.content(legacy=True)
            with path.open("rb") as stream:
                content = stream.read(len(current) + 1)
            if content == current:
                return self.legacy_type or "notify"
            if self.worker_slot is None and self.legacy_type is None and path == self.commands.unit_path and content == legacy:
                return "exec"
            conflict()
        return True

    def read_state(self):
        self.commands.bindings.revalidate(self.binding)
        raw = self.commands.show(self.PROPERTIES)
        try:
            lines = raw.decode("utf-8").splitlines()
            state = dict(line.split("=", 1) for line in lines)
            if len(state) != len(lines):
                conflict()
        except (ValueError, UnicodeError):
            conflict()
        return state

    def inspect(self, *, missing=False, removing=False):
        state = self.read_state()
        if missing and state.get("LoadState") == "not-found":
            if (state.get("DropInPaths") != "" or state.get("FragmentPath") != ""
                    or state.get("ActiveState") not in {"inactive", "failed"}
                    or state.get("MainPID") != "0" or "ControlGroup" not in state
                    or not cgroup_empty(state["ControlGroup"])):
                conflict()
            return state
        if removing:
            if (self.journal_operation() != "remove"
                    or self.root_file(self.commands.unit_path, missing=True)
                    or state.get("NeedDaemonReload") not in {"yes", "no"}
                    or state.get("ActiveState") not in {"inactive", "failed"}
                    or state.get("MainPID") != "0" or "ControlGroup" not in state
                    or not cgroup_empty(state["ControlGroup"])):
                conflict()
            unit_type = state.get("Type")
        else:
            unit_type = self.root_file(self.commands.unit_path)
        return self.check_state(state, unit_type, removing=removing)

    def check_state(self, state, unit_type, *, removing=False, reloading=False):
        expected = {"Id": self.binding.service, "LoadState": "loaded",
                    "FragmentPath": str(self.commands.unit_path), "DropInPaths": "",
                    "NeedDaemonReload": state.get("NeedDaemonReload") if removing or reloading else "no",
                    "KillMode": "control-group",
                    "User": str(self.binding.uid), "Group": str(self.binding.gid),
                    "Environment": "PATH=/usr/bin:/bin LANG=C.UTF-8 HOME=" + self.binding.home, "PassEnvironment": "",
                    "WorkingDirectory": "/", "RootDirectory": "", "RootImage": "", "NoNewPrivileges": "yes",
                    "Type": unit_type, "NotifyAccess": "main" if unit_type == "notify" else "none"}
        if unit_type not in {"notify", "exec"}:
            conflict()
        if state.get("NeedDaemonReload") not in {"yes", "no"} or any(state.get(k) != v for k, v in expected.items()):
            conflict()
        # systemctl 255 omits empty structured arrays even with --all. The
        # string-array printer still emits PassEnvironment=; require it above.
        if any(state.get(k, "") != "" for k in ("ExecCondition", "ExecStartPre", "ExecStartPost", "ExecStop",
                                               "ExecStopPost", "ExecReload", "EnvironmentFiles")):
            conflict()
        # Compare the manager's executable and complete argv, allowing only its
        # runtime accounting fields after ignore_errors. No second command.
        prefix = "{ path=/usr/bin/python3 ; argv[]=" + " ".join(self.argv) + " ; ignore_errors=no ; "
        execution = state.get("ExecStart", "")
        if (not execution.startswith(prefix) or not execution.endswith(" }")
                or not re.fullmatch(r"start_time=[^;{}]* ; stop_time=[^;{}]* ; pid=[0-9]+ ; code=[^;{}]* ; status=[^;{}]*", execution[len(prefix):-2])):
            conflict()
        if (state.get("ActiveState") not in {"active", "inactive", "failed", "activating", "deactivating", "reloading"}
                or not state.get("MainPID", "").isdigit() or "ControlGroup" not in state
                or state.get("UnitFileState") not in ({"disabled", "static", "not-found", ""} if removing else {"enabled", "disabled", "static"})):
            conflict()
        return state

    def replacement_installed(self):
        path = self.commands.unit_path
        self.root_file(path, exact=False)
        content = self.content()
        with path.open("rb") as stream:
            return stream.read(len(content) + 1) == content

    @staticmethod
    def require_disabled(state):
        if (state["ActiveState"] not in {"inactive", "failed"} or state["MainPID"] != "0"
                or state["UnitFileState"] not in {"disabled", "static"}
                or not cgroup_empty(state["ControlGroup"])):
            raise Error("stop_unverified", "The deployment switch requires an inactive, disabled unit with an empty cgroup.")

    def replace(self, previous):
        """Resume a journaled switch. Only the two expected unit definitions count."""
        if self.replacement_installed():
            try:
                return self.inspect()
            except Error as error:
                if error.code != "unit_conflict":
                    raise
            # The root file was replaced, but the manager may still have the
            # former definition. Validate it and prove stopped before reloading.
            state = self.read_state()
            try:
                self.check_state(state, self.legacy_type or "notify", reloading=True)
            except Error:
                previous.check_state(state, previous.legacy_type or "notify", reloading=True)
            self.require_disabled(state)
        else:
            self.require_disabled(previous.inspect())
            if self.root_file(self.commands.stage_path, exact=False, missing=True):
                self.commands.unlink_unit(stage=True)
            atomic(self.source, self.content())
            self.commands.stage_unit(self.source)
            self.root_file(self.commands.stage_path)
            self.commands.replace_unit()
        self.commands.sync(self.commands.unit_path.parent)
        self.commands.reload()
        return self.inspect()

    def install(self):
        self.commands.bindings.revalidate(self.binding)
        # Journal the only root staging path before any root file creation.
        # An interrupted install can retry without guessing which files belong
        # to it; a pre-existing stage without this intent is a collision.
        pending = None
        if self.journal.exists() or self.journal.is_symlink():
            pending = self.journal_operation()
            if pending != "install":
                conflict()
        if self.root_file(self.commands.unit_path, missing=True):
            if pending is None:
                self.inspect()
                return
        else:
            state = self.inspect(missing=True)
            if state.get("LoadState") != "not-found":
                conflict()
            if pending is None:
                if self.root_file(self.commands.stage_path, exact=False, missing=True):
                    conflict()
                write_json(self.journal, {"version": 1, "operation": "install"})
            elif self.root_file(self.commands.stage_path, exact=False, missing=True):
                # Only a journaled, root-owned regular stage may be discarded.
                # It might contain interrupted install output; never execute it.
                self.commands.unlink_unit(stage=True)
            atomic(self.source, self.content())
            self.commands.stage_unit(self.source)
            self.root_file(self.commands.stage_path)
            self.commands.publish_unit()
            self.root_file(self.commands.unit_path)
        if self.root_file(self.commands.stage_path, missing=True):
            self.commands.unlink_unit(stage=True)
        self.commands.reload()
        self.inspect()
        self.journal.unlink(missing_ok=True)

    def stopped(self):
        state = self.inspect()
        return (state["ActiveState"] in {"inactive", "failed"} and state["MainPID"] == "0"
                and cgroup_empty(state["ControlGroup"]))

    def control(self, operation):
        self.inspect()
        self.commands.control(operation)
        return self.inspect()

    def start(self):
        if self.inspect()["ActiveState"] == "failed":
            self.control("reset-failed")
        self.control("enable")
        self.control("start")

    def stop(self):
        self.control("disable")
        self.control("stop")
        if not self.stopped() or self.inspect()["UnitFileState"] not in {"disabled", "static"}:
            raise Error("stop_unverified", "Service stop, disabled autostart and empty cgroup were not verified. Keep the binding, unit and private data; retry stop.")

    def remove(self):
        if self.journal.exists() or self.journal.is_symlink():
            if self.journal_operation() != "remove":
                conflict()
            if not self.root_file(self.commands.unit_path, missing=True):
                # A missing fragment alone does not prove that a previously
                # loaded service has no remaining processes. Recheck the trusted
                # manager before any retry effect, as well as after reload.
                self.inspect(missing=True, removing=True)
                self.commands.reload()
                if self.inspect(missing=True).get("LoadState") != "not-found":
                    conflict()
                return
        if not self.stopped() or self.inspect()["UnitFileState"] not in {"disabled", "static"}:
            raise Error("stop_unverified", "Refusing unit removal without verified stop and disabled autostart.")
        # Removal is journaled before unlink. Retry can verify absence against
        # the manager; target reports never authorize this operation.
        write_json(self.journal, {"version": 1, "operation": "remove"})
        self.commands.unlink_unit()
        self.commands.reload()
        if self.inspect(missing=True).get("LoadState") != "not-found":
            raise Error("delete_unverified", "The system manager still knows this unit. Private data and binding were retained; retry delete.")
        # Keep the private removal proof so a crash between root removal and
        # target retirement can retry without treating an unknown unit as owned.

    def detached(self):
        if self.journal_operation() != "remove":
            return False
        if (self.root_file(self.commands.unit_path, missing=True)
                or self.root_file(self.commands.stage_path, exact=False, missing=True)):
            return False
        state = self.inspect(missing=True)
        return (state.get("LoadState") == "not-found" and state.get("ActiveState") in {"inactive", "failed"}
                and state.get("MainPID") == "0" and "ControlGroup" in state and cgroup_empty(state["ControlGroup"]))
