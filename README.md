# Buzz Server

Agent host management for [Buzz](https://github.com/block/buzz). Run agents on a Linux host without Buzz Desktop. Bring a Buzz relay and a model supported by [Goose](https://github.com/aaif-goose/goose).

## Run an agent from your operator login

Choose how to run the agent. In both cases, install and run the CLI from your
own unprivileged operator login.

- **Same account.** Uses your login and its `systemd --user` manager. You need a
  working user manager, with no second account or sudo. The agent shares your
  login's access to files and other resources.
- **Dedicated account (recommended for isolation).** Uses a system unit and a
  [pre-existing unprivileged account](docs/service-accounts.md) such as
  `buzz-assistant`. Privileged actions require explicit operator sudo.
  `doctor --account` runs without sudo.

For the installation below, use Ubuntu 24.04 or 26.04 (amd64), Bash,
`/usr/bin/python3` 3.12+ and curl.

```sh
(set -o pipefail; curl --proto '=https' -fsSL https://github.com/j2-ms/buzz-server/releases/latest/download/install-buzz-server.py | /usr/bin/python3 -I -)
```

If the installer offers missing runtime tools, press Enter to install them.
If it offers to remove group and other write bits from your `~/.local/bin`,
check the proposed change before accepting.
If your shell can't find `buzz-server`, see the [launcher and PATH instructions](docs/releases.md#find-the-launcher).
Have your [relay URL and Buzz public key](docs/quickstart.md) ready. Run the
pair for your chosen route, and continue after its preflight passes.

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

`create` asks for your Buzz public key, relay, and the agent's profile and prompt.
For either route, configure Goose for the stopped agent.

```sh
buzz-server inference configure assistant
```

Follow the quickstart to
[admit the key if required](docs/quickstart.md#admit-the-key-and-start), start the
agent, then [add it to a channel and check a reply](docs/quickstart.md#first-reply).
For a custom OpenAI-compatible endpoint, see [Goose provider setup](docs/quickstart.md#custom-openai-compatible-endpoint).

See the [account guide](docs/service-accounts.md) for isolation, retained data,
[worker upgrades and approved prompts](docs/service-accounts.md#maintain-an-existing-agent).
See the [release guide](docs/releases.md) for installer verification, automation
and recovery, and [Contributing](CONTRIBUTING.md) and [Vision](VISION.md) for
development and future scope.

## License and upstream tools

Buzz Server is licensed under [Apache-2.0](LICENSE). Buzz and Goose are separately released and are not bundled in Buzz Server's release assets.
