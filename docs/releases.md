# Release installation and updates

Linux, `/usr/bin/python3` 3.12+, and a working
unprivileged systemd user session are required. Compatible manually installed Buzz, buzz-acp and Goose are reused unchanged.
Optional private prerequisite downloads never change host services.

## First install

Use the [public-release instructions in README](../README.md#run-an-agent-from-your-operator-login).
Both bootstrap and updater pin `repos/j2-ms/buzz-server`. Neither supports an
endpoint or repository override.
The first-install asset is **install-buzz-server.py**, published from the standalone,
versioned `scripts/bootstrap.py`. It imports no Buzz Server code before verifying
the downloaded zipapp. Obtain that reviewed bootstrap from the exact release;
its initial acquisition is a separate trust step.

Bootstrap and updater use the same anonymous standard-library transport. No `gh`,
login, credential files, netrc or inherited authentication is used. Metadata starts
only at `https://api.github.com/repos/j2-ms/buzz-server/releases/latest`.
The binary starts independently at
`https://github.com/j2-ms/buzz-server/releases/download/vX.Y.Z/buzz-server.pyz`,
constructed from the validated stable numeric version and fixed asset name.
A supplied `browser_download_url` must equal that exact URL. REST asset endpoints
are not used for binary acquisition because they may return JSON metadata.

Downloads use direct HTTPS with TLS certificate/hostname verification and the
Ubuntu system CA bundle, `/etc/ssl/certs/ca-certificates.crt` (provided by
`ca-certificates`). Proxies and ambient TLS/config/credential overrides are excluded.
Each request has a 30-second total process deadline (including DNS, TLS, redirects
and body reads), a 10-second socket timeout and at most four redirects. Metadata
redirects stay on `api.github.com`; binary redirects allow only `github.com` and
`release-assets.githubusercontent.com`. Every destination requires HTTPS, no
userinfo or fragment, and default port or 443. There are no retries or alternate
endpoints. Metadata is limited to 1 MiB and the app to its exact declared size,
up to 8 MiB. Rate limits, private repositories and unavailable assets fail closed
with sanitized errors. Temporary downloads are private and removed on ordinary
failures or interruption; after a forced kill an abandoned private
`buzz-first-install-*` directory may need manual removal. Verified zipapp execution
retains the scrubbed child environment and existing installation output contract.

The bootstrap requires a stable numeric `vX.Y.Z`, `draft=false`,
`prerelease=false`, exactly one `buzz-server.pyz`, and GitHub's `sha256:` asset
digest. It verifies exact size, hash, ZIP structure and matching embedded version
before the first execution. No prerelease or draft is used as the stable channel.
The bootstrap itself is trusted through the exact repository and release used
to fetch it, **not** an independent publisher signature. Publish it only with the
same review and access controls as the app.

Supply `--prefix`, `--state-dir` and `--unit-dir` to the bootstrap for custom
locations. Only nonsecret directory paths belong in these arguments. Defaults
are `$HOME/.local/buzz-server-cli`, `$HOME/.local/share/buzz-server` and
`$HOME/.config/systemd/user`; ambient XDG overrides are deliberately ignored by
the bootstrap. Use the same explicit global state/unit arguments for subsequent
CLI operations when using custom locations. `--json` preserves the v1 CLI stdout/
stderr envelope and suppresses prose. The installed CLI serializes mutations
with its existing prefix lock; a failed download never activates code. Retry the
same prefix and directory arguments after interruption.

The interactive consent prompt has no bootstrap time limit; downloads and probes
have their own limits. The noninteractive bootstrap gives the verified installer
10 minutes. If it times out, Buzz Server may already be installed. Retry the
same command to finish.

The prefix must be private and empty on first installation, with no symlink
components. Do not use a source installation's prefix. An already verified offline
`buzz-server.pyz` still supports:

```sh
/usr/bin/python3 -I buzz-server.pyz install --prefix "$HOME/.local/buzz-server-cli"
```

### Optional private prerequisites

Default installation downloads only verified Buzz Server. If tools are missing
and the installer has a terminal, installation lists existing/missing tools, roles,
pinned source/version and private destination, explains that no sudo or services
are involved, then asks `Install missing tools? [Y/n]`. Declining leaves the CLI
installed. When bootstrap input is piped, it reopens the controlling terminal for consent if output is terminal-bound. Noninteractive and `--json` invocations never prompt.

Both the bootstrap and the verified zipapp's `install` command accept mutually
exclusive `--install-dependencies` (explicit noninteractive consent) and
`--skip-dependencies` (suppress prompts). For example, pass `--install-dependencies` to the downloaded bootstrap, or retry
an already verified local artifact:

```sh
/usr/bin/python3 -I buzz-server.pyz install --prefix "$HOME/.local/buzz-server-cli" --install-dependencies
```

Use the original `--state-dir` and `--unit-dir` global arguments when customized.
Managed downloads refuse root and allow **Ubuntu 24.04 and 26.04 amd64**. Pinned binary compatibility on 26.04 has not been tested. On
compatible other Linux hosts, install compatible upstream tools manually in
`/usr/local/bin`, `/usr/bin` or `/bin`. No ambient PATH is searched. Existing system
tools take precedence, then private tools in the installation's bound
`STATE_DIR/tools/bin`. That directory accepts only regular, account-owned,
mode-0700 `buzz`, `buzz-acp` and `goose` files; symlinks, unsafe permissions and
unknown entries fail closed. Child PATH uses the same fixed system paths followed
by that validated private directory. A configured absolute Goose command remains
supported.

Downloads use direct verified HTTPS without proxies, inherited authentication or
TLS overrides. Public upstream assets are not rehosted. Exact full-file size and
SHA-256 are checked before parsing or executing anything:

| Upstream file | Bytes | SHA-256 |
| --- | ---: | --- |
| [Buzz_0.5.30_amd64.deb](https://github.com/block/buzz/releases/download/desktop-v0.5.30/Buzz_0.5.30_amd64.deb) | 120825052 | `1834e4db989637a800e481d48ea2d0a743d31d62bcb9d0361ca8d253828324d6` |
| [goose-x86_64-unknown-linux-gnu.tar.gz](https://github.com/aaif-goose/goose/releases/download/v1.60.2/goose-x86_64-unknown-linux-gnu.tar.gz) | 63566822 | `5f6e463621a8428617ae4870eecee7a00a253e9bbfccb6340dfaa4f0ec2aaf32` |

Only exact regular `usr/bin/buzz` (18855512 bytes), `usr/bin/buzz-acp`
(18202248 bytes) and `./goose` (213003128 bytes) are copied into private staging.
Buzz's deb is read using `/usr/bin/dpkg-deb --fsys-tarfile` with finite output and
time limits. No package installation, postinst, vendor script, archive path
extraction or link following occurs. Tar reading and decompression are bounded;
ordinary unrelated files/directories are ignored, while links, traversal and
extended metadata records are refused. Allow roughly 1 GiB of temporary disk
space for staging. Offline help probes check Buzz profile operations, required
buzz-acp flags, `goose acp` and `goose configure` before any tool is published.
All necessary packages stage successfully before publication. Only missing tools
are published; existing external or valid managed binaries are never replaced.
The installer does not hold the Store lock while waiting for consent. It rechecks
the approved tools before acquiring them, and holds the lock during staging and
publication to serialize cooperating acquisitions.

On dependency failure the working Buzz Server install remains. Output reports
exactly which tools were published, remaining missing tools and a safe retry
message. JSON keeps its v1 envelope: successful application installation has
`ok: true`, with `dependencies_installed`, `missing_runtime_tools`,
`runtime_tool_paths` and a non-null `dependency_error` when optional acquisition
fails. Retry `install --install-dependencies` with the same prefix/state/unit
arguments. That retry clears abandoned private staging under `STATE_DIR/tools/`
and completes only the missing tools. The `.stage-*` namespace there is reserved
for installer scratch; do not store operator data in it.

Presence alone does not prove compatibility or relay/provider readiness. Set
`harness.command` in the [agent JSON](quickstart.md) to the reported exact absolute
Goose path and pass that file with `--config`. Run the installed
`bin/buzz-server doctor` next; `doctor --config FILE` uses and reports the resolved
configured Goose path. Doctor checks local interfaces, never credentials or a
live provider/relay. Agent creation and Goose configuration remain separate.
Verify upstream artifacts and fresh-host compatibility separately; synthetic
tests establish neither.

Managed downloads and private agent state remain on application uninstall.
Dependencies have no manifest, background acquisition or update mechanism.
Private-tool cleanup is a separate deliberate operation: stop all agents using
them, inspect the installation's original bound state directory, and remove only
its `tools` directory when those binaries are no longer needed. Never remove the
state root or agent directories to clean up dependencies. After a forced kill,
abandoned private `STATE_DIR/tools/.stage-*` directories may also need removal
once no install is running.

## Upgrade and remove

```sh
buzz-server update                 # explicit anonymous, verified stable upgrade
buzz-server update --auto on       # enable interactive offers; no immediate check
buzz-server update --auto off      # disable interactive offers
buzz-server uninstall
```

`--json` before the subcommand preserves the v1 stdout/stderr contract.
`--version` and `version` are offline. Built-in update checks and downloads use
the same fixed anonymous HTTPS contract as the bootstrap above. No GitHub login
or default credential file is consulted. Offline, rate-limit or unavailable-release
errors leave the previous CLI usable. Explicit updates
report sanitized errors; automatic check failures stay quiet.
An already verified local artifact can also run `install --prefix SAME_PREFIX`.
An active installation cannot downgrade.
`buzz-server update` updates the management CLI, not privately installed runtime tools.

On a fresh first install, interactive update offers are enabled. Existing installs
retain their stored on/off preference unchanged; `update --auto on/off` now controls
interactive offers only. There is no timer, detached probe, unattended download or
activation in this version. The old private probe command is a no-op; legacy cache
and lock files remain recognized by the installation inventory. If migrating from
an unpublished build with background updates, run `buzz-server update --auto off`
using that installation before upgrading. Old code already in memory keeps its
old behavior; the new CLI cannot replace it. After upgrading, use `update --auto on`
if you want the new interactive offers.

Before a due ordinary command with stdin, stdout and stderr all terminals, the CLI
synchronously checks for a newer stable release and offers it before dispatch.
Only `y` or `yes` (case insensitive, surrounding whitespace ignored) accepts;
EOF and every other answer decline. The exact checked version, asset ID, digest
and size bind the download. Metadata checks, declines and outages are throttled
once daily, including the first synchronous check. Explicit `update` is the
unthrottled retry. If a confirmed update cannot be applied, a brief diagnostic
explains how to retry before the original command runs once with the installed
CLI. Ctrl-C aborts before dispatch. JSON, redirected streams,
help/version, workers, explicit maintenance commands, root, source invocations
and invalid installation bindings never offer updates.

Accepted activation executes the verified immutable new zipapp with the original
arguments once. It does not restart workers. Exec failure reports a command error
without dispatching the old CLI; the verified active pointer remains committed,
so retry the command through the launcher. Only accept if you trust everyone who
can publish releases in the repository to run code as this Linux account. The
asset digest checks bytes against GitHub metadata, not an independent signature.

The stable launcher resolves an immutable versioned zipapp before execution.
Activation replaces one symlink; foreground upgrades never restart live agents.
Old payloads remain until uninstall so running workers can reopen their original
code. This uses disk space for each upgrade. Eligible due offers validate the
installed archives before prompting. Lifecycle commands and release mutations
share a prefix lock, with a 10-second acquisition limit; state operations also
use the existing state lock.
Metadata requests, consent reads and downloads hold no lifecycle lock; activation
revalidates the binding, version, preference and exact downloaded artifact under
a short write lock.

Dedicated-account workers/tools are staged independently of the operator CLI.
Updating or reinstalling the CLI does not upgrade those payloads. There is no
fleet upgrade mechanism. Uninstall checks dedicated bindings under the exclusive
installation and state locks and refuses any attached, incomplete, corrupt or
unverifiably removed binding before touching user units or the installation.
Finish/retry dedicated deletion first; only verified deleted bindings allow
uninstall, while retaining target private state and operator removal proofs.
See the [account guide](service-accounts.md).

For same-account agents, uninstall preflights every managed user unit in the bound state/unit directories,
then verifies stop, disable and empty cgroups before removing managed units and
installation payloads. Unknown ownership, overrides, unsafe files or unknown stop
state block removal. A later failure can leave some agents stopped; retry after
resolving the problem. Keys, definitions, prompts, agent state, accounts and
external tools are retained. A small private installation tombstone and lock files
remain for safe same-prefix reinstall; interactive offers reset to off. Same-prefix
reinstall preserves that off preference; only a fresh prefix defaults to on. Foreign
files in the prefix block installation/removal rather than being deleted.
Reinstall to the same prefix and directories, then `start ID` to restore a unit
with its original identity. After a completed uninstall you can use an older,
verified artifact to roll back, with downtime while the agents are stopped.
Keep a known-good artifact for recovery. Deleted agent definitions cannot be
recreated this way.

For legacy/source installations, stop agents with the existing CLI first, install
the release into a new empty prefix using the same state/unit directories, and
start each agent from the new CLI to refresh its unit. This is an explicit local
conversion, not a host migration. The source installer remains for development;
it does not provide immutable payloads or release update support. Do not run it
over a release prefix.

## Maintainer build and publishing gate

```sh
/usr/bin/python3 -B -m unittest discover -s tests -v
version=0.1.0  # Set to the numeric version of the release being built.
stage=$(mktemp -d)
/usr/bin/python3 -B scripts/build_release.py --version "$version" --output "$stage/buzz-server.pyz"
```

In the same shell, stage the first-install asset separately (never from a
downloaded runtime) and require every entry point to report the release version:

```sh
cp scripts/bootstrap.py "$stage/install-buzz-server.py"
test "$(/usr/bin/python3 -B entry.py --version)" = "$version"
test "$(/usr/bin/python3 -I "$stage/buzz-server.pyz" --version)" = "$version"
test "$(/usr/bin/python3 -I "$stage/install-buzz-server.py" --version)" = "$version"
```

Before tagging, stop on any mismatch and align the source CLI and bootstrap
versions with the release version. The offline bootstrap tests build a local
candidate file, supply synthetic GitHub responses
through injected synthetic transport, and execute that verified local file into a disposable prefix.
They exercise state binding and retry without GitHub publication or systemd.
Verify public anonymous download, a fresh Ubuntu install, upstream interfaces,
first-use setup and a real signed reply separately.
Do not create a draft/prerelease as an updater test channel. No workflow,
repository setting or alternate hosting channel is introduced.

The builder writes a deterministic, uncompressed zipapp with an embedded version
and `buzz-server.pyz.sha256`. Version selection is explicit and numeric. Publish
only after independent review and disposable acceptance, using separately
authorized maintainer actions: create the matching stable `vX.Y.Z` release in
`j2-ms/buzz-server`, attach `buzz-server.pyz`, `buzz-server.pyz.sha256` and the unchanged
`install-buzz-server.py`, and verify GitHub's asset metadata
contains the same SHA-256 digest and size. Missing or invalid GitHub digests are
rejected by the updater. No alternate endpoints or update servers are supported.
No publishing, tagging, service changes or live provider/relay calls occur in the
builder or unit tests. See [verification](testing.md) for outstanding gates.

For the default prefix the installer adds `~/.local/bin/buzz-server`. If your shell does not find it, run `"$HOME/.local/bin/buzz-server" doctor` and consult your distribution’s instructions for enabling `~/.local/bin` on PATH. Custom prefixes use their own `bin/buzz-server`.
