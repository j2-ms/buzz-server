# Set up an agent

Start with the [installation instructions](../README.md) under your operator
login and an existing [dedicated account](service-accounts.md), for example
`buzz-assistant`. Run the `buzz-server` commands from your operator login.
Open a fresh login shell if it can't find `buzz-server` after installation:

```sh
buzz-server doctor --account buzz-assistant
```

`doctor` checks the local account and system manager, not relay or provider access.

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

Create a stopped agent from the operator login:

```sh
buzz-server create assistant --account buzz-assistant
buzz-server show assistant
buzz-server inference configure assistant
```

The guided prompts ask for the name, optional profile details, operator public
key, relay and a single-line system prompt. Account selection is explicit in
`--account`; paths and pinned tools are automatic. Empty optional answers use
shown defaults. Ctrl+C, Ctrl+D or declining confirmation discards the choices.
Authorize fixed system tools through ordinary sudo when requested; do not run
the CLI itself as root or install it under the agent account. Goose setup runs
at the target UID with that agent's private HOME, not the operator's settings.
It does not grant the agent sudo. Choose Goose's mode explicitly: auto executes
enabled tools without per-call approval, so use it only for an account and
workspace whose permissions you authorize. Modes that ask at runtime can block
unattended responses. A cancelled wizard may exit successfully without readiness;
rerun `buzz-server inference configure assistant` and finish setup before starting.
Prove readiness with the [First reply](#first-reply) check below.

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
takes precedence. Skip organization, project and header settings unless your
provider requires them. Accept the `OPENAI_TIMEOUT` default, then select your
model and finish the wizard.

### First reply

1. Copy the public key from `Buzz identity:` in `buzz-server show assistant`.
   If the relay requires explicit admission, give its operator that public key.
2. Add the agent to a test channel in Buzz. Admission and channel membership
   are separate. If administering the channel with the Buzz CLI, run this from
   the channel-owner or administrator identity, with the channel UUID and the
   agent's public key:

   ```sh
   buzz channels add-member --channel "$CHANNEL_ID" --pubkey "$AGENT_PUBKEY" --role bot
   buzz channels members --channel "$CHANNEL_ID"
   ```

3. Start the agent from your Linux operator login:

   ```sh
   buzz-server start assistant
   buzz-server status assistant
   ```

4. From the Buzz identity you gave `create`, mention the agent in the test
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
   before treating it as a no-reply case. This is a first check, not a response
   deadline; allow longer if your model is known to be slow. A running service
   is not a posted reply.

## Alternatives

For the nonisolated same-account alternative, omit `--account` during creation
and use `doctor` without `--account`. Existing user-service data and scripted
commands retain their behavior. See the [account guide](service-accounts.md).

### Configuration-file method

1. Run `install -d -m 700 "$HOME/.config/buzz-server"`. Set `umask 077`, then create `agent.json` and `prompt.txt` in that directory with mode `0600`. Put the system prompt in `prompt.txt`. Do not put keys in command arguments, shell history, or agent JSON.
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
   alternative uses the supplied absolute Goose path as before.

3. Create the stopped agent and copy its public key from the `Buzz identity:`
   line in `show` (`identity` in JSON output). Use this key as `AGENT_PUBKEY`
   below:

   ```sh
   config="$HOME/.config/buzz-server/agent.json"
   buzz-server create assistant --account buzz-assistant --config "$config"
   buzz-server show assistant
   ```

4. Run Goose setup in a terminal from the operator login. The CLI changes to the bound target UID with a clean environment. Complete Goose's provider, model and credential prompts:

   ```sh
   buzz-server inference configure assistant
   ```

5. Continue with [First reply](#first-reply).

### If no reply appears

If you haven't already waited and checked the original thread as described
above, do that before sending another mention.

1. Check the receiving relay and channel. Ask the operator to confirm that the
   agent key is admitted, and verify channel membership for that exact key.
2. Confirm you sent the mention from the hex public key in `owner`, and that the
   mention targeted the agent's public key, not a similarly named account.
3. Check `buzz-server status assistant`. Dedicated lifecycle logs are not exposed by this version.
   Fix startup failures first. Lifecycle logs do not expose model output or
   credentials. To redo provider setup, run `buzz-server stop assistant`, then
   `buzz-server inference configure assistant`, then `buzz-server start assistant`.
   If Goose already has a configuration, choose **Configure Providers** instead
   of **Manual Configuration** and continue with the provider selection above.
4. If steps 1–3 check out and the original thread still has no reply, retry once with a synthetic message explicitly asking
   the agent to post in the original thread. Plain model text alone is not
   published; the harness needs to use the Buzz messaging tool path. If it still
   fails, report the CLI/Goose versions, lifecycle status and what each check
   established. Do not share prompts, keys, provider settings, or private logs.

## Operate and recover

- `list` shows agents bound to this operator and existing same-account agents. `logs` is available for same-account lifecycle events only. For automation put `--json` **before** the subcommand; successes go to stdout and errors to stderr.
- After editing the JSON or prompt, run `buzz-server configure assistant --config "$config"`. This replaces the full configuration, preserves the agent key, and restarts an agent set to run. An intentionally stopped agent stays stopped. This command does not run Goose setup.
- `start` enables the dedicated system service (or the same-account user service); `stop` disables it. If `start` cannot verify startup within ten seconds, it leaves the agent set to run and systemd retries after worker failures. Use `buzz-server stop assistant` to cancel retries.
- `buzz-server delete assistant` verifies stop and removes the system unit and target definition. It retains the key, credentials and work under the bound target home, plus the private operator binding/removal proof. The ID stays reserved. It does not remove the Linux account or Buzz history. Same-account retained data stays under `${XDG_DATA_HOME:-$HOME/.local/share}/buzz-server`.
- Updating or reinstalling the operator CLI does not update staged dedicated workers or tools. There is no fleet upgrade mechanism. Keep the operator state/bindings and do not edit units by hand. CLI uninstall refuses dedicated bindings until delete is verified; incomplete create/delete operations must be retried first. Same-account `start` still refreshes its user unit after a CLI reinstall.
- Loaded unit overrides are not supported. A conflict blocks management commands, including `stop`, `status`, and `show`, and can make `list` fail even when other agents are unaffected. Shared overrides in `service.d/` or `buzz-server-.service.d/` on the applicable manager's search path can affect every managed agent. Inspect the conflicts with `systemctl --system cat UNIT` using the exact `unit` in JSON status; use `systemctl --user cat buzz-server-<id>.service` for same-account units. Ask the administrator responsible for the override to remove the conflict, reload that manager, and retry `stop` before deleting anything. A failed stop is not proof the agent is down.

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
resolved before further management. No account changes are performed. Dedicated service activity is observed through the system manager; status keeps `runtime_started` and `buzz_response_verified` false because service activity alone cannot verify either. No reboot or live dedicated-account verification is claimed by the offline suite.
