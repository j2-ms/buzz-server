# Linux service accounts

Install and run the user-local management CLI from your unprivileged operator
login. Choose the account that will run the agent.

- **Same account.** Use your login's working `systemd --user` manager, with no
  second account or sudo. The agent shares your login's files and authority.
- **Dedicated account (recommended for isolation).** Run the agent under a
  pre-existing unprivileged account using the system manager. Privileged actions
  require explicit operator sudo. The dedicated `doctor` check runs without sudo.

The `owner` configuration field is a **Buzz public key**, separate from the
Linux account and generated agent identity.

## Dedicated account

Ask the host administrator to supply one dedicated account per agent, with a
nonzero UID/GID distinct from the operator, a real private mode-0700 home and
root-owned parent directories without group/world write access. Home paths use
letters, digits, underscores, dots, slashes and hyphens. The account must have no
sudo or other administrative authority. Linux with Python 3.12+, OpenSSL,
systemd's system manager and cgroup v2 is required. The operator needs ordinary
explicit sudo authorization for the fixed system-tool operations and target-UID
operations. Buzz Server installs no sudo policy or permission grant. It never
creates or retires accounts, configures login access, enables linger, or changes
a relay. No target login or user manager is required.

For an interactive command (stdin, stdout and stderr all terminals, without
`--json`), Buzz Server runs fixed `/usr/bin/sudo --validate` once before its first
sudo effect. Authentication uses the operator's existing policy and controlling
TTY; its human wait is separate from command deadlines. It holds the existing
mutation/installation locks during this wait and child cleanup; cancel with
Ctrl+C rather than killing the CLI. The CLI rechecks the binding after
authentication and makes its service safety checks after that wait. Effects
always use `sudo --non-interactive`: fixed system tools retain their 30-second
deadline and target operations 60 seconds.
Interactive Goose setup retains direct terminal forwarding and no wizard deadline.

JSON and non-TTY calls never run interactive validation. They require existing
passwordless authorization for the exact effects or a usable cached timestamp.
For recovery, from the same operator terminal run `/usr/bin/sudo --validate`,
then retry the original command (including `--json` for automation). A cache
must apply to the later sudo process and its execution identity under local
policy. If validation is cancelled or fails, no privileged effect follows it.
If the cache expires between effects, the next effect fails without prompting; retained
state/journals support retry. A policy such as `timestamp_timeout=0`, or a
process-scoped timestamp that cannot be reused, may prevent this split from
working even after successful validation. Existing exact-effect NOPASSWD grants
can still support noninteractive/JSON operation; interactive validation itself
may require authentication under local policy. The Goose wizard cannot use the
JSON/non-TTY fallback: its interactive validation must succeed too. If neither a
reusable timestamp nor existing passwordless authorization is available, the
dedicated-account route is unsupported.
Do not pipe passwords, run the CLI as root, or add/relax grants to work around it.
Doctor, account-plan, status/show/list and uninstall's system-manager removal
proof remain read-only and do not request sudo validation.

Run `buzz-server doctor --account ACCOUNT` from the operator login to check the
account layout and reachability of the system manager without sudo. It does not
verify the account's effective administrative permissions; the host administrator
must check local policy, including direct grants as well as group membership.
Doctor does not check tools, provider credentials, relay admission or a live reply.
`account-plan --account ACCOUNT` is a read-only account-selection aid.

`create ID --account ACCOUNT` guides you through the normal agent questions.
`create ID --account ACCOUNT --config /absolute/private.json` supports automation;
put `--json` before the subcommand for the versioned stdout/stderr contract.
The prompt referenced by that JSON is read under the operator UID and transferred
on private stdin. Tool and prompt paths in the target are chosen automatically;
the dedicated route uses staged pinned Goose rather than the supplied harness path.
Configure, inference setup, start, status, stop and delete then use the saved
binding, without repeating the account argument. See the [quickstart](quickstart.md).

The private operator binding records the account name, numeric UID/GID, target
data path and deterministic service ID. Keep it and the operator state directory.
Moving that directory changes the namespace and is unsupported. Before effects,
its ancestry must also prevent another UID from replacing the operator's unit
source path: directories belong to root or the operator, and writable ancestors
must be protected by a private operator ancestor or root-owned sticky directory.
The CLI also checks the current account mapping and trusted system manager's effective
unit: fragment path, execution identity/argv and absence of overrides. Corruption,
remapped accounts, foreign units and unverifiable stops fail closed. Fix conflicts
with the responsible administrator; a failed stop is never evidence of shutdown.
Keep the bound UID and GID reserved for their dedicated account while its unit is
installed. Systemd uses these numeric identities for launch and prompt access.

Native root-owned units live under `/etc/systemd/system/buzz-server-account-*.service`.
Ordinary explicit sudo invokes only fixed system tools for placement and control;
project Python and target-owned code never run as root. Target file operations and
Goose setup run only at the bound target UID with a clean environment. The target
receives a worker-only archive and pinned tools, with private files/directories;
operator management code, credentials and unrelated account data are not copied
or made readable. Private installed tools are reused after ownership, layout,
mode and pinned-size checks; their bytes are hashed for transfer. Missing files
are acquired through the existing pinned archive verification and extraction path.

