"""Real PTY handoff using two synthetic zipapps and an offline transport fixture."""
import errno
import hashlib
import json
import os
from pathlib import Path
import pty
import select
import shutil
import subprocess
import tempfile
import time
import unittest

from buzz_server import release


class UpdatePtyTests(unittest.TestCase):
    def test_two_build_precommand_handoff(self):
        with tempfile.TemporaryDirectory(prefix='buzz-pty-') as temp:
            base = Path(temp)
            source = base / 'source'
            source.mkdir()
            repo = Path(__file__).resolve().parents[1]
            shutil.copytree(repo / 'buzz_server', source / 'buzz_server')
            shutil.copy(repo / 'entry.py', source / 'entry.py')
            # Fixture changes only: no authority/config inputs in product code.
            with (source / 'buzz_server/release.py').open('a') as f:
                f.write('\ndef fetch(args, target, limit):\n'
                        f'    root = Path({str(base)!r})\n'
                        "    with (root / 'requests').open('a') as log: log.write(args + '\\n')\n"
                        "    target.write_bytes((root / ('metadata' if 'releases/latest' in args else 'download')).read_bytes())\n")
            with (source / 'buzz_server/cli.py').open('a') as f:
                f.write('\n_original_execute = execute\ndef execute(args):\n'
                        f'    with open({str(base / "dispatch")!r}, "a") as f:\n'
                        "        f.write(json.dumps([__version__, sys.argv[1:]]) + '\\n')\n"
                        "    return {'agents': []}\n")
            a, b = base / 'a.pyz', base / 'b.pyz'
            release.build(source, a, '1.0.0')
            release.build(source, b, '1.1.0')
            metadata = dict(tag_name='v1.1.0', draft=False, prerelease=False, assets=[dict(
                name=release.ASSET, id=71, size=b.stat().st_size,
                digest='sha256:' + hashlib.sha256(b.read_bytes()).hexdigest())])
            (base / 'metadata').write_text(json.dumps(metadata))
            for scenario in ('accept', 'decline', 'failure', 'json', 'noTTY'):
                with self.subTest(scenario=scenario):
                    prefix = base / scenario
                    state, units = base / 'state', base / 'units'
                    release.install(a, prefix, state, units)
                    (base / 'download').write_bytes(b'bad' if scenario == 'failure' else b.read_bytes())
                    (base / 'dispatch').unlink(missing_ok=True)
                    (base / 'requests').unlink(missing_ok=True)
                    argv = ['--state-dir', str(state), '--unit-dir', str(units)]
                    if scenario == 'json':
                        argv += ['--json']
                    argv += ['list']
                    command = [str(prefix / 'bin/buzz-server'), *argv]
                    if scenario == 'noTTY':
                        p = subprocess.run(command, capture_output=True, timeout=8)
                        output, code = p.stdout + p.stderr, p.returncode
                    else:
                        master, slave = pty.openpty()
                        p = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave)
                        os.close(slave)
                        output = b''
                        sent = False
                        deadline = time.monotonic() + 8
                        try:
                            while time.monotonic() < deadline:
                                if select.select([master], [], [], .1)[0]:
                                    try:
                                        chunk = os.read(master, 65536)
                                    except OSError as e:
                                        if e.errno != errno.EIO:
                                            raise
                                        break
                                    if not chunk:
                                        break
                                    output += chunk
                                    if b'[y/N]' in output and not sent:
                                        # Offer precedes the original command's observable side effect.
                                        self.assertFalse((base / 'dispatch').exists())
                                        os.write(master, b'no\n' if scenario == 'decline' else b'yes\n')
                                        sent = True
                            code = p.wait(timeout=1)
                        finally:
                            if p.poll() is None:
                                p.kill()
                                p.wait(timeout=2)
                            os.close(master)
                    self.assertEqual(code, 0, output)
                    expected = '1.1.0' if scenario == 'accept' else '1.0.0'
                    self.assertEqual([json.loads(x) for x in (base / 'dispatch').read_text().splitlines()], [[expected, argv]])
                    self.assertEqual(release.manifest(prefix)['version'], expected)
                    self.assertEqual(output.count(b'[y/N]'), 0 if scenario in ('json', 'noTTY') else 1)
                    warning = b'Update not applied; continuing with Buzz Server 1.0.0. Run buzz-server update to retry.'
                    self.assertEqual(output.count(warning), int(scenario == 'failure'))
                    if scenario == 'failure':
                        self.assertLess(output.index(warning), output.index(b'No managed agents.'))
                    if scenario in ('json', 'noTTY'):
                        self.assertFalse((base / 'requests').exists())
                    else:
                        self.assertEqual(len((base / 'requests').read_text().splitlines()), 1 if scenario == 'decline' else 2)
