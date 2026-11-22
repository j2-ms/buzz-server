import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import NoReturn

from .errors import Error


def fail(message="State is corrupt or unsafe; restore a trusted backup or inspect permissions offline.") -> NoReturn:
    raise Error("unsafe_state", message)


def private_dir(path, *, allow_readable=False):
    path = Path(path)
    # Reject symlink components, including pre-existing parent directories.
    for part in (path, *path.parents):
        if part.is_symlink():
            fail("Symlink directories are not supported; use a private real directory.")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    s = path.stat()
    if s.st_uid != os.getuid() or stat.S_IMODE(s.st_mode) & (0o022 if allow_readable else 0o077):
        if allow_readable:
            fail("The systemd unit directory must be owned by the service user and not group- or world-writable.")
        fail("State directories must be owned by the service user with mode 0700.")
    return path


def read_private(path, limit=131072):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as f:
            s = os.fstat(f.fileno())
            if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or stat.S_IMODE(s.st_mode) & 0o077:
                fail()
            value = f.read(limit + 1)
            if len(value) > limit:
                fail()
            return value
    except OSError:
        fail()


def atomic(path, value, *, allow_readable_parent=False):
    path = Path(path)
    private_dir(path.parent, allow_readable=allow_readable_parent)
    fd, temp = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(value)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
        dirfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def write_json(path, value):
    atomic(path, (json.dumps(value, sort_keys=True) + "\n").encode())


def read_json(path):
    try:
        return json.loads(read_private(path))
    except (ValueError, UnicodeError):
        fail()


def agent_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", value):
        raise Error("invalid_id", "Use an agent ID of 1–40 lowercase letters, digits or hyphens, starting with a letter.")
    return value


class Store:
    def __init__(self, root):
        self.root = private_dir(Path(root).absolute())
        self.agents = private_dir(self.root / "agents")

    @contextlib.contextmanager
    def lock(self):
        fd = os.open(self.root / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def directory(self, ident):
        return self.agents / agent_id(ident)

    def load(self, ident):
        directory = self.directory(ident)
        if not (directory / "definition.json").exists():
            raise Error("not_found", "No managed agent with that ID. Use list; deleted data is retained separately.")
        private_dir(directory)
        doc = read_json(directory / "definition.json")
        from .config import validate_stored
        if not isinstance(doc, dict) or set(doc) != {"version", "id", "identity", "config", "desired", "revision"}:
            fail()
        if doc["version"] != 1 or doc["id"] != ident or doc["desired"] not in ("running", "stopped"):
            fail()
        if type(doc["revision"]) is not int or doc["revision"] < 1:
            fail()
        if not isinstance(doc["identity"], str) or not re.fullmatch("[0-9a-f]{64}", doc["identity"]):
            fail()
        try:
            validate_stored(doc["config"])
        except Error:
            fail()
        return doc

    def save(self, doc):
        write_json(self.directory(doc["id"]) / "definition.json", doc)

    def ids(self):
        return sorted(p.name for p in self.agents.iterdir() if (p / "definition.json").exists())