This is a Unix account boundary, not a sandbox. Agent tools retain that account's
permissions, including network access. Anyone able to execute as that UID can
read its prompts, keys and credentials. Interactive `inference configure`
attaches the target account to the operator's terminal. Do not use it
from your operator session if you suspect the account is compromised; ask the host
administrator to handle recovery. Target reports never select a privileged
command. Status reports system-service activity separately from runtime/provider
or Buzz verification. Starting enables the system unit at boot, and stopping
disables it. The [disposable VM results](https://github.com/j2-ms/buzz-server/issues/4#issuecomment-7202259824)
record operator logout, startup before login, an intentionally stopped agent and
identity retention on Ubuntu 24.04 and 26.04 amd64 using Buzz Server v0.3.0.

Create/install interruptions retain an operator binding and private retry journal.
Retry the same create command with the same account/configuration. A retained
binding reserves the ID. Stop/delete interruptions retain authority and state;
retry the same command. Do not erase journals or guess at root-owned unit removal.
`list` refuses incomplete lifecycles and directs you to retry them. Dedicated
`logs` and `restart` are currently unavailable; use status and explicit stop/start.

Delete verifies disabled autostart, stopped service and an empty cgroup before
removing the system unit. It retains the target identity, Goose credentials,
home/work files, staged worker/tools, approved prompts and the private operator binding/removal
proof. It removes neither the Linux account nor past Buzz messages. The target
data location is beneath `HOME/.local/share/buzz-server-managed/NAMESPACE/ID`;
the CLI does not read retained credentials to verify deletion.

Uninstall refuses attached, incomplete, corrupt or unverifiably removed
dedicated bindings; finish delete first. Verified deleted bindings retain private
data while allowing CLI uninstall. Account remapping after deletion can still
require offline administrator inspection before uninstall.

### Maintain an existing agent

Use these commands from the operator login for a named dedicated-account agent.

```sh
buzz-server status assistant
buzz-server worker upgrade assistant
buzz-server worker rollback assistant
```

`status` shows the selected worker and the worker systemd observed ready, with
their Buzz and Goose versions. The worker version for an original installation
is unrecorded. `worker upgrade` verifies the current CLI's pinned packages and
stages a separate worker and tool set before stopping the service. An agent
requested running is restarted. A stopped agent stays stopped. The switch
preserves the agent identity, configuration, Goose setup and private state.
If startup fails, check `status` and use `worker rollback` to select the
previous payload. Retry an interrupted upgrade with the CLI that staged it.
An explicit `stop` cancels an unpublished switch or stops the installed unit.
Allow disk space for up to three worker payloads plus download and verification
scratch space. Root-owned payloads are under `/var/lib/buzz-server`.

Maintenance uses fixed sudo effects. For operators with exact-effect
authorization, staging uses `/usr/bin/install -d` and `/usr/bin/install -T`
under `/var/lib/buzz-server`, then `/usr/bin/cmp --silent`,
`/usr/bin/mv -T`, and `/usr/bin/sync -f`. Recovery can remove an interrupted
stage with `/usr/bin/rm`. Switching also replaces the staged unit in
`/etc/systemd/system` with `mv -T` and synchronizes that directory before
`systemctl --system daemon-reload`. All effects use existing operator sudo.

After the worker upgrade, approve an operator-owned prompt file with private
permissions such as mode 0600. The file must be UTF-8 and at most 64 KiB.

```sh
buzz-server prompt activate assistant --file /absolute/private/assistant.md
buzz-server prompt rollback assistant
buzz-server prompt clear assistant
```

`prompt activate` snapshots the file and selects that revision for the service.
Run it again to approve edits. Automatic retry and reboot use the selected
snapshot. Systemd loads it as a read-only credential for the bound account, and
the root-owned worker passes it directly to the adapter. The source file and its
surrounding directory stay private to the operator. `status` shows revision IDs.
To finish an interrupted activation, supply a file with the originally staged
bytes. Use `stop` before starting a new activation if the source has changed.
Goose can also load instructions from agent-writable configuration and its
working directory.
`prompt rollback` reselects the prior approved revision, and `prompt clear`
returns to the prompt from the agent configuration. Clear the active prompt
before rolling back to the original worker.

`buzz-server dependencies refresh` replaces the operator's private pinned tools
at their existing paths. Fixed system tools still take precedence when the
operator's CLI resolves a tool. CLI self-update, operator dependency refresh,
and per-agent worker upgrade are distinct operations.

## Same-account alternative

Omit `--account` to keep the existing CLI, data layout and `systemd --user`
services under the invoking account. This is explicitly nonisolated from that
login's other files and authority. `doctor` without `--account` retains the user
manager and runtime checks, including existing `--config` scripts. This route
requires a functioning user session/manager; arrange persistence separately with
the administrator if needed. The CLI grants no sudo and changes no linger policy.
Read the current setting with `loginctl show-user ACCOUNT --property=Linger`.
Without linger the user manager can stop after the last login ends. Enabling
linger with `loginctl enable-linger ACCOUNT` is an explicit administrator action.
Existing guided creation, JSON/config-file commands and same-account update and
uninstall remain available. Same-account unit refresh on start differs from the
frozen dedicated worker/tool payload described above.

For either route, follow the [quickstart](quickstart.md#create-and-configure)
from preflight and creation through shared Goose setup, relay admission and a
first reply.
