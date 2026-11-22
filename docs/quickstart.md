# Set up an agent

Start with the [installation instructions](../README.md) and use the same
[unprivileged service account](service-accounts.md) throughout. If you used the
default install prefix, make the commands below available in this shell:

```sh
export PATH="$HOME/.local/buzz-server-cli/bin:$PATH"
buzz-server doctor
```

Use your chosen prefix instead if you changed it. `doctor` checks local
prerequisites, not relay or provider access.

Before creating an agent, confirm that your relay accepts a new public key
without owner attestation (NIP-OA). Ask the relay operator if you don't know the
admission policy. An open relay can accept the key directly. On a managed relay,
ask the operator whether they can explicitly admit the generated key under the
existing policy. If their only supported route is an owner attestation, stop:
Buzz Server does not produce it. Do not change relay policy just to run the
quickstart. Use `wss://`; plain `ws://` is only allowed for literal loopback
addresses.

On a machine where you already use Buzz as the intended owner, run `buzz users get`
with that identity against the same relay. Copy `pubkey` from your returned
profile: 64 lowercase hex characters, not an `npub` or `nsec`. If no profile is
returned, confirm the configured identity and relay, or obtain the hex public
key from your Buzz client or relay operator. Never transfer your private key to
the server to perform this step.

## Configure and start

1. Run `install -d -m 700 "$HOME/.config/buzz-server"`. Set `umask 077`, then create `agent.json` and `prompt.txt` in that directory with mode `0600`. Put the system prompt in `prompt.txt`. Do not put keys in command arguments, shell history, or agent JSON.
2. Save the following as `agent.json`. Replace the relay URL, owner **public** key (64 lowercase hex characters), and absolute paths. The owner is the Buzz identity allowed to prompt this agent.

   ```json
   {
     "relay_url": "wss://relay.example.com",
     "owner": "REPLACE_WITH_OWNER_HEX_PUBKEY",
     "profile": {"name": "Server assistant", "about": "Managed on Linux", "avatar": ""},
     "prompt_file": "/home/agent/.config/buzz-server/prompt.txt",
     "harness": {"kind": "goose", "command": "/usr/local/bin/goose"},
     "provider": {"kind": "goose-managed"}
   }
   ```

3. Create the stopped agent and copy its public key from the `Buzz identity:`
   line in `show` (`identity` in JSON output). Use this key as `AGENT_PUBKEY`
   below:

   ```sh
   config="$HOME/.config/buzz-server/agent.json"
   buzz-server create assistant --config "$config"
   buzz-server show assistant
   ```

4. Run Goose setup in a terminal under the same service account. Complete Goose's provider, model and credential prompts:

   ```sh
   buzz-server inference configure assistant
   ```

   If the relay requires explicit admission, give its operator the agent's
   **public** key (`AGENT_PUBKEY`); never send the private key. Admission and
   channel membership are separate. Use a test channel. From the channel-owner
   or administrator identity on your Buzz client (not the Linux service
   account), add the agent:

   ```sh
   buzz channels add-member --channel "$CHANNEL_ID" --pubkey "$AGENT_PUBKEY" --role bot
   buzz channels members --channel "$CHANNEL_ID"
   ```

   Use your channel's UUID as `CHANNEL_ID`. Check that the agent key appears
   before you start it; Buzz Server cannot grant any of these rights.

   ```sh
   buzz-server start assistant
   buzz-server status assistant
   buzz-server logs assistant --lines 20
   ```

5. From the configured owner identity, mention the agent in the test channel and
   ask it to send a short reply **in the same thread**. In the CLI, an explicit
   mention avoids relying on name lookup:

   ```sh
   buzz messages send --channel "$CHANNEL_ID" --mention "$AGENT_PUBKEY" \
     --content 'Please send "Hello from Buzz Server" as a reply in this thread.'
   ```

   Check that the reply is from the agent key and is attached to that message.
   With the CLI, use `buzz messages thread --channel "$CHANNEL_ID" --event EVENT_ID`,
   where `EVENT_ID` is the ID returned by the send. A running worker or ordinary
   model text isn't a posted reply. See the [verification limits](testing.md#what-the-tests-prove).

### If no reply appears

1. Check the receiving relay and channel. Ask the operator to confirm that the
   agent key is admitted, and verify channel membership for that exact key.
2. Confirm you sent the mention from the hex public key in `owner`, and that the
   mention targeted the agent's public key, not a similarly named account.
3. Check `buzz-server status assistant` and `buzz-server logs assistant --lines 20`.
   Fix startup failures first. Lifecycle logs do not expose model output or
   credentials. To redo provider setup, run `buzz-server stop assistant`, then
   `buzz-server inference configure assistant`, then `buzz-server start assistant`.
4. If steps 1–3 check out, retry once with a synthetic message explicitly asking
   the agent to post in the original thread. Plain model text alone is not
   published; the harness needs to use the Buzz messaging tool path. If it still
   fails, report the CLI/Goose versions, lifecycle status and what each check
   established. Do not share prompts, keys, provider settings, or private logs.

## Operate and recover

- `list` shows agents managed by this account. `logs` shows lifecycle events, not raw agent output. For automation put `--json` **before** the subcommand; successes go to stdout and errors to stderr.
- After editing the JSON or prompt, run `buzz-server configure assistant --config "$config"`. This replaces the full configuration, preserves the agent key, and restarts an agent set to run. An intentionally stopped agent stays stopped. This command does not run Goose setup.
- `start` enables the user service; `stop` disables it. If `start` cannot verify startup within ten seconds, it leaves the agent set to run and systemd retries after worker failures. Use `buzz-server stop assistant` to cancel retries.
- `buzz-server delete assistant` stops and removes the agent from management. It retains the key and private state under `${XDG_DATA_HOME:-$HOME/.local/share}/buzz-server`; the ID cannot be reused while those files remain. It does not remove the Linux account or Buzz history.
- After reinstalling to a new prefix, use the new CLI against the same state and unit directories. The next `start` refreshes the managed unit. Do not edit the unit by hand.
- Loaded unit overrides are not supported. A conflict blocks management commands, including `stop`, `status`, and `show`, and can make `list` fail even when other agents are unaffected. Shared overrides in `service.d/` or `buzz-server-.service.d/` on the user manager's search path can affect every managed agent. Inspect the conflicts with `systemctl --user cat buzz-server-<id>.service`. Ask the administrator responsible for the override to remove the conflict, run `systemctl --user daemon-reload`, and retry `stop` before deleting anything. A failed stop is not proof the agent is down.

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

Run the wizard in a terminal under the same service account. stdin, stdout and
stderr must all be terminals; `--json` is refused before any Goose command.
Goose runs exactly `configure`, with the same private HOME, XDG config/data paths
and working directory used for that agent's ACP launch. Only TERM and COLORTERM
are inherited; provider, credential, author, loader and locale overrides from
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
