# Buzz Server

Agent host management for [Buzz](https://github.com/block/buzz). Run agents on a Linux host without Buzz Desktop. Bring a Buzz relay and a model supported by [Goose](https://github.com/aaif-goose/goose).

## Run an agent from your operator login

Use Ubuntu 24.04 or 26.04 (amd64), Bash, Python 3.12+ and curl. You need `sudo`
for administration and an existing [dedicated agent account](docs/service-accounts.md),
such as `buzz-assistant`. Install the CLI from **your own login**:

```sh
(set -o pipefail; curl --proto '=https' -fsSL https://github.com/j2-ms/buzz-server/releases/latest/download/install-buzz-server.py | /usr/bin/python3 -I -)
```

If the installer offers missing runtime tools, press Enter to install them.
If it offers to remove group and other write bits from your `~/.local/bin`,
check the proposed change before accepting.
If your shell can't find `buzz-server`, open a fresh login shell. Have your
[relay URL and Buzz public key](docs/quickstart.md) ready, then run:

```sh
buzz-server doctor --account buzz-assistant
buzz-server create assistant --account buzz-assistant
buzz-server inference configure assistant
```

`create` asks for your Buzz public key, relay, and the agent's profile and prompt.
The CLI handles paths and permissions; the agent gets neither the management
CLI nor `sudo`. Follow the quickstart to
[admit the key if required](docs/quickstart.md#admit-the-key-and-start), start the
agent, then [add it to a channel and check a reply](docs/quickstart.md#first-reply).
For a custom OpenAI-compatible endpoint, see [Goose provider setup](docs/quickstart.md#custom-openai-compatible-endpoint).

See the [account guide](docs/service-accounts.md) for isolation, retained data and
same-account use; the [release guide](docs/releases.md) for installer verification,
automation and recovery; and [Contributing](CONTRIBUTING.md) and [Vision](VISION.md)
for development and future scope.

Buzz Server is licensed under [Apache-2.0](LICENSE). Buzz and Goose are separately released and are not bundled in Buzz Server's release assets.
