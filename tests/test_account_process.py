"""Exercise the clean, bounded transport with local synthetic child processes."""
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest

from buzz_server.account_process import run


class AccountProcessTests(unittest.TestCase):
    def test_private_request_uses_stdin_and_fixed_environment(self):
        program = "import json,os,sys; print(json.dumps({'stdin':sys.stdin.read(),'cwd':os.getcwd(),'override':os.environ.get('SYNTHETIC_OVERRIDE')}))"
        argv = ["/usr/bin/python3", "-I", "-c", program]
        result = run(argv, env={"PATH": "/usr/bin:/bin"}, cwd="/", umask=0o077,
                     input=b"synthetic private request", timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("synthetic private request", " ".join(argv))
        self.assertEqual(json.loads(result.stdout), {"stdin": "synthetic private request", "cwd": "/", "override": None})

    def test_output_bound_and_timeout_reap_child_and_restore_handlers(self):
        original = (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM))
        with self.assertRaises(OSError):
            run(["/usr/bin/python3", "-I", "-c", "print('x'*131072)"],
                env={"PATH": "/usr/bin:/bin"}, cwd="/", umask=0o077, timeout=5)
        with tempfile.TemporaryDirectory(prefix="buzz-command-") as temp:
            marker = Path(temp) / "pid"
            program = "import os,sys,time; open(sys.argv[1],'w').write(str(os.getpid())); time.sleep(30)"
            with self.assertRaises(subprocess.TimeoutExpired):
                run(["/usr/bin/python3", "-I", "-c", program, str(marker)],
                    env={"PATH": "/usr/bin:/bin"}, cwd="/", umask=0o077, timeout=0.3)
            with self.assertRaises(ProcessLookupError):
                os.kill(int(marker.read_text()), 0)
        self.assertEqual((signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)), original)


if __name__ == "__main__":
    unittest.main()
