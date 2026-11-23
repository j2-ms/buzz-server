"""Fixed, ephemeral target-UID operations. Not part of the worker archive.

The operator embeds this source in an isolated target-UID Python invocation.
No part of this module runs under root and no system-service control is here.
Only BINDING's private subtree is touched. Errors return static messages through
the operator, never file content or diagnostic strings from the target account.
"""
import hashlib
import json
import os
from pathlib import Path
import pwd
import stat
import struct
import sys


def require(value):
    if not value:
        raise ValueError("unsafe target layout or request")


def directory(path):
    # Called only after assert_identity, never as the operator or root.
    path = Path(path)
    home = Path(BINDING["home"])
    require(path == home or home in path.parents)
    relative = path.relative_to(home)
    current = home
    for component in ("", *relative.parts):
        if component:
            current = current / component
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            pass
        s = current.lstat()
        mask = 0o022 if current in (home / ".local", home / ".local/share") else 0o077
        require(stat.S_ISDIR(s.st_mode) and s.st_uid == os.getuid() and not stat.S_IMODE(s.st_mode) & mask)
    return path


def assert_identity():
    entry = pwd.getpwnam(BINDING["account"])
    require(os.getuid() == os.geteuid() == BINDING["uid"] != 0)
    require(os.getgid() == os.getegid() == BINDING["gid"])
    require((entry.pw_uid, entry.pw_gid, entry.pw_dir) ==
            (BINDING["uid"], BINDING["gid"], BINDING["home"]))
    require(str(Path(BINDING["home"]) / ".local/share/buzz-server-managed" /
                BINDING["service"].removeprefix("buzz-server-account-").removesuffix("-" + BINDING["id"] + ".service") /
                BINDING["id"]) == BINDING["data"])
    directory(BINDING["home"])


def exact(stream, size):
    data = stream.read(size)
    require(len(data) == size)
    return data


def install():
    """Stream an explicit file allowlist, then atomically publish each file.

    Interrupted stages are private fixed files and retried in place. No archive
    extraction, arbitrary paths, links, chmod or chown inputs are accepted.
    """
    root = directory(BINDING["data"])
    state = directory(root / "state")
    tools = directory(state / "tools/bin")
    stream = sys.stdin.buffer
    header_size = struct.unpack("!I", exact(stream, 4))[0]
    require(0 < header_size <= 65536)
    header = json.loads(exact(stream, header_size))
    require(set(header) == {"worker.pyz", "buzz", "buzz-acp", "goose"})
    for name in ("worker.pyz", "buzz", "buzz-acp", "goose"):
        info = header[name]
        require(set(info) == {"size", "sha256"} and type(info["size"]) is int and 0 < info["size"] <= 256 * 1024 * 1024)
        require(isinstance(info["sha256"], str) and len(info["sha256"]) == 64)
        destination = root / name if name == "worker.pyz" else tools / name
        stage = destination.with_name("." + name + ".stage")
        # O_NOFOLLOW and O_NONBLOCK reject target-created link/FIFO stages.
        fd = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        digest = hashlib.sha256()
        with os.fdopen(fd, "wb") as out:
            s = os.fstat(out.fileno())
            require(stat.S_ISREG(s.st_mode) and s.st_uid == os.getuid() and s.st_nlink == 1)
            os.ftruncate(out.fileno(), 0)
            remaining = info["size"]
            while remaining:
                block = exact(stream, min(remaining, 65536))
                remaining -= len(block)
                digest.update(block)
                out.write(block)
            out.flush()
            os.fsync(out.fileno())
            require(digest.hexdigest() == info["sha256"])
            os.fchmod(out.fileno(), 0o600 if name == "worker.pyz" else 0o700)
        # Never follow a destination; replace replaces the directory entry.
        if destination.exists() or destination.is_symlink():
            s = destination.lstat()
            require(stat.S_ISREG(s.st_mode) and s.st_uid == os.getuid() and s.st_nlink == 1)
        os.replace(stage, destination)
    require(stream.read(1) == b"")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def modules():
    sys.path.insert(0, BINDING["data"] + "/worker.pyz")
    from buzz_server.storage import Store
    return Store(Path(BINDING["data"]) / "state")


def apply():
    from buzz_server.storage import atomic
    from buzz_server.identity import generate
    from buzz_server.runtime import prepare
    from buzz_server.config import validate, validate_relay_transport
    store = modules()
    ident = BINDING["id"]
    root = store.directory(ident)
    config = REQUEST["config"]
    config["prompt_file"] = str(root / "user-prompt.txt")
    config["harness"]["command"] = str(store.root / "tools/bin/goose")
    validate(config)
    validate_relay_transport(config["relay_url"])
    with store.lock():
        if (root / "definition.json").exists():
            doc = store.load(ident)
            require(doc["desired"] == "stopped")
            doc["config"] = config
            doc["revision"] = REQUEST["revision"]
        else:
            require(REQUEST["revision"] == 1)
            directory(root)
            # Retry after interruption preserves a key written before definition.
            from buzz_server.storage import read_private
            from buzz_server.identity import public
            key_path = root / "identity.key"
            if key_path.exists() or key_path.is_symlink():
                key = read_private(key_path, 64).decode("ascii")
                identity = public(key)
            else:
                key, identity = generate()
                atomic(key_path, key.encode())
            doc = {"version": 1, "id": ident, "identity": identity,
                   "config": config, "desired": "stopped", "revision": 1}
        if REQUEST.get("identity") is not None:
            require(doc["identity"] == REQUEST["identity"])
        atomic(root / "user-prompt.txt", REQUEST["prompt"].encode("utf-8"))
        prepare(store, doc, snapshot=False)
        store.save(doc)
    return {"identity": doc["identity"], "revision": doc["revision"]}


def operation():
    assert_identity()
    if OPERATION == "install":
        install()
        return {"installed": True}
    # Validate layout again before importing target-owned Python, which is
    # permitted only at the already established target UID.
    directory(BINDING["data"])
    store = modules()
    if OPERATION == "apply":
        return apply()
    with store.lock():
        if OPERATION == "delete" and not (store.directory(BINDING["id"]) / "definition.json").exists():
            return {"applied": True}
        doc = store.load(BINDING["id"])
        require(doc["identity"] == REQUEST["identity"])
        if OPERATION == "desired":
            require(REQUEST["desired"] in ("running", "stopped"))
            doc["desired"] = REQUEST["desired"]
            store.save(doc)
        elif OPERATION == "status":
            return {"identity": doc["identity"], "revision": doc["revision"],
                    "desired": doc["desired"]}
        elif OPERATION == "inference":
            require(doc["desired"] == "stopped")
            from buzz_server.runtime import configure_goose
            return configure_goose(store, doc)
        elif OPERATION == "delete":
            (store.directory(BINDING["id"]) / "definition.json").unlink()
        else:
            raise ValueError("unsupported operation")
    return {"applied": True}


def main():
    try:
        result = operation()
        if OPERATION != "inference":
            print(json.dumps(result, sort_keys=True))
    except Exception:
        sys.exit(1)
