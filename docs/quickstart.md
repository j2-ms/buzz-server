# Set up an agent

Start with the [installation instructions](../README.md#run-an-agent-from-your-operator-login).
Run every `buzz-server` command from your unprivileged operator login. See
[launcher and PATH recovery](releases.md#find-the-launcher) if the shell can't
find the command.

- **Same account.** The worker and Goose run as your login UID under a
  `systemd --user` unit, with access to your login's files and other resources.
  This route needs a working user manager, with no second account or sudo.
- **Dedicated account (recommended for isolation).** A system unit runs the
  worker and Goose at the UID of a
  [pre-existing unprivileged account](service-accounts.md), such as
  `buzz-assistant`. Use explicit operator sudo when asked for privileged actions.

Before creating an agent, confirm that your relay accepts a new public key
without owner attestation (NIP-OA). Ask the relay operator if you don't know the
admission policy. An open relay can accept the key directly. On a managed relay,
ask the operator whether they can explicitly admit the generated key under the
existing policy. If their only supported route is an owner attestation, stop:
Buzz Server does not produce it. Do not change relay policy just to run the
quickstart. Use `wss://`; plain `ws://` is only allowed for literal loopback
addresses.

On a machine where you already use Buzz as the intended operator, run
`buzz users get` with that identity against the same relay. Copy `pubkey` from
your profile, or obtain the public key from your Buzz client. The guided prompt
accepts 64 lowercase hex characters or a valid `npub`. Keep your private key
(`nsec`) on your client.

## Create and configure

Run the preflight for your route, then create a stopped agent. The same-account
preflight checks the user manager and local runtime tools. The dedicated
preflight checks the account layout and system manager without sudo. Both are
local checks. Provider access and relay admission are checked separately.

**Same account**

```sh
buzz-server doctor
buzz-server create assistant
```

**Dedicated account**

```sh
buzz-server doctor --account buzz-assistant
buzz-server create assistant --account buzz-assistant
```

The guided prompts ask for the name, optional profile details, operator public
key, relay and a single-line system prompt. Guided creation selects tool and
state paths for the chosen route. Empty optional answers use the shown defaults.
Ctrl+C, Ctrl+D or declining confirmation discards the choices.
On the dedicated route, authorize fixed system tools through ordinary operator
sudo when requested. The dedicated agent receives neither the management CLI
nor a sudo grant. To supply an explicit Goose path for the same-account route
or automate creation, see the [configuration-file method](#configuration-file-method).

### Configure Goose

For either route, keep the agent stopped and run this in your operator terminal:

```sh
buzz-server inference configure assistant
```

Goose uses a private per-agent HOME. The wizard runs at your login UID for a
same-account agent and at the bound target UID for a dedicated agent. Choose
Goose's mode explicitly. Auto executes enabled tools without per-call approval,
so use it only for an account and workspace whose permissions you authorize.
Modes that ask at runtime can block unattended responses. A cancelled wizard
may exit successfully. If you cancelled, rerun
`buzz-server inference configure assistant` and finish setup before starting.

### Custom OpenAI-compatible endpoint

On first run, choose **Manual Configuration** → **Search all providers...**.
Search for `OpenAI` and select **OpenAI** (described as “OpenAI models, including
OpenAI compatible ones”). The **Custom Providers** menu is not available on a
fresh Goose profile.

Enter the endpoint's API key when prompted for `OPENAI_API_KEY`; skip it only if
your endpoint does not require authentication. Answer **Yes** to advanced
settings. Set `OPENAI_BASE_URL` to the full API base URL (for example,
`https://models.example.com/v1`, without `/chat/completions`). Accept the
`OPENAI_HOST` default and leave `OPENAI_BASE_PATH` blank; the explicit base URL
takes precedence. Leave organization and project blank unless your provider
requires them. Choose **No** when asked to set `OPENAI_CUSTOM_HEADERS` unless
your provider needs them. Accept the `OPENAI_TIMEOUT` default, then select
your model and finish the wizard.

## Admit the key and start

Get the agent's hex public key from `data.identity` on either route:

```sh
buzz-server --json show assistant
```

On either route, `buzz-server show assistant` also prints the key as
`Buzz identity:`. Replace the placeholder below with its 64-character hex
value. Set it in each shell that runs the examples, or substitute the actual
public key for `"$AGENT_PUBKEY"` in each command.

```sh
AGENT_PUBKEY='paste-the-agent-hex-public-key-here'
```

On a membership-gated relay, give the administrator this public key and the
community name before the first start. Ask them to admit it as a regular relay
member under the existing policy. For example, on a compatible self-hosted
Buzz relay, the relay administrator can run this in its administrative
environment for the intended community:

```sh
buzz-admin add-member --pubkey "$AGENT_PUBKEY" --role member
```

This command admits the key without joining default channels. Invite claims
and some other administrator add paths may auto-join channels. Buzz Server
does not supply owner attestation (NIP-OA). A relay that requires it for this
agent is unsupported.

Start from the Linux operator login. You can do this before choosing a channel:

```sh
buzz-server start assistant
buzz-server status assistant
```

Profile publication, local runtime readiness and a reply are separate checks.
Relay admission and channel membership let the agent participate. Confirm a
reply in your thread before relying on it. If startup reports a failure, follow
its next step and reuse the same key when you retry.

Before adding the agent to a channel, verify its profile against the same
relay from the operator's Buzz identity:

```sh
buzz users get --pubkey "$AGENT_PUBKEY"
```

Check that the returned `pubkey` matches the agent key you copied. The signed
profile contains `is_agent: true`, which compatible Buzz Desktop versions use
to list the identity under **Agents** in **Channel members** and recognize it
in the mention picker.

## First reply

For the CLI examples, run `buzz channels list` against the same community and
copy the `channel_id` UUID for your test channel. Set it in the shell with
`AGENT_PUBKEY`, or substitute the UUID for `"$CHANNEL_ID"` in each command.

```sh
CHANNEL_ID='paste-the-test-channel-uuid-here'
```

1. In Buzz Desktop 0.5.30, select the same community and open your test channel.
   Open **Channel members**, then search for the existing agent under **Add
   people and agents** using the display name from the verified profile. It
   can appear as a regular member. Select **Add** on that result and confirm
   it appears in the channel members. Use `buzz channels members --channel
   "$CHANNEL_ID"` to verify the added key against `AGENT_PUBKEY`.
2. If the agent does not appear, confirm the community, your permission to add
   members, the exact key's relay admission, and the startup status. Repeat
   the profile lookup above. Use the CLI alternative if the exact profile is
   visible but Desktop cannot find it, or if several results have the same
   name. Use the existing public key for channel membership and keep the
   private key on the server.

   From the channel owner or administrator identity, with the channel UUID
   and the agent's public key:

   ```sh
   buzz channels add-member --channel "$CHANNEL_ID" --pubkey "$AGENT_PUBKEY" --role bot
   buzz channels members --channel "$CHANNEL_ID"
   ```

3. From the Buzz identity you gave `create`, mention the agent in the test
   channel and ask it to post a short reply in the same thread. Check that the
   reply comes from the agent you added and is attached to your message. To
   send and check with the Buzz CLI:

   ```sh
   buzz messages send --channel "$CHANNEL_ID" --mention "$AGENT_PUBKEY" \
     --content 'Please send "Hello from Buzz Server" as a reply in this thread.'
   buzz messages thread --channel "$CHANNEL_ID" --event EVENT_ID
   ```

   Use the event ID returned by `send`. If you only see your message, refresh the
   thread or repeat the thread query every 10–15 seconds for up to a minute
   before following [If no reply appears](#if-no-reply-appears). Allow longer
   if your model is known to be slow.

### If no reply appears

Wait and check the original thread as described above before sending another
mention.

1. Check the receiving relay and channel. Ask the relay operator to confirm
   that the agent key is admitted, and verify channel membership for that
   exact key.
2. Confirm the sender is eligible under the [sender policy](#sender-policy)
   and the mention targets the exact agent key.
3. Check `buzz-server status assistant` and fix startup failures first. For a
   same-account agent, `buzz-server logs assistant` shows sanitized lifecycle
   events. Dedicated agents report [last-observed outcomes](service-accounts.md#diagnose-a-silent-agent).
4. If provider access expired or was revoked, follow [Renew provider access](#renew-provider-access).
5. If the original thread still has no reply, retry once with a synthetic
   message asking the agent to post in the original thread. Publishing a reply
   requires the Buzz messaging tool. If it still fails, report CLI and Goose
   versions, lifecycle status and what each check established. Keep prompts,
   keys, provider settings and private logs private.

## Alternatives

### Configuration-file method

1. Run `install -d -m 700 "$HOME/.config/buzz-server"`. Set `umask 077`, then
   create `agent.json` and `prompt.txt` there with mode `0600`. Put the system
   prompt in `prompt.txt`. Keep private keys and credentials out of arguments,
   shell history and agent JSON.
2. Save the following as `agent.json`. Replace the relay URL, owner **public** key (64 lowercase hex characters), and absolute paths. The owner is the Buzz identity allowed to prompt this agent.

   ```json
   {
     "relay_url": "wss://relay.example.com",
     "owner": "REPLACE_WITH_OWNER_HEX_PUBKEY",
     "profile": {"name": "Server assistant", "about": "Managed on Linux", "avatar": ""},
     "prompt_file": "/home/operator/.config/buzz-server/prompt.txt",
     "harness": {"kind": "goose", "command": "/usr/local/bin/goose"},
     "provider": {"kind": "goose-managed"}
   }
   ```

   The dedicated route replaces the harness path with its pinned staged Goose;
   only the prompt path must exist under the operator login. The same-account
   route uses the supplied absolute Goose executable. On that route use
   `buzz-server doctor --config FILE` to check that executable.

3. Set `config="$HOME/.config/buzz-server/agent.json"` in your terminal, then
   run the command for your route.

   **Same account**

   ```sh
   buzz-server create assistant --config "$config"
   ```

   **Dedicated account**

   ```sh
   buzz-server create assistant --account buzz-assistant --config "$config"
   ```

4. [Configure Goose](#configure-goose), then continue with
   [Admit the key and start](#admit-the-key-and-start) to get the public key.

### Sender policy

Guided creation uses and displays `owner-only`. Scripted creation can add this
optional object to the [configuration JSON](#configuration-file-method):

```json
{"sender_policy": {"mode": "owner-only", "allowlist": []}}
```

| Mode | Eligible senders in joined channels |
| --- | --- |
| `owner-only` | The configured operator and agents with a verified attestation to that same owner. |
| `allowlist` | The operator, verified same-owner agents and specifically listed human or agent keys. Include 1–128 distinct public keys as 64 lowercase hexadecimal characters. |
| `anyone` | Any eligible sender. Select this explicitly with an empty `allowlist`. |

Relay admission, channel membership and mention routing still apply. Buzz ACP
admits only the operator and verified same-owner agents in DMs, including in
`allowlist` and `anyone` mode. This setting never issues or changes an owner
attestation. See the [pinned Buzz ACP sender gate](https://github.com/block/buzz/blob/desktop-v0.5.30/crates/buzz-acp/src/lib.rs#L569-L590).

For example, create a coordinator and a worker with their own identities. In
the worker's configuration, select `allowlist` and put the coordinator's public
hex key in the `allowlist` array. Add both agents to a shared channel and have
the coordinator mention the worker there. The worker still rejects other
unlisted senders outside the implicit operator and same-owner access.

Run `buzz-server configure worker --config "$config"` with the full definition
to change the policy in place. Omission keeps the previous selection when
reconfiguring and defaults to `owner-only` for new agents. Status reports the
selected mode and the size of the allowlist. Dedicated agents with an older
worker need `buzz-server worker upgrade worker` before selecting a custom
policy. Select `owner-only` before rolling back to a worker that predates this
setting.

## Operate and recover

- `list` shows agents bound to this operator and existing same-account agents. `logs` is available for same-account lifecycle events only. For automation put `--json` **before** the subcommand; successes go to stdout and errors to stderr.
- After editing the JSON or prompt, run `buzz-server configure assistant --config "$config"`. This replaces the configuration, preserves the agent key, and restarts an agent set to run. Omit the sender policy to keep the selected policy. An intentionally stopped agent stays stopped. This command does not run Goose setup.
- `start` enables the unit in its manager. Same-account services use
  `systemd --user` and depend on the operator's user manager. Dedicated services
  use the system manager. For logout and reboot verification see
  [issue #4](https://github.com/j2-ms/buzz-server/issues/4).
  A new dedicated service waits for profile publication and a locally running
  worker. The CLI observes
  startup for up to ten seconds while systemd can continue trying in the
  background. Systemd allows four starts in five minutes, including restarts
  after runtime crashes, and stops sooner on a classified refusal. Use
  `buzz-server status assistant` to see
  the latest stage, `buzz-server stop assistant` to cancel, or `buzz-server
  start assistant` after correcting a failure.
- `buzz-server delete assistant` verifies stop and removes the managed unit and
  agent definition. For a same-account agent, it retains the key, prompt,
  Goose credentials and work in the operator's agent state directory (by
  default under `${XDG_DATA_HOME:-$HOME/.local/share}/buzz-server/agents`).
  For a dedicated agent, it retains the private state and staged worker/tools
  under the bound account's home, plus the operator binding and removal proof.
  The retained data reserves the ID on both routes. Linux accounts and past
  Buzz messages are retained.
- Updating or reinstalling the operator CLI does not update staged dedicated workers or tools. There is no fleet upgrade mechanism. Keep the operator state/bindings and do not edit units by hand. CLI uninstall refuses dedicated bindings until delete is verified; incomplete create/delete operations must be retried first. Same-account `start` still refreshes its user unit after a CLI reinstall.
- Loaded unit overrides are not supported. A conflict blocks management commands, including `stop`, `status`, and `show`, and can make `list` fail even when other agents are unaffected. Shared overrides in `service.d/` or `buzz-server-.service.d/` on the applicable manager's search path can affect every managed agent. Inspect the conflicts with `systemctl --system cat UNIT` using the exact `unit` in JSON status; use `systemctl --user cat buzz-server-<id>.service` for same-account units. Ask the administrator responsible for the override to remove the conflict, reload that manager, and retry `stop` before deleting anything. A failed stop is not proof the agent is down.

### Renew provider access

From the operator login, explicitly stop the dedicated agent before repeating
Goose's native setup. Verify that systemd reports it stopped. Use the same
agent ID throughout.

```sh
buzz-server stop assistant
buzz-server status assistant
buzz-server inference configure assistant
```

In Goose, choose **Configure Providers**, select the existing or replacement
provider, and complete its normal credential entry or sign-in. Enter the new
provider authorization in Goose's terminal. Goose keeps it in the dedicated
account's private per-agent HOME. Keep credentials from your own login out of
the agent's setup. The [Goose state layout](#goose-setup-and-private-state)
describes the access boundary.

If you cancel or Goose reports an error, keep the agent stopped and retry
setup. Some Goose cancellation paths exit cleanly. A clean exit alone gives
no evidence that Goose stored new authorization or that the provider accepts
it. After completing provider setup, run `buzz-server start assistant` and
[read back a new reply](#first-reply) from the original Buzz public key. The
existing identity, profile, channel memberships and working files carry over.

## Goose setup and private state

Goose is the supported harness. Scripted configuration requires
`harness: {"kind": "goose", "command": "/absolute/path/to/goose"}` and
`provider: {"kind": "goose-managed"}`. Goose owns provider selection, model and
credentials. Do not add `model` or `env_file` to agent JSON or create a provider
environment file. A new agent can be created before Goose is configured.

If you tried an older, unreleased build, `status` shows its old provider mode
as `obsolete`. That mode cannot launch. You can still stop or remove the
agent, or replace its configuration without creating a new identity. Run
`buzz-server stop assistant`, then `buzz-server configure assistant --config /absolute/path/to/current.json`,
where `current.json` uses the schema above. This leaves the agent stopped so
you can set up Goose before starting it. Old credential files are not read,
imported, or removed.

To change Goose setup for an existing agent, explicitly run
`buzz-server stop assistant`, then `buzz-server inference configure assistant`.
The wizard requires stopped intent and supervisor proof of no worker, including
an empty service cgroup. It preserves the Buzz identity and leaves the agent
stopped; use `buzz-server start assistant` when ready.

Run the wizard from the operator login for a dedicated agent, or under the service account for the same-account alternative. stdin, stdout and
stderr must all be terminals; `--json` is refused before any Goose command.
Goose runs exactly `configure`, with the same private HOME, XDG config/data paths
and working directory used for that agent's ACP launch. Same-account setup may
inherit TERM and COLORTERM; dedicated setup starts with the fixed clean target
environment. Provider, credential, author, loader and locale overrides from
the caller are excluded. The wizard receives no Buzz identity key. Runtime
receives its Buzz key for ACP, while the profile helper remains scrubbed of
provider credentials and Goose settings. No model flag or GOOSE_PROVIDER /
GOOSE_MODEL override is supplied in runtime.

`GOOSE_DISABLE_KEYRING=1` is pinned for setup and runtime. Goose 1.60.2
uses its native mode-0600 secret file in this mode; credentials remain **at rest**
in per-agent private state rather than a desktop keyring. Setup uses child umask
077 and private mode-0700 directories under `agents/<id>/home`; working files use
`agents/<id>/work`. This isolates agent state; it is not a sandbox. Goose and its
tools retain the service account's permissions. Deletion retains this private
state, including Goose credentials.

Buzz Server treats Goose settings as opaque: it does not read YAML, report
settings or secrets, or record wizard output in lifecycle logs. Status
reports provider `goose-managed` and model `null`; Goose owns provider and model.
The terminal is connected directly to upstream Goose, so avoid recording or
sharing its session. Use Goose's normal cancellation controls. Nonzero exits
and interrupts return a static error and do not rewrite the Buzz definition or
reset settings. Goose itself may have written partial changes before exiting.
A zero exit reports only that setup exited; it does not prove configuration was
completed, credentials work, or a Buzz reply is possible. Creation and local
start checks do not establish provider readiness either.

The wizard holds the existing state-wide mutation lock and, for an installed
CLI, the shared installation lock throughout setup and child cleanup. Other
management commands using that state wait; release mutation waits or reports
busy. On Ctrl+C or SIGTERM, Buzz Server terminates and reaps the wizard before
releasing locks, ignoring repeated interrupts during cleanup (five seconds
before killing an unresponsive child). Do not forcibly kill the managing CLI:
SIGKILL or a host failure cannot run cleanup, and an orphan wizard must be
resolved before further management. No account changes are performed.
`runtime_started` for a new dedicated worker reflects its local systemd
readiness signal after profile publication and a successful local process
check. Test the reply separately in Buzz. Existing v0.2 dedicated units keep
their original retry policy and report local readiness as unobserved. No reboot
or live dedicated-account verification is claimed by the offline suite.
