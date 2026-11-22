"""Installed entry point, invoked with Python isolated mode."""
from pathlib import Path
import os
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

if len(sys.argv) == 3 and sys.argv[1] == "_release_probe":
    from buzz_server.release import location, probe
    if os.getuid() != 0 and location() == Path(sys.argv[2]):
        probe(Path(sys.argv[2]))
    sys.exit(0)
elif len(sys.argv) > 1 and sys.argv[1] == "_worker":
    from buzz_server.storage import Store
    from buzz_server.runtime import worker
    try:
        if os.getuid() == 0 or len(sys.argv) != 5 or sys.argv[2] != "--state-dir":
            sys.exit(1)
        sys.exit(worker(Store(sys.argv[3]), sys.argv[4]))
    except Exception:
        sys.exit(1)
else:
    from buzz_server.cli import main
    sys.exit(main())
