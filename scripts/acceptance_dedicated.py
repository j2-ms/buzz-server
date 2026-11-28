"""Exercise a dedicated worker with real systemd and a loopback-only fixture."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

from acceptance import SOURCE, wait_until


def exercise(root, account, tools):
    state = root / "state"
    private_tools = state / "tools/bin"
    private_tools.mkdir(parents=True)
    for name in ("buzz", "buzz-acp", "goose"):
        target = private_tools / name
        shutil.copyfile(tools / name, target)
        target.chmod(0o700)
    ident = "restricted-fixture"
    owner = "ab" * 32
    command = [sys.executable, "-B", str(SOURCE / "entry.py"), "--json", "--state-dir", str(state)]

    def cli(*args, ok=True):
        result = subprocess.run([*command, *args], capture_output=True, text=True, timeout=75)
        assert result.returncode == (0 if ok else 1), (args, result.stderr)
        assert not (result.stderr if ok else result.stdout), args
        report = json.loads(result.stdout if ok else result.stderr)
        assert report["schema_version"] == 1 and report["ok"] is ok
        return report["data"] if ok else report["error"]

    def status():
        return cli("status", ident)

    fixture = root / "fixture"
    fixture.mkdir()
    (fixture / "restricted").touch()

    def attempts():
        return json.loads((fixture / "counts.json").read_text())["profile_attempts"]

    with subprocess.Popen([sys.executable, "-B", str(SOURCE / "tests/fixture_relay.py"), str(fixture)]) as relay:
        created = False
        try:
            wait_until((fixture / "relay-port").exists, (relay,))
            url = "ws://127.0.0.1:" + (fixture / "relay-port").read_text()
            prompt = root / "prompt.txt"
            prompt.write_text("Synthetic lifecycle fixture.")
            config = root / "config.json"
            config.write_text(json.dumps({
                "relay_url": url, "owner": owner, "profile": {"name": "Restricted fixture", "about": "", "avatar": ""},
                "prompt_file": str(prompt), "harness": {"kind": "goose", "command": str(private_tools / "goose")},
                "provider": {"kind": "goose-managed"},
            }))
            cli("doctor", "--account", account)
            identity = cli("create", ident, "--account", account, "--config", str(config))["identity"]
            created = True
            assert not (fixture / "counts.json").exists()
            assert cli("start", ident, ok=False)["code"] == "profile_refused"
            assert status()["startup"]["retry"] == "ended"
            assert not (fixture / "profile.json").exists()
            assert attempts() == 1
            print("PASS explicit membership refusal stopped retries before runtime readiness", flush=True)

            (fixture / "admitted-pubkey").write_text(identity)
            started = cli("start", ident)
            assert started["identity"] == identity and started["runtime_started"]
            assert started["startup"]["profile"] == "confirmed" and not started["buzz_response_verified"]
            profile = json.loads((fixture / "profile.json").read_text())
            assert profile["pubkey"] == identity and profile["tags"] == []
            assert json.loads(profile["content"])["is_agent"] is True
            print("PASS explicit restart published the same key and reached local readiness", flush=True)

            cli("stop", ident)
            (fixture / "unconfirmed").touch()
            before = attempts()
            assert cli("start", ident, ok=False)["code"] == "profile_unconfirmed"
            wait_until(lambda: status()["startup"]["retry"] == "ended", (relay,), timeout=25)
            failed = status()
            assert failed["startup"]["retry"] == "ended" and not failed["runtime_started"]
            assert 2 <= attempts() - before <= 4
            (fixture / "unconfirmed").unlink()
            print("PASS repeated ambiguous failures exhausted the systemd start limit", flush=True)

            (fixture / "transient").touch()
            assert cli("start", ident)["runtime_started"]
            assert not (fixture / "transient").exists()
            print("PASS systemd recovered from a transient publication failure", flush=True)

            cli("stop", ident)
            (fixture / "unconfirmed").touch()
            assert cli("start", ident, ok=False)["code"] == "profile_unconfirmed"
            assert status()["startup"]["retry"] == "pending"
            stopped = cli("stop", ident)
            assert stopped["desired"] == "stopped" and stopped["service_state"] == "inactive"
            assert not stopped["runtime_started"]
            before = attempts()
            time.sleep(5.5)
            assert attempts() == before
            print("PASS operator stop cancelled retries", flush=True)
        finally:
            try:
                if created:
                    cli("delete", ident)
            finally:
                relay.terminate()
                relay.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disposable", action="store_true", required=True,
                        help="Use this host's real system manager only in an authorized disposable environment")
    parser.add_argument("--account", required=True, help="Existing unprivileged dedicated test account")
    parser.add_argument("--tools", required=True, type=Path, help="Directory of already verified pinned runtime tools")
    args = parser.parse_args()
    if os.getuid() == 0:
        parser.error("run from the existing unprivileged operator account with sudo already authorized")
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix="buzz-dedicated-lifecycle-") as temporary:
        exercise(Path(temporary), args.account, args.tools)


if __name__ == "__main__":
    main()
