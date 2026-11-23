# Linux service accounts

The recommended route keeps Buzz Server's user-local management CLI under the
operator login and runs each agent in a different existing unprivileged account.
The `owner` configuration field is a **Buzz public key**, separate from the
Linux account and generated agent identity.

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
read its prompts, keys and credentials. Interactive `inference configure` also
attaches target-writable worker code to the operator's terminal. Do not use it
from your operator session if you suspect the account is compromised; ask the host
administrator to handle recovery. Target reports never select a privileged
command. Status reports system-service activity separately from runtime/provider
or Buzz verification. The unit is persistent and enabled by start, but unattended
boot/reboot operation and the real dedicated-account lifecycle have not been
qualified by offline fixtures.

Create/install interruptions retain an operator binding and private retry journal.
Retry the same create command with the same account/configuration. A retained
binding reserves the ID. Stop/delete interruptions retain authority and state;
retry the same command. Do not erase journals or guess at root-owned unit removal.
`list` refuses incomplete lifecycles and directs you to retry them. Dedicated
`logs` and `restart` are currently unavailable; use status and explicit stop/start.

Delete verifies disabled autostart, stopped service and an empty cgroup before
removing the system unit. It retains the target identity, Goose credentials,
home/work files, staged worker/tools and the private operator binding/removal
proof. It removes neither the Linux account nor past Buzz messages. The target
data location is beneath `HOME/.local/share/buzz-server-managed/NAMESPACE/ID`;
the CLI does not read retained credentials to verify deletion.

Updating/reinstalling the operator CLI does **not** upgrade already staged
dedicated workers or tools. There is no fleet upgrade mechanism. Keep the existing
installation and binding until an independently reviewed upgrade procedure is
available. Uninstall refuses attached, incomplete, corrupt or unverifiably removed
dedicated bindings; finish delete first. Verified deleted bindings retain private
data while allowing CLI uninstall. Account remapping after deletion can still
require offline administrator inspection before uninstall.

## Same-account alternative

Omit `--account` to keep the existing CLI, data layout and `systemd --user`
services under the invoking account. This is explicitly nonisolated from that
login's other files and authority. `doctor` without `--account` retains the user
manager and runtime checks, including existing `--config` scripts. This route
requires a functioning user session/manager; arrange persistence separately with
the administrator if needed. The CLI grants no sudo and changes no linger policy.
Existing guided creation, JSON/config-file commands and same-account update and
uninstall remain available. Same-account unit refresh on start differs from the
frozen dedicated worker/tool payload described above.
