#!/usr/bin/env python3
"""Real CLI + private systemd + installed buzz/buzz-acp, synthetic transport/ACP.

Run only on an authorized disposable Linux environment. Does not contact the
host's existing service manager, change accounts, or use existing user data.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

SOURCE = Path(__file__).resolve().parent.parent


class Blocked(Exception):
    pass


def wait_until(predicate, processes=(), timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        if any(p.poll() is not None for p in processes):
            raise Blocked("A disposable prerequisite process exited; inspect the retained private prerequisite logs.")
        time.sleep(0.1)
    raise Blocked("Disposable prerequisite did not become ready within 15 seconds.")


class Acceptance:
    def __init__(self, root, releases=False):
        self.root = root
        self.releases = releases
        self.processes = []
        self.env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": str(root / "home"),
                    "XDG_RUNTIME_DIR": str(root / "run"), "XDG_CONFIG_HOME": str(root / "config"),
                    "XDG_DATA_HOME": str(root / "data"), "SYSTEMD_UNIT_PATH": str(root / "config/systemd/user"),
                    "DBUS_SESSION_BUS_ADDRESS": "unix:path=" + str(root / "run/bus")}
        for name in ("home", "run", "config", "data", "config/systemd", "config/systemd/user", "fixture"):
            (root / name).mkdir(mode=0o700, parents=True, exist_ok=True)
        self.state = root / "data/buzz-server"
        self.binary = root / "install/bin/buzz-server"
        self.transcript = []

    def launch(self, command, name):
        log = (self.root / (name + ".log")).open("wb")
        try:
            p = subprocess.Popen(command, env=self.env, cwd=self.root, stdout=log, stderr=log, start_new_session=True)
        finally:
            log.close()
        self.processes.append(p)
        return p

    def systemctl(self, *args, check=True):
        return subprocess.run(["/usr/bin/systemctl", "--user", *args], env=self.env, capture_output=True, timeout=20, check=check)

    def manager(self):
        return self.launch(["/usr/lib/systemd/systemd", "--user", "--unit=default.target"], "systemd")

    def exit_manager(self, manager):
        # Only our private unit search path is visible; it deliberately omits
        # the distro's exit helper. Call this after verifying/stopping workers.
        self.systemctl("--force", "exit")
        manager.wait(timeout=15)

    def run(self, *args, ok=True, env=None):
        p = subprocess.run([str(self.binary), "--json", *args], env=env or self.env, capture_output=True, text=True, timeout=45)
        if (p.returncode == 0) != ok:
            raise AssertionError(f"CLI expectation failed: {' '.join(args[:2])}: exit={p.returncode}; {p.stderr}")
        result = json.loads(p.stdout if ok else p.stderr)
        self.transcript.append({"command": list(args), "exit": p.returncode, "response": result})
        print(f"PASS {' '.join(args[:2])} exit={p.returncode}", flush=True)
        return result["data"] if ok else result["error"]

    def config(self, port):
        fixture = self.root / "fixture"
        goose = fixture / "goose"
        shutil.copyfile(SOURCE / "tests/fixture_goose.py", goose)
        goose.chmod(0o700)
        (fixture / "prompt.txt").write_text("You are a disposable synthetic test agent.\n")
        c = {"relay_url": f"ws://127.0.0.1:{port}", "owner": "ab" * 32,
             "profile": {"name": "Disposable", "about": "Synthetic lifecycle only", "avatar": ""},
             "prompt_file": str(fixture / "prompt.txt"), "harness": {"kind": "goose", "command": str(goose)}, "provider": {"kind": "goose-managed"}}
        path = fixture / "config.json"
        path.write_text(json.dumps(c))
        return c, path

    def lifecycle(self):
        # Install is fully local and uses only the existing Python interpreter.
        if self.releases:
            self.artifacts = []
            for number in ("1.0.0", "1.1.0"):
                output = self.root / ("buzz-server-v" + number + ".pyz")
                subprocess.run([sys.executable, str(SOURCE / "scripts/build_release.py"), "--version", number,
                                "--output", str(output)], env=self.env, check=True, timeout=15)
                self.artifacts.append(output)
            subprocess.run([sys.executable, "-I", str(self.artifacts[0]), "install", "--prefix", str(self.root / "install")],
                           env=self.env, check=True, timeout=15)
        else:
            subprocess.run([sys.executable, str(SOURCE / "scripts/install.py"), "--prefix", str(self.root / "install")], env=self.env, check=True)
        units = self.root / "config/systemd/user"
        units.chmod(0o755)
        for name in ("default.target", "basic.target", "shutdown.target", "exit.target"):
            (units / name).write_text("[Unit]\nDescription=Disposable lifecycle target\nDefaultDependencies=no\n")
        # No socket activation or default host services are in this search path.
        dbus = self.launch(["/usr/bin/dbus-daemon", "--session", "--nofork", "--nopidfile", "--address=" + self.env["DBUS_SESSION_BUS_ADDRESS"]], "dbus")
        wait_until(lambda: (self.root / "run/bus").exists(), (dbus,))
        manager = self.manager()
        wait_until(lambda: self.systemctl("show", "--property=Version", "--value", check=False).returncode == 0, (manager, dbus))
        relay = self.launch([sys.executable, str(SOURCE / "tests/fixture_relay.py"), str(self.root / "fixture")], "relay")
        wait_until(lambda: (self.root / "fixture/relay-port").exists(), (relay,))
        port = int((self.root / "fixture/relay-port").read_text())
        c, path = self.config(port)
        doctor = self.run("doctor", "--config", str(path))
        assert doctor["goose_path"] == c["harness"]["command"]
        assert doctor["live_buzz_tested"] is False
        created = self.run("create", "disposable", "--config", str(path))
        identity = created["identity"]
        assert created["desired"] == "stopped" and not created["runtime_started"]
        profile_path = self.root / "fixture/profile.json"
        assert not profile_path.exists()

        def check_profile(name):
            profile = json.loads(profile_path.read_text())
            assert profile["pubkey"] == identity and profile["tags"] == []
            metadata = json.loads(profile["content"])
            assert metadata["display_name"] == name and metadata["is_agent"] is True

        assert len(self.run("list")["agents"]) == 1
        assert self.run("show", "disposable")["identity"] == identity
        unavailable = self.root / "fixture/unavailable"
        unavailable.touch()
        assert self.run("start", "disposable", ok=False)["code"] == "start_unverified"
        events = self.state / "agents/disposable/events.jsonl"
        wait_until(lambda: events.read_text().count('"worker_failed"') >= 4, (relay, manager))
        waiting = self.run("status", "disposable")
        assert waiting["desired"] == "running" and not waiting["runtime_started"]
        assert waiting["service_result"] != "start-limit-hit"
        unavailable.unlink()
        # No start/reset-failed after the fixture begins accepting requests.
        wait_until(lambda: self.run_quiet_status()["runtime_started"], (relay, manager))
        check_profile("Disposable")
        print("PASS automatic recovery after four failed starts, without operator action", flush=True)
        marker = self.state / "agents/disposable/home/fixture-acp-initialized"
        wait_until(marker.exists, (relay,))
        wait_until(lambda: (self.root / "fixture/counts.json").exists(), (relay,))
        counts = json.loads((self.root / "fixture/counts.json").read_text())
        assert counts["profile_posts"] >= 1
        wait_until(lambda: json.loads((self.root / "fixture/counts.json").read_text())["subscriptions"] >= 1, (relay,))
        # Capture the actual supervisor, worker and installed harness relationship.
        pid = self.systemctl("show", "buzz-server-disposable.service", "--property=MainPID", "--value").stdout.decode().strip()
        assert int(pid) > 1
        print("PASS real systemd MainPID and real buzz-acp/ACP handshake", flush=True)
        new_prefix = self.root / "replacement prefix"
        if self.releases:
            new_prefix = self.root / "install"
            old_unit = (units / "buzz-server-disposable.service").read_bytes()
            old_pid = pid
            subprocess.run([sys.executable, "-I", str(self.artifacts[1]), "install", "--prefix", str(new_prefix)],
                           env=self.env, check=True, timeout=15)
            assert (units / "buzz-server-disposable.service").read_bytes() == old_unit
            assert self.systemctl("show", "buzz-server-disposable.service", "--property=MainPID", "--value").stdout.decode().strip() == old_pid
            assert self.run("version")["version"] == "1.1.0"
            print("PASS named release upgrade preserves running worker and stable unit", flush=True)
        else:
            subprocess.run([sys.executable, str(SOURCE / "scripts/install.py"), "--prefix", str(new_prefix)], env=self.env, check=True)
        unit = units / "buzz-server-disposable.service"
        old_unit = unit.read_bytes()
        self.binary = new_prefix / "bin/buzz-server"
        stopped = self.run("stop", "disposable")
        assert stopped["desired"] == "stopped" and not stopped["runtime_started"]
        assert unit.read_bytes() == old_unit
        started = self.run("start", "disposable")
        assert started["runtime_started"] and started["identity"] == identity
        assert str(new_prefix).encode() in unit.read_bytes()
        if self.releases:
            assert unit.read_bytes() == old_unit
        else:
            assert unit.read_bytes() != old_unit
        print("PASS stop from replacement installation and restart with refreshed unit", flush=True)
        c["profile"]["name"] = "Reconfigured"
        (self.root / "fixture/prompt.txt").write_text("Reconfigured synthetic prompt.\n")
        path.write_text(json.dumps(c))
        edited = self.run("configure", "disposable", "--config", str(path))
        assert edited["revision"] == 2 and edited["identity"] == identity and edited["runtime_started"]
        check_profile("Reconfigured")
        assert self.run("restart", "disposable")["identity"] == identity
        check_profile("Reconfigured")
        self.run("logs", "disposable")
        # A running service comes back after an actual private-manager restart.
        self.systemctl("stop", "buzz-server-disposable.service")
        self.exit_manager(manager)
        manager = self.manager()
        wait_until(lambda: self.systemctl("show", "--property=Version", "--value", check=False).returncode == 0, (manager, dbus))
        wait_until(lambda: self.run_quiet_status()["runtime_started"], (manager,))
        assert self.run("status", "disposable")["identity"] == identity
        self.run("stop", "disposable")
        c["profile"]["about"] = "Edited while intentionally stopped"
        path.write_text(json.dumps(c))
        assert self.run("configure", "disposable", "--config", str(path))["desired"] == "stopped"
        self.exit_manager(manager)
        manager = self.manager()
        wait_until(lambda: self.systemctl("show", "--property=Version", "--value", check=False).returncode == 0, (manager, dbus))
        stopped = self.run("status", "disposable")
        assert stopped["desired"] == "stopped" and stopped["service_state"] == "inactive" and not stopped["runtime_started"]
        # Reconfigure the stopped managed fixture without invoking a wizard or
        # provider: the deterministic ACP fixture owns its model.
        path.write_text(json.dumps(c))
        selected = self.run("configure", "disposable", "--config", str(path))
        assert selected["identity"] == identity and selected["desired"] == "stopped"
        assert selected["configuration"]["provider"] == "goose-managed"
        assert selected["configuration"]["model"] is None
        managed_marker = self.state / "agents/disposable/home/fixture-managed-acp-initialized"
        previous_handshake = managed_marker.stat().st_mtime_ns
        assert self.run("start", "disposable")["identity"] == identity
        wait_until(lambda: managed_marker.stat().st_mtime_ns > previous_handshake, (manager,))
        assert not self.run("status", "disposable")["buzz_response_verified"]
        self.run("stop", "disposable")
        print("PASS stopped managed reconfiguration and deterministic ACP launch; live reply unverified", flush=True)
        self.run("start", "disposable")
        # A loaded override must not silently change how the managed service is
        # started or stopped, even though the generated fragment is unchanged.
        unit = units / "buzz-server-disposable.service"
        fragment = unit.read_bytes()
        override = units / (unit.name + ".d") / "fixture.conf"
        override.parent.mkdir()
        override.write_text("[Service]\nTimeoutStopSec=9\n")
        self.systemctl("daemon-reload")
        before = self.systemctl("show", unit.name, "--property=MainPID", "--value").stdout.strip()
        assert int(before) > 1
        assert self.run("start", "disposable", ok=False)["code"] == "unit_conflict"
        assert self.run("delete", "disposable", ok=False)["code"] == "unit_conflict"
        assert unit.read_bytes() == fragment
        assert (self.state / "agents/disposable/definition.json").is_file()
        assert self.systemctl("show", unit.name, "--property=MainPID", "--value").stdout.strip() == before
        override.unlink()
        self.systemctl("daemon-reload")
        print("PASS override rejected; running PID and managed definition retained for operator recovery", flush=True)
        if self.releases:
            retained = (self.state / "agents/disposable/identity.key").read_bytes()
            self.run("uninstall")
            assert not self.binary.exists()
            assert not (units / "buzz-server-disposable.service").exists()
            assert (self.state / "agents/disposable/identity.key").read_bytes() == retained
            subprocess.run([sys.executable, "-I", str(self.artifacts[0]), "install", "--prefix", str(self.root / "install")],
                           env=self.env, check=True, timeout=15)
            assert self.run("version")["version"] == "1.0.0"
            assert self.run("start", "disposable")["identity"] == identity
            self.run("stop", "disposable")
            print("PASS release uninstall and identity-preserving rollback", flush=True)
        # Delete must stop an active service, then remove its managed definition.
        deleted = self.run("delete", "disposable")
        assert deleted["worker_stopped"] and deleted["data_retained"]
        assert (self.state / "agents/disposable/identity.key").exists()
        assert not (self.state / "agents/disposable/definition.json").exists()
        assert self.run("list")["agents"] == []
        self.run("status", "disposable", ok=False)
        self.run("create", "disposable", "--config", str(path), ok=False)
        # Missing Goose executable, invalid config, corrupt state and unavailable manager.
        c["harness"]["command"] = str(self.root / "missing-goose")
        path.write_text(json.dumps(c))
        assert self.run("create", "missing", "--config", str(path), ok=False)["code"] == "prerequisite_missing"
        c["relay_url"] = "ws://user:secret@localhost"
        path.write_text(json.dumps(c))
        self.run("create", "invalid", "--config", str(path), ok=False)
        c, path = self.config(port)
        self.run("create", "corrupt", "--config", str(path))
        definition = self.state / "agents/corrupt/definition.json"
        saved = definition.read_bytes()
        definition.write_text("{")
        self.run("list", ok=False)
        self.run("delete", "corrupt", ok=False)
        definition.write_bytes(saved)
        self.run("delete", "corrupt")
        unavailable = dict(self.env, DBUS_SESSION_BUS_ADDRESS="unix:path=" + str(self.root / "absent-bus"))
        # systemctl prefers its private socket; after manager exit both are absent.
        self.exit_manager(manager)
        self.run("status", "disposable", ok=False, env=unavailable)
        serialized = json.dumps(self.transcript)
        assert "SYNTHETIC-NEVER-A-REAL-KEY" not in serialized
        print("PASS complete disposable lifecycle (synthetic transport/inference; no live Buzz/model test)", flush=True)

    def run_quiet_status(self):
        p = subprocess.run([str(self.binary), "--json", "status", "disposable"], env=self.env, capture_output=True, timeout=25)
        return json.loads(p.stdout)["data"] if p.returncode == 0 else {"runtime_started": False}

    def cleanup(self):
        # Terminate only process handles launched by this script. A private
        # manager controls its own worker cgroups; never query the host manager.
        managers = [p for p in self.processes if isinstance(p.args, list) and p.args[0] == "/usr/lib/systemd/systemd" and p.poll() is None]
        if managers:
            try:
                self.systemctl("stop", "buzz-server-disposable.service", "buzz-server-corrupt.service", check=False)
            except (OSError, subprocess.SubprocessError):
                pass
        for p in reversed(self.processes):
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait(timeout=5)
        (self.root / "transcript.json").write_text(json.dumps(self.transcript, indent=2))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--disposable", action="store_true", required=True, help="Confirm this is an authorized disposable Linux environment")
    p.add_argument("--releases", action="store_true", help="Exercise named offline zipapp releases, stable upgrade and uninstall/reinstall")
    args = p.parse_args()
    if os.getuid() == 0:
        p.error("run as an unprivileged user, not root")
    os.umask(0o077)
    root = Path(tempfile.mkdtemp(prefix="buzz-lifecycle-"))
    print(f"Disposable evidence directory: {root}", flush=True)
    run = Acceptance(root, args.releases)
    result = 0
    try:
        run.lifecycle()
    except (Blocked, OSError, subprocess.SubprocessError) as e:
        print(f"BLOCKED: {e}", file=sys.stderr)
        result = 2
    except (AssertionError, ValueError, KeyError) as e:
        print(f"FAIL: {e}", file=sys.stderr)
        result = 1
    finally:
        run.cleanup()
    print(f"Evidence retained in {root}; no ongoing automation.", flush=True)
    return result


if __name__ == "__main__":
    sys.exit(main())
