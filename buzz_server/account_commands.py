"""Fixed system-tool boundary. Project Python is never executed as root.

This module does not accept command strings, environment overrides, tool paths
or service names supplied by the target. Every effect revalidates the private
operator binding immediately before invoking an ordinary explicit sudo command.
"""
import subprocess
from pathlib import Path

from .errors import Error
from .account_process import run


CLEAN_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}


class Commands:
    def __init__(self, bindings, binding):
        self.bindings = bindings
        self.binding = binding

    def _run(self, argv, *, input=None, stdin=None, terminal=False, timeout=30, allow_missing=False):
        self.bindings.revalidate(self.binding)
        try:
            result = run(argv, env=CLEAN_ENV.copy(), cwd="/", umask=0o077,
                                    input=input, stdin=stdin, capture_output=not terminal, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            raise Error("account_command_failed", "The authorized system command failed or timed out. State is retained; inspect locally and retry.") from None
        if result.returncode and not (allow_missing and b"\nLoadState=not-found\n" in b"\n" + result.stdout):
            raise Error("account_command_failed", "The authorized system command did not succeed. State is retained; inspect locally and retry.")
        return result.stdout

    def show(self, properties):
        # Read-only access to the trusted system manager; no caller D-Bus address.
        if not properties or any(not p.isalpha() for p in properties):
            raise ValueError("invalid property selection")
        return self._run(["/usr/bin/systemctl", "--system", "show", self.binding.service,
                          "--all", "--property=" + ",".join(properties)], allow_missing=True)

    def control(self, operation):
        if operation not in {"start", "stop", "enable", "disable", "reset-failed"}:
            raise ValueError("invalid system operation")
        return self._run(["/usr/bin/sudo", "--", "/usr/bin/systemctl", "--system",
                          operation, self.binding.service])

    def reload(self):
        return self._run(["/usr/bin/sudo", "--", "/usr/bin/systemctl", "--system", "daemon-reload"])

    @property
    def unit_path(self):
        return Path("/etc/systemd/system") / self.binding.service

    @property
    def stage_path(self):
        return self.unit_path.with_name("." + self.binding.service + ".stage")

    def stage_unit(self, source):
        # Only the private operator-side unit source reserved for this binding.
        expected = self.bindings.root / (self.binding.id + ".unit")
        if Path(source) != expected:
            raise ValueError("unbound unit source")
        return self._run(["/usr/bin/sudo", "--", "/usr/bin/install", "-T", "-o", "0", "-g", "0",
                          "-m", "0644", "--", str(expected), str(self.stage_path)])

    def publish_unit(self):
        # link(2) refuses an existing destination and publishes complete bytes.
        return self._run(["/usr/bin/sudo", "--", "/usr/bin/ln", "-T", "--",
                          str(self.stage_path), str(self.unit_path)])

    def unlink_unit(self, *, stage=False):
        path = self.stage_path if stage else self.unit_path
        return self._run(["/usr/bin/sudo", "--", "/usr/bin/rm", "--", str(path)])

    def target(self, program, *, input=None, stdin=None, terminal=False):
        # The program is an operator-selected fixed payload, never a target
        # response. Python receives no operator HOME, PYTHONPATH or credentials.
        # env -i runs after sudo's UID transition and pins the target environment.
        if not isinstance(program, str):
            raise TypeError("fixed target program required")
        return self._run(["/usr/bin/sudo", "--user=#" + str(self.binding.uid),
                          "--group=#" + str(self.binding.gid), "--", "/usr/bin/env", "-i",
                          "PATH=/usr/bin:/bin", "LANG=C.UTF-8", "HOME=" + self.binding.home,
                          "/usr/bin/python3", "-I", "-c", program],
                         input=input, stdin=stdin, terminal=terminal, timeout=None if terminal else 60)
