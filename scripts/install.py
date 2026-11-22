#!/usr/bin/env python3
"""Offline, prefix-local install. No pip, network, root or host toolchain."""
import argparse
import os
from pathlib import Path
import shlex
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from buzz_server.storage import atomic, private_dir
from buzz_server.errors import Error


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--prefix", required=True, type=Path, help="Private installation prefix (mode 0700)")
    args = p.parse_args()
    if os.getuid() == 0:
        p.error("install as an unprivileged account")
    prefix = private_dir(args.prefix.absolute())
    lib = private_dir(prefix / "lib/buzz-server")
    package = private_dir(lib / "buzz_server")
    bindir = private_dir(prefix / "bin")
    source = Path(__file__).resolve().parent.parent
    for path in (source / "buzz_server").glob("*.py"):
        atomic(package / path.name, path.read_bytes())
    atomic(lib / "entry.py", (source / "entry.py").read_bytes())
    # The interpreter and installed package location are pinned for systemd as well.
    executable = bindir / "buzz-server"
    content = f"#!/bin/sh\nexec {shlex.quote(sys.executable)} -I {shlex.quote(str(lib / 'entry.py'))} \"$@\"\n"
    atomic(executable, content.encode())
    executable.chmod(0o700)
    print(f"Installed {executable}")


if __name__ == "__main__":
    try:
        main()
    except (Error, OSError):
        print("Install failed: use a private writable prefix, owned by this user, without symlink components.", file=sys.stderr)
        sys.exit(1)
