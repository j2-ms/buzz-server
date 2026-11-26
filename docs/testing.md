# Verification

Run the unit tests as described in [Contributing](../CONTRIBUTING.md#develop-and-verify).

## Dedicated-account offline checks

`test_account_cli.py` drives the operator CLI through guided/scripted creation,
installed-CLI lifecycle, reconfiguration, interactive setup routing and deletion.
NSS, system tools and the system manager are synthetic. Checks include collection
outside mutation locks, remap/collision rejection at commit, reuse of owned private
tools, secret-free argv, typed target acknowledgments, retry after interrupted
publication, and uninstall refusal under both exclusive locks. A dedicated doctor
check needs no operator user manager. Same-account tests remain separate.

`test_accounts.py` checks binding corruption/remapping and fixed UID/argv/env.
It also rejects operator unit-source paths replaceable through untrusted ancestors.
`test_account_supervisor.py` checks effective-unit mismatches, staged publication,
typed retry journals, cgroup refusal and interrupted removal. The synthetic manager
models omitted empty hook and environment-file lists from `systemctl show --all`
on systemd 255; missing required fields and nonempty hooks still fail closed. It
also models a missing fragment that still has live or unknown process state;
retry must verify stopped state and empty cgroup before its reload or deletion.
These fixtures do not establish real systemd serialization or sudo behavior.
`test_account_target.py` executes file operations and synthetic tool scripts only
inside private scratch directories at the unprivileged test UID, with synthetic
NSS. It verifies permissions, identity/private-state retention, interrupted staging
and refusal to truncate an unrelated hardlink. `test_worker_payload.py` assembles
the worker from an actual local release zipapp, checks the exact module allowlist,
and executes its real stopped-worker entry without operator source access.

These tests do not run sudo, change real accounts/services, fetch dependencies or
contact providers/relays. A real disposable dedicated-account lifecycle and a
signed Buzz reply require separate verification. Persistent unit configuration
is not evidence of unattended reboot behavior.

## Worker and prompt deployment

The synthetic dedicated CLI tests cover an upgrade without replacing identity,
configuration or private state, keeping a stopped service disabled, reporting
the selected and running versions, rolling back after failed startup, and
retrying after the unit file was replaced but before the manager loaded it.
They also check that an interrupted activation accepts only the staged prompt
bytes, that an interrupted upgrade requires the original worker and pins, and
that reconfiguration leaves the approved prompt selected.
`test_accounts.py` uses scratch files and synthetic root ownership to check the
fixed staging commands, comparison before publication, unit content checks,
and refusal of unsafe modes, ownership and links. The packaged worker test
feeds a synthetic credential to the actual worker archive and checks that the
adapter is given that file directly. A missing or substituted credential causes
startup refusal. Operator dependency tests check whole-directory publication and
preservation of the previous directory after failed staging.

The hosted Ubuntu test runner additionally exercises real systemd credential
delivery with a transient unit running only as the unprivileged test UID.
It checks that the packaged reader can read the synthetic credential and that
the service UID cannot make it writable. To run just that check on a disposable
systemd host with existing operator sudo authorization:

```sh
BUZZ_SERVER_TEST_SYSTEM_CREDENTIALS=1 PYTHONPATH=tests timeout 30s \
  /usr/bin/python3 -B -m unittest \
  test_worker_payload.WorkerPayloadTests.test_packaged_reader_accepts_real_systemd_credential -v
```

The [reboot results](https://github.com/j2-ms/buzz-server/issues/4#issuecomment-7202259824)
cover the released v0.3.0 system and user units on Ubuntu 24.04 and 26.04 amd64.
The new deployment switch and managed prompt selection are covered by the
focused checks above. Full deployment and reboot acceptance for those changes
requires a disposable VM with the service account and sudo already configured.

## Disposable lifecycle test

Use an isolated Linux environment with Python 3.12+, OpenSSL, `dbus-daemon`, and systemd with cgroup v2. Install trusted `buzz` and `buzz-acp` binaries in `/usr/bin` or `/usr/local/bin` and run as an unprivileged user:

```sh
python3 -B scripts/acceptance.py --disposable
```

The test installs the CLI in a temporary prefix and starts a private systemd user manager and D-Bus socket. It uses a loopback relay fixture and deterministic ACP adapter. `doctor --config` checks the explicit fixture path rather than requiring Goose on the system path. The test checks lifecycle, recovery after failed starts, reloads and manager restarts, identity retention, and fail-closed handling of unit overrides. It does not use credentials or external services. The script stops its processes and prints the retained evidence directory.

For the dedicated route, use an isolated disposable systemd host with an
existing dedicated account, an unprivileged operator and already verified pinned
tools. Authorize ordinary operator sudo in the same terminal, then run:

```sh
sudo --validate
timeout 180s python3 -B scripts/acceptance_dedicated.py --disposable \
  --account buzz-fixture --tools /path/to/verified/tools/bin
```

The test uses the real system manager and packaged Buzz tools against a loopback
membership fixture. It checks a classified refusal, publication of the same key
after admission, a limited sequence of ambiguous failures, automatic recovery,
and operator stop. It deletes the test unit and ends the loopback server.
Desktop discovery, a reply, and a real relay's access policy require separate
tests.

## Release candidate acceptance

The offline unit suite builds synthetic named releases and exercises checksums,
version selection, failure preservation, locking, interactive consent, daily metadata
throttling and managed configuration. All release transport is synthetic; no GitHub requests are made.
Run the isolated systemd acceptance test serially with resource limits:

```sh
timeout 600s /usr/bin/python3 -B scripts/acceptance.py --disposable --releases
```

This mode builds local v1.0.0/v1.1.0 artifacts, checks a stable-path upgrade while
a worker runs, and checks stop/uninstall and identity-preserving reinstall.
The source-installer acceptance mode remains available without `--releases`.
Independent review, both disposable modes, public anonymous asset downloads,
a fresh OS install and live provider/relay verification remain separate gates.

## Installed CLI and update consent (offline)

`tests/test_update_pty.py` installs two synthetic zipapps with a local release
fixture and runs the launcher in a real terminal. It checks offline help and
version shortcuts, offer eligibility and consent, dispatch once with the exact
original arguments, and explicit updates during the daily offer throttle.
A blocked metadata response verifies that progress arrives before the response.
The tests also cover failed downloads, already-current output, and JSON and
non-TTY behavior. They do not invoke systemd or contact GitHub.
`tests/test_release.py` covers activation races, lock-free requests, installed
artifact verification, old probe no-op handling, preferences and interruption.
Run serially with the full bounded unittest command below; no live acceptance
or public download is implied.

## First-install bootstrap checks

`tests/test_bootstrap.py` uses synthetic metadata, injected synthetic transport and an explicitly
built local zipapp. It checks stable metadata parity with the updater, size/hash/
version rejection before execution, bounded output, sanitized environments and
errors, actual offline prefix installation, custom directory binding and safe retry.
`tests/test_release.py` also checks missing-tool reporting and exact Goose-path
guidance without executing prerequisite binaries. These tests do not acquire
upstream dependencies or prove their compatibility. `tests/test_dependencies.py` covers fixed-system reuse, consent suppression,
private permissions and symlinks, pinned size/digest verification, bounded exact
tar payload selection, fixed TLS/proxy/redirect boundaries, staged failure,
partial-publication retry and managed child paths with synthetic files. Bootstrap
tests check dependency flag and terminal forwarding after verification. A
slow local release fixture exercises a piped bootstrap with a controlling TTY,
including consent and refusal for a mode-0775 bin directory, and verifies the
launcher. Separate no-terminal and JSON runs preserve the unsafe mode and
return the actionable failure. The unit
suite does not download upstream assets or execute vendor tools.

`tests/test_public_transport.py` checks bootstrap/updater transport parity, fixed
initial URL construction, exact supplied browser URL matching, redirect destinations
and chain limits, verified TLS and disabled proxies, byte bounds, HTTP failures,
exclusive private files, and a process deadline that kills blocked transport.
The two-build offline install/update and terminal handoff checks execute only local
artifacts. These tests do not establish public availability or use real GitHub
transport, systemd, vendor binaries or a live model.

Run the full suite serially in a resource-limited cgroup, with additional limits:

```sh
(ulimit -v 524288; ulimit -t 120; timeout 180s /usr/bin/python3 -B -m unittest discover -s tests -v)
git diff --check
```

## What the tests prove

The [v0.3.0 first-use evidence](https://github.com/j2-ms/buzz-server/pull/18)
records a network-isolated trial with Buzz relay 0.3.0, Buzz Desktop 0.5.30 and
Goose 1.60.2 using a deterministic OpenAI-compatible fixture. The trial
admitted the dedicated agent's key on a membership-gated relay, confirmed its
profile, and added that same key to a channel through Desktop. The owner
mention and agent reply were read back in the original thread. Independent
verification checked signatures, authors, channel and reply linkage.

The same-account disposable lifecycle tests use a private systemd user manager,
loopback relay fixture and deterministic ACP adapter. Those tests verify
same-account lifecycle and recovery, including a manager restart. They provide
separate evidence from the membership-gated first-reply trial.

An earlier signed-reply test on disposable Ubuntu 24.04 amd64 ran the worker
directly against an isolated Buzz relay. A text-only control did not post. One
upstream model request selected Goose's shell tool, the only tool offered, and
the post-tool completion was local. Owner readback verified the agent author,
channel, original thread and signature. For a deployment, provider readiness
and a signed reply with the intended provider and relay policy require separate
verification. Mandatory owner attestation (NIP-OA) is unsupported. Verification
across logout and host reboot is tracked in
[issue #4](https://github.com/j2-ms/buzz-server/issues/4).

## Goose focused offline checks

Run shared-host checks serially with resource and time limits. For example:

```sh
(ulimit -v 524288; ulimit -t 60; timeout 60s python3 -B -m unittest discover -s tests -p test_managed.py -v)
```

Synthetic tests cover rejection of obsolete credential-file configurations with
static errors, unconfigured creation, status, identity-preserving reconfiguration,
regular restart behavior without invoking setup, runtime environment/model
exclusion, wizard argv/cwd/umask, terminal and JSON refusals, stopped-worker proof,
nonzero exits and Ctrl+C cleanup. A separate Python process probes the state
lock during interrupted child cleanup. Runtime and profile-helper checks retain
coverage that caller credentials and overrides are excluded. No test needs real
Goose settings, keys, provider requests or relays.

The disposable lifecycle script uses managed configuration throughout, verifies
unchanged identity after reconfiguration and checks an ACP initialization marker.
Run it with a private systemd user manager in an isolated environment. It bypasses
the real wizard with a deterministic ACP fixture and cannot establish Goose
credential storage behavior or a real provider response.

The Goose 1.60.2 native secret-file behavior with GOOSE_DISABLE_KEYRING=1 is an
upstream verification premise for this setup path, not a live verification by these
tests. A wizard zero exit means only setup exited. Provider selection (including
DeepSeek), credential validity, the installed Goose/buzz-acp integration and a
signed Buzz reply require separate explicitly approved live tests. Do not infer
readiness from status or inspect/output private Goose settings to prove it.
