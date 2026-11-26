"""Synthetic account/authority tests: never invoke sudo or a host manager."""
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, PropertyMock, patch

from buzz_server.accounts import Bindings
from buzz_server.account_commands import CLEAN_ENV, Commands
from buzz_server.account_supervisor import AccountSystemd
from buzz_server.errors import Error
from buzz_server.storage import Store, atomic


class DeploymentCommandsTests(unittest.TestCase):
    """Run the deployment guards against real files with synthetic root ownership."""

    def setUp(self):
        workspace = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="buzz-root-files-")))
        operator = Store(workspace / "operator")
        self.source_dir = operator.root / ".deployment-fixture"
        self.source_dir.mkdir(mode=0o700)
        self.source = self.source_dir / "prompt"
        atomic(self.source, b"synthetic approved")
        self.base = workspace / "system"
        self.base.mkdir(mode=0o755)
        self.root = self.base / "buzz-server" / "fixture.service"
        self.unit = self.base / "fixture.service"
        self.target = self.root / "prompt-0" / "prompt"
        self.staged = self.target.with_name(".prompt.stage")
        binding = SimpleNamespace(id="fixture", service=self.unit.name, uid=20001, gid=20001,
                                  home="/home/fixture", data="/home/fixture/data")
        self.commands = Commands(SimpleNamespace(root=operator.root, revalidate=Mock()), binding)
        self.enterContext(patch.object(Commands, "deployment_root", new_callable=PropertyMock, return_value=self.root))
        self.enterContext(patch.object(Commands, "unit_path", new_callable=PropertyMock, return_value=self.unit))
        original = Path.lstat
        self.owners = {}

        def lstat(path):
            info = original(path)
            if path in self.base.parents:
                return SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0, st_gid=0)
            if path == self.base or self.base in path.parents:
                uid, gid = self.owners.get(path, (0, 0))
                return SimpleNamespace(st_mode=info.st_mode, st_uid=uid, st_gid=gid, st_nlink=info.st_nlink)
            return info

        self.enterContext(patch.object(Path, "lstat", lstat))
        self.run = self.enterContext(patch("buzz_server.account_commands.run", side_effect=self.run_fixed))

    def run_fixed(self, argv, **kwargs):
        self.assertEqual(argv[:3], ["/usr/bin/sudo", "--non-interactive", "--"])
        tool, *args, destination = argv[3:]
        path = Path(destination)
        self.assertTrue(self.base in path.parents or path == self.base)
        result = 0
        if tool == "/usr/bin/install":
            mode = int(args[args.index("-m") + 1], 8)
            if args[0] == "-d":
                path.mkdir(mode=mode)
            else:
                shutil.copyfile(args[-1], path)
                path.chmod(mode)
        elif tool == "/usr/bin/cmp":
            source = kwargs["input"] if args[-1] == "-" else Path(args[-1]).read_bytes()
            result = int(source != path.read_bytes())
        elif tool == "/usr/bin/mv":
            Path(args[-1]).replace(path)
        elif tool == "/usr/bin/rm":
            path.unlink()
        else:
            self.assertEqual(tool, "/usr/bin/sync")
        return SimpleNamespace(returncode=result, stdout=b"", stderr=b"")

    def stage(self):
        self.commands.stage_deployment(0, {"prompt": self.source}, prompt=True)

    def test_stage_uses_fixed_tools_and_compares_before_publication(self):
        self.stage()
        self.assertEqual(self.target.read_bytes(), b"synthetic approved")
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)
        self.assertFalse(self.staged.exists())
        effects = [call.args[0][3:] for call in self.run.call_args_list]
        directories = [self.root.parent, self.root, self.target.parent]
        self.assertEqual(effects, [
            *[["/usr/bin/install", "-d", "-o", "0", "-g", "0", "-m", "0755", "--", str(p)] for p in directories],
            ["/usr/bin/install", "-T", "-o", "0", "-g", "0", "-m", "0600", "--", str(self.source), str(self.staged)],
            ["/usr/bin/cmp", "--silent", "--", str(self.source), str(self.staged)],
            ["/usr/bin/mv", "-T", "--", str(self.staged), str(self.target)],
            ["/usr/bin/sync", "-f", "--", str(self.target.parent)],
        ])
        self.assertTrue(self.commands.prompt_matches(0, b"synthetic approved"))
        self.assertFalse(self.commands.prompt_matches(0, b"synthetic edited"))
        self.assertEqual(self.run.call_args.args[0][3:], ["/usr/bin/cmp", "--silent", "--", "-", str(self.target)])
        self.assertEqual(self.run.call_args.kwargs["input"], b"synthetic edited")

    def test_slot_tampering_refuses_before_system_effects(self):
        self.stage()
        self.run.reset_mock()
        self.target.chmod(0o644)
        with self.assertRaises(Error):
            self.stage()
        self.target.chmod(0o600)
        self.owners[self.target] = (0, 1000)
        with self.assertRaises(Error):
            self.commands.prompt_matches(0, b"synthetic approved")
        self.owners.clear()
        hardlink = self.target.with_name("extra-link")
        hardlink.hardlink_to(self.target)
        with self.assertRaises(Error):
            self.stage()
        hardlink.unlink()
        self.staged.symlink_to(self.target)
        with self.assertRaises(Error):
            self.stage()
        self.staged.unlink()
        self.target.parent.chmod(0o775)
        with self.assertRaises(Error):
            self.stage()
        self.run.assert_not_called()

    def test_replacement_checks_real_unit_content_and_rejects_a_symlink(self):
        supervisor = AccountSystemd(self.commands)
        self.unit.write_bytes(supervisor.content())
        self.unit.chmod(0o644)
        self.assertTrue(supervisor.replacement_installed())
        self.unit.write_bytes(supervisor.content() + b"\n")
        self.assertFalse(supervisor.replacement_installed())
        other = self.unit.with_name("foreign.service")
        self.unit.rename(other)
        self.unit.symlink_to(other)
        with self.assertRaises(Error):
            supervisor.replacement_installed()
        self.run.assert_not_called()


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="buzz-account-")
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "state")
        self.bindings = Bindings(self.store)
        self.entry = SimpleNamespace(pw_name="fixture", pw_uid=os.getuid() + 10000,
                                     pw_gid=os.getgid() + 10000, pw_dir="/home/fixture")
        self.lookup = patch("buzz_server.accounts.pwd.getpwnam", return_value=self.entry).start()
        self.reverse = patch("buzz_server.accounts.pwd.getpwuid", return_value=self.entry).start()
        self.addCleanup(patch.stopall)
        original = Path.lstat

        def layout(path):
            if str(path) in ("/home/fixture", "/home", "/"):
                return SimpleNamespace(st_mode=stat.S_IFDIR | (0o700 if str(path) == "/home/fixture" else 0o755),
                                       st_uid=self.entry.pw_uid if str(path) == "/home/fixture" else 0)
            return original(path)

        patch("buzz_server.accounts.Path.lstat", layout).start()
        self.run = patch("buzz_server.account_commands.run",
                         return_value=SimpleNamespace(returncode=0, stdout=b"synthetic")).start()

    def reserve(self):
        return self.bindings.reserve("assistant", "fixture")

    def test_private_binding_is_deterministic_and_reserves_id(self):
        binding = self.reserve()
        self.assertEqual(self.bindings.load("assistant"), binding)
        self.assertRegex(binding.service, r"^buzz-server-account-[0-9a-f]{24}-assistant.service$")
        self.assertTrue(binding.data.startswith("/home/fixture/.local/share/buzz-server-managed/"))
        self.assertEqual(self.bindings.path("assistant").stat().st_mode & 0o777, 0o600)
        with self.assertRaises(Error):
            self.reserve()
        self.run.assert_not_called()

    def test_every_command_revalidates_account_before_effect(self):
        binding = self.reserve()
        command = Commands(self.bindings, binding)
        self.entry = SimpleNamespace(**{**self.entry.__dict__, "pw_uid": self.entry.pw_uid + 1})
        self.lookup.return_value = self.reverse.return_value = self.entry
        for effect in (lambda: command.control("stop"), command.reload,
                       lambda: command.show(["User"]), lambda: command.target("pass")):
            with self.assertRaises(Error):
                effect()
        self.run.assert_not_called()

    def test_root_operator_remap_and_missing_account_refuse(self):
        for uid in (0, os.getuid()):
            self.lookup.return_value = SimpleNamespace(**{**self.entry.__dict__, "pw_uid": uid})
            with self.assertRaises(Error):
                self.reserve()
        self.lookup.side_effect = KeyError
        with self.assertRaises(Error):
            self.reserve()
        self.run.assert_not_called()

    def test_corrupt_binding_cannot_authorize_effect(self):
        binding = self.reserve()
        path = self.bindings.path(binding.id)
        original = binding.document()
        for field, value in (("service", "sshd.service"), ("data", "/home/unrelated"),
                             ("uid", True), ("home", "/root"), ("version", True)):
            atomic(path, json.dumps({**original, field: value}).encode())
            with self.assertRaises(Error):
                Commands(self.bindings, binding).control("start")
        path.unlink()
        with self.assertRaises(Error):
            Commands(self.bindings, binding).control("stop")
        self.run.assert_not_called()

    def test_untrusted_operator_state_ancestor_cannot_authorize_root_file_access(self):
        binding = self.reserve()
        original = Path.lstat
        parent = self.store.root.parent
        # Keep the bad directory on the actual binding path, regardless of
        # where TMPDIR lives. Model trusted, traversable ancestors so a private
        # host home cannot shield the deliberately writable fixture directory.
        ancestors = set(parent.parents)
        for owner, mode in ((self.entry.pw_uid, 0o700), (os.getuid(), 0o777)):
            def replaced(path):
                if path == parent:
                    return SimpleNamespace(st_uid=owner, st_mode=stat.S_IFDIR | mode)
                if path in ancestors:
                    return SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o755)
                return original(path)
            with self.subTest(owner=owner, mode=mode), patch("buzz_server.accounts.Path.lstat", replaced):
                with self.assertRaises(Error):
                    Commands(self.bindings, binding).stage_unit(self.bindings.root / "assistant.unit")
        self.run.assert_not_called()

    def test_fixed_root_argv_and_clean_environment(self):
        binding = self.reserve()
        command = Commands(self.bindings, binding)
        command.control("stop")
        args, kwargs = self.run.call_args
        self.assertEqual(args[0], ["/usr/bin/sudo", "--non-interactive", "--", "/usr/bin/systemctl", "--system", "stop", binding.service])
        self.assertEqual(kwargs["env"], CLEAN_ENV)
        self.assertEqual(kwargs["cwd"], "/")
        self.assertEqual(kwargs["umask"], 0o077)
        for operation in ("edit", "link", "start sshd", "daemon-reexec"):
            with self.assertRaises(ValueError):
                command.control(operation)
        self.assertEqual(self.run.call_count, 1)

    def test_separate_authentication_wait_then_bounded_effects(self):
        binding = self.reserve()
        command = Commands(self.bindings, binding, interactive=True)
        with patch("buzz_server.account_commands.sys.stdin.isatty", return_value=True), \
             patch("buzz_server.account_commands.sys.stdout.isatty", return_value=True), \
             patch("buzz_server.account_commands.sys.stderr.isatty", return_value=True):
            command.authenticate()
            command.target("pass", input=b"private fixture")
            command.control("stop")
        self.assertEqual(self.run.call_count, 3)
        auth, target, control = self.run.call_args_list
        self.assertEqual(auth.args[0], ["/usr/bin/sudo", "--validate"])
        self.assertIsNone(auth.kwargs["timeout"])
        self.assertFalse(auth.kwargs["capture_output"])
        self.assertEqual(auth.kwargs["stdin"], subprocess.DEVNULL)
        self.assertIsNone(auth.kwargs["input"])
        self.assertEqual(target.kwargs["timeout"], 60)
        self.assertEqual(target.kwargs["input"], b"private fixture")
        self.assertTrue(target.kwargs["capture_output"])
        self.assertEqual(control.kwargs["timeout"], 30)
        self.assertEqual(control.kwargs["stdin"], subprocess.DEVNULL)
        for call in (auth, target, control):
            self.assertEqual(call.kwargs["env"], CLEAN_ENV)
            self.assertEqual(call.kwargs["cwd"], "/")
            self.assertEqual(call.kwargs["umask"], 0o077)

    def test_failed_authentication_never_runs_effect(self):
        binding = self.reserve()
        for failure in (SimpleNamespace(returncode=1, stdout=b""), OSError(), KeyboardInterrupt()):
            self.run.reset_mock()
            self.run.side_effect = [failure]
            with patch("buzz_server.account_commands.sys.stdin.isatty", return_value=True), \
                 patch("buzz_server.account_commands.sys.stdout.isatty", return_value=True), \
                 patch("buzz_server.account_commands.sys.stderr.isatty", return_value=True):
                with self.assertRaises((Error, KeyboardInterrupt)):
                    Commands(self.bindings, binding, interactive=True).authenticate()
            self.assertEqual(self.run.call_count, 1)
            self.assertEqual(self.run.call_args.args[0], ["/usr/bin/sudo", "--validate"])

    def test_binding_revalidated_after_authentication_wait(self):
        binding = self.reserve()
        def remap(*args, **kwargs):
            self.lookup.return_value = SimpleNamespace(**{**self.entry.__dict__, "pw_uid": self.entry.pw_uid + 1})
            return SimpleNamespace(returncode=0, stdout=b"")
        self.run.side_effect = remap
        with patch("buzz_server.account_commands.sys.stdin.isatty", return_value=True), \
             patch("buzz_server.account_commands.sys.stdout.isatty", return_value=True), \
             patch("buzz_server.account_commands.sys.stderr.isatty", return_value=True):
            with self.assertRaises(Error):
                Commands(self.bindings, binding, interactive=True).authenticate()
        self.assertEqual(self.run.call_count, 1)

    def test_no_auth_prompt_without_all_terminals_or_explicit_permission(self):
        binding = self.reserve()
        for interactive, terminals in ((False, (True, True, True)), (True, (False, True, True)),
                                       (True, (True, False, True)), (True, (True, True, False))):
            self.run.reset_mock()
            with patch("buzz_server.account_commands.sys.stdin.isatty", return_value=terminals[0]), \
                 patch("buzz_server.account_commands.sys.stdout.isatty", return_value=terminals[1]), \
                 patch("buzz_server.account_commands.sys.stderr.isatty", return_value=terminals[2]):
                command = Commands(self.bindings, binding, interactive=interactive)
                command.authenticate()
                command.control("stop")
            self.assertEqual(self.run.call_count, 1)
            self.assertEqual(self.run.call_args.args[0][1], "--non-interactive")

    def test_read_only_show_skips_auth_and_terminal_target_keeps_forwarding(self):
        binding = self.reserve()
        command = Commands(self.bindings, binding, interactive=True)
        with patch("buzz_server.account_commands.sys.stdin.isatty", return_value=True), \
             patch("buzz_server.account_commands.sys.stdout.isatty", return_value=True), \
             patch("buzz_server.account_commands.sys.stderr.isatty", return_value=True):
            command.show(["User"])
            self.assertEqual(self.run.call_count, 1)
            self.assertEqual(self.run.call_args.args[0][0], "/usr/bin/systemctl")
            self.assertEqual(self.run.call_args.kwargs["timeout"], 30)
            command.authenticate()
            command.target("pass", terminal=True)
        self.assertEqual(self.run.call_count, 3)
        self.assertIsNone(self.run.call_args.kwargs["timeout"])
        self.assertFalse(self.run.call_args.kwargs["capture_output"])
        self.assertIsNone(self.run.call_args.kwargs["stdin"])

    def test_target_python_runs_only_after_numeric_uid_transition(self):
        binding = self.reserve()
        Commands(self.bindings, binding).target("pass", input=b"synthetic")
        args, kwargs = self.run.call_args
        self.assertEqual(args[0], ["/usr/bin/sudo", "--non-interactive", "--user=#" + str(binding.uid),
                                  "--group=#" + str(binding.gid), "--", "/usr/bin/env", "-i",
                                  "PATH=/usr/bin:/bin", "LANG=C.UTF-8", "HOME=/home/fixture",
                                  "/usr/bin/python3", "-I", "-c", "pass"])
        self.assertEqual(kwargs["input"], b"synthetic")
        self.assertEqual(kwargs["env"], CLEAN_ENV)


if __name__ == "__main__":
    unittest.main()
