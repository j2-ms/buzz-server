"""Operator-owned authorization for existing dedicated Linux accounts.

Nothing read from the target account is an authority to invoke sudo. Bindings
live beside (and use the same private-file rules as) the operator's own state.
"""
from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
import pwd
import re
import stat
from typing import NoReturn

from .errors import Error
from .storage import agent_id, private_dir, read_json, write_json


def refused() -> NoReturn:
    raise Error("account_binding", "Dedicated-account authorization is missing, changed or unsafe. No privileged action was authorized; inspect the operator binding and existing account offline.")


def account(name, *, diagnose=False):
    def reject(prerequisite) -> NoReturn:
        if diagnose:
            raise Error("account_binding", prerequisite + " Ask the host administrator to check the dedicated account, then rerun doctor. No privileged action was authorized.")
        refused()

    if not isinstance(name, str) or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", name):
        reject("Use a valid Linux account name for --account.")
    try:
        entry = pwd.getpwnam(name)
        reverse = pwd.getpwuid(entry.pw_uid)
    except KeyError:
        reject("The dedicated account must exist and resolve by name and UID.")
    if (type(entry.pw_uid) is not int or entry.pw_uid <= 0 or entry.pw_uid == os.getuid()
            or type(entry.pw_gid) is not int or entry.pw_gid <= 0
            or entry.pw_name != name or reverse.pw_name != name
            or reverse.pw_dir != entry.pw_dir or reverse.pw_gid != entry.pw_gid):
        reject("The dedicated account needs a non-root UID distinct from the operator, a non-root GID, and matching name and UID records.")
    home = Path(entry.pw_dir)
    # A deliberately narrow layout makes unit escaping and authority auditable.
    # The administrator supplies the account; this code never repairs its home.
    if (not re.fullmatch(r"/[A-Za-z0-9_./-]+", entry.pw_dir) or home == Path("/")
            or str(home) != entry.pw_dir or ".." in home.parts):
        reject("The dedicated account needs an absolute, normalized home path in the supported format.")
    for path in (home, *home.parents):
        location = "The dedicated account's home" if path == home else "A parent of the dedicated account's home"
        try:
            s = path.lstat()
        except OSError:
            reject(location + " could not be checked; verify that it exists and can be reached from the operator login.")
        if not stat.S_ISDIR(s.st_mode):
            reject(location + " must be a real directory, not a symlink or file.")
        if path == home:
            if s.st_uid != entry.pw_uid:
                reject(location + " must be owned by that account.")
            if stat.S_IMODE(s.st_mode) != 0o700:
                reject(location + " must have mode 0700.")
        elif s.st_uid != 0 or stat.S_IMODE(s.st_mode) & 0o022:
            reject(location + " must be root-owned and not writable by group or others.")
    return entry


@dataclass(frozen=True)
class Binding:
    id: str
    account: str
    uid: int
    gid: int
    home: str
    data: str
    service: str

    def document(self):
        return {"version": 1, **self.__dict__}


class Bindings:
    def __init__(self, store):
        self.store = store
        self.root = private_dir(store.root / "accounts")

    def path(self, ident):
        return self.root / (agent_id(ident) + ".json")

    def trusted_layout(self):
        # Root's fixed install tool reads a source under this operator path.
        # Private leaf permissions alone do not prevent a different UID that
        # owns an ancestor from replacing the path before root opens it.
        try:
            protected = False
            for path in reversed((self.root, *self.root.parents)):
                s = path.lstat()
                sticky_system_parent = s.st_uid == 0 and bool(s.st_mode & stat.S_ISVTX)
                if (not stat.S_ISDIR(s.st_mode) or s.st_uid not in {0, os.getuid()}
                        or stat.S_IMODE(s.st_mode) & 0o022 and not protected and not sticky_system_parent):
                    refused()
                # A private operator ancestor prevents other users/groups from
                # reaching writable descendants owned by the trusted operator.
                if s.st_uid == os.getuid() and not stat.S_IMODE(s.st_mode) & 0o077:
                    protected = True
        except OSError:
            refused()

    def expected(self, ident, entry):
        agent_id(ident)
        namespace = sha256((str(os.getuid()) + "\0" + str(self.store.root)).encode()).hexdigest()[:24]
        service = f"buzz-server-account-{namespace}-{ident}.service"
        data = str(Path(entry.pw_dir) / ".local/share/buzz-server-managed" / namespace / ident)
        return Binding(ident, entry.pw_name, entry.pw_uid, entry.pw_gid, entry.pw_dir, data, service)

    def reserve(self, ident, name):
        self.trusted_layout()
        path = self.path(ident)
        # Retained bindings deliberately reserve deleted IDs too.
        if path.exists() or path.is_symlink() or self.store.directory(ident).exists():
            raise Error("already_exists", "This ID has managed or retained data. Choose another ID.")
        binding = self.expected(ident, account(name))
        write_json(path, binding.document())
        return binding

    def load(self, ident):
        self.trusted_layout()
        saved = read_json(self.path(ident))
        if (not isinstance(saved, dict) or set(saved) != {"version", "id", "account", "uid", "gid", "home", "data", "service"}
                or type(saved["version"]) is not int or saved["version"] != 1
                or type(saved["uid"]) is not int or type(saved["gid"]) is not int
                or saved["id"] != ident):
            refused()
        expected = self.expected(ident, account(saved["account"]))
        if saved != expected.document():
            refused()
        return expected

    def revalidate(self, binding):
        if self.load(binding.id) != binding:
            refused()
