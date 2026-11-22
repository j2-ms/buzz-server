# Buzz Server

Run and manage [Buzz](https://github.com/block/buzz) agents on a headless Linux host. You can install, configure, start, stop, inspect, and remove agents without running Buzz Desktop. Buzz Server manages the Linux services; it does not replace the relay or the agent runtime.

## First install from a public release

Ubuntu 24.04 amd64 is the reference platform. This path requires a published
stable release with assets in `j2-ms/buzz-server`.
Use an existing unprivileged Linux account and `/usr/bin/python3` 3.12+.
No GitHub CLI installation, login or credential file is needed.

Obtain the reviewed **install-buzz-server.py** asset from the
[public stable GitHub release](https://github.com/j2-ms/buzz-server/releases/latest).
Save it in a private directory and run the local file:

```sh
/usr/bin/python3 -I ./install-buzz-server.py --prefix "$HOME/.local/buzz-server-cli"
```

No clone, pip, checksum copying or asset IDs are needed. The bootstrap is trusted
through this exact repository's GitHub Release and its publishers; it has no
independent signature. It anonymously downloads the stable app over direct,
verified HTTPS and checks release metadata, SHA-256, byte limits, exact size and
embedded version before executing the app. Fresh installations enable interactive
update offers by default. These offers require consent before downloading or
activating an update; explicit `update` remains available.

By default, installation downloads only verified Buzz Server. When tools are missing
and stdin/stdout/stderr are terminals, it shows the existing tools, missing tools,
roles and pinned download destinations, then asks once (default **no**).
`--install-dependencies` explicitly opts in without a prompt; `--skip-dependencies`
suppresses the prompt. `--json` never prompts. Optional private downloads support
Ubuntu 24.04 amd64 initially: only Buzz CLI, buzz-acp and Goose are acquired from
their upstream releases, with pinned sizes and SHA-256 checks. No desktop package
installation, accounts or service changes occur.

Install status is not provider or relay readiness. Use the reported absolute Goose
path as `harness.command` in agent JSON passed with `--config`. Next run
`"$HOME/.local/buzz-server-cli/bin/buzz-server" doctor` (or `doctor --config FILE`
for an existing configured Goose path). See the [release guide](docs/releases.md)
for private tools, retry, custom directories, offline installation and removal,
then the [agent setup guide](docs/quickstart.md).

## Install from source

Use Linux with Python 3.12+, systemd with cgroup v2, and OpenSSL with secp256k1 support. The reference test environment was Ubuntu 24.04 LTS; see the [verification limits](docs/testing.md#what-the-tests-prove). Use an existing [unprivileged service account](docs/service-accounts.md) with a working `systemctl --user`.

Install `buzz` and [`buzz-acp`](https://github.com/block/buzz/tree/desktop-v0.5.30/crates/buzz-acp) from a trusted [Buzz release](https://github.com/block/buzz/releases/tag/desktop-v0.5.30) into `/usr/bin` or `/usr/local/bin`. Install [Goose](https://github.com/aaif-goose/goose/releases/tag/v1.60.2) with `goose acp` support. Each agent uses upstream `goose configure` to select its provider and model and store credentials in private Goose state; real Buzz-to-Goose replies remain gated by the [verification limits](docs/testing.md#what-the-tests-prove).

Clone the [Buzz Server repository](https://github.com/j2-ms/buzz-server) and enter it. Run as the service account:

```sh
python3 scripts/install.py --prefix "$HOME/.local/buzz-server-cli"
export PATH="$HOME/.local/buzz-server-cli/bin:$PATH"
buzz-server doctor
```

Installation needs neither root nor pip. `doctor` checks local prerequisites without contacting a relay.

## Run your first agent

Follow the [agent setup guide](docs/quickstart.md) to configure credentials, create an agent, grant it relay and channel access, and start it. The relay must be able to admit a new agent without mandatory NIP-OA owner attestation. Tool calls run with the Linux account's permissions and without interactive approval; use an account with only the access the agent needs.

Start with `buzz-server --help` to see all commands. See [Contributing](CONTRIBUTING.md) for development and [Vision](VISION.md) for the scope of future work.

## License

Buzz Server is licensed under [Apache-2.0](LICENSE).
