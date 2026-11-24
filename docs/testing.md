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

## Disposable lifecycle test

Use an isolated Linux environment with Python 3.12+, OpenSSL, `dbus-daemon`, and systemd with cgroup v2. Install trusted `buzz` and `buzz-acp` binaries in `/usr/bin` or `/usr/local/bin` and run as an unprivileged user:

```sh
python3 -B scripts/acceptance.py --disposable
```

The test installs the CLI in a temporary prefix and starts a private systemd user manager and D-Bus socket. It uses a loopback relay fixture and deterministic ACP adapter. `doctor --config` checks the explicit fixture path rather than requiring Goose on the system path. The test checks lifecycle, recovery after failed starts, reloads and manager restarts, identity retention, and fail-closed handling of unit overrides. It does not use credentials or external services. The script stops its processes and prints the retained evidence directory.

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

## Pre-command update consent (offline)

`tests/test_update_pty.py` builds two synthetic zipapps, replaces only their fixture
transport and dispatch, and runs real terminal subprocesses. It checks acceptance,
decline, failed download, JSON and non-TTY behavior, exact original argv and one
observable dispatch side effect. It does not invoke systemd or contact GitHub.
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
tests check dependency flag and terminal forwarding after verification. The unit
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

The lifecycle test uses a real private systemd user manager with a loopback relay
fixture and deterministic ACP adapter. This is separate from the signed-reply
test, which starts the managed worker directly against an isolated Buzz relay.

On disposable Ubuntu 24.04 amd64, Goose 1.60.2 and Buzz CLI/ACP 0.5.30 handled
a synthetic owner mention. A text-only control did not post. A live model then
chose Goose's shell tool on an isolated test with one upstream model request;
only the shell tool was offered, and the post-tool completion was local. An
owner readback verified the agent author, channel, original thread and signature.
This does not qualify other models, relay policies or production configuration.
Before deployment, verify a signed reply with a dedicated test agent, the
intended relay policy and the actual provider. Mandatory NIP-OA is unsupported;
host reboot was not tested.

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
