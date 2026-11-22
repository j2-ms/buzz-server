# Buzz Server

Run and manage [Buzz](https://github.com/block/buzz) agents on a headless Linux host without running Buzz Desktop. Buzz Server manages the Linux services; v0.1 supports [Goose](https://github.com/aaif-goose/goose) as its only agent runtime. Bring a Buzz relay you can use and a model supported by Goose.

## First install from a public release

Ubuntu 24.04 amd64 is the reference platform. Use `/usr/bin/python3` 3.12+
and an existing [unprivileged service account](docs/service-accounts.md)
with a working `systemctl --user`. If agents should keep running after logout
or start at boot, ask your administrator to enable linger for that account.
No GitHub CLI, login or credential file is needed.

Download **install-buzz-server.py** from the **Assets** section of the
[latest stable release](https://github.com/j2-ms/buzz-server/releases/latest).
Save it in a private directory, open a terminal there as the service account,
and run:

```sh
/usr/bin/python3 -I ./install-buzz-server.py --prefix "$HOME/.local/buzz-server-cli"
```

To run an agent you need `buzz`, `buzz-acp`, and `goose`. If any are missing,
the interactive installer shows what it would download and asks once (default
**no**). On a fresh Ubuntu 24.04 amd64 account, accept to install the pinned
upstream tools into private storage. If you decline, Buzz Server is still installed;
install the missing tools before continuing. For noninteractive install, use
`--install-dependencies` or `--skip-dependencies` to choose explicitly.
When setting up the agent, use the absolute Goose path printed by the installer
as `harness.command` in the agent's configuration.

You now have the manager and any tools you chose to install, **not a configured or
running agent**. Check local prerequisites, then follow the
[agent setup guide](docs/quickstart.md) to configure Goose, grant access, and verify
that the agent replies in the original test thread:

```sh
"$HOME/.local/buzz-server-cli/bin/buzz-server" doctor
```

Use your chosen prefix if it differs. `doctor` does not check relay admission or
provider credentials. For a retry, a custom Goose path, offline installation,
or removal, see the [release guide](docs/releases.md).

No clone, pip or checksum copying is needed. Trust the bootstrap from this
repository's GitHub Release; it has no independent signature. It verifies the
app's HTTPS download, exact size, SHA-256 and embedded version before running it.
Fresh installs offer updates before an interactive command; they activate only
with your consent. Scripts and JSON output never prompt.

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

## Credits and license

Buzz Server is licensed under [Apache-2.0](LICENSE). It uses the separately
released [Buzz CLI and ACP](https://github.com/block/buzz) and
[Goose](https://github.com/aaif-goose/goose); their binaries are not bundled in
Buzz Server's release assets.
