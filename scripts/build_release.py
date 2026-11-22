#!/usr/bin/env python3
"""Build a deterministic zipapp and checksum offline. Never publishes."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from buzz_server.release import build

if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--version', required=True, help='Numeric X.Y.Z selected by the maintainer')
    p.add_argument('--output', type=Path, required=True, help='Output file, normally buzz-server.pyz')
    args = p.parse_args()
    build(Path(__file__).resolve().parents[1], args.output, args.version)
