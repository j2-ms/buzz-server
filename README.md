# Buzz Server

Agent host management for [Buzz](https://github.com/block/buzz). Run agents on a Linux host without Buzz Desktop. Bring a Buzz relay and a model supported by [Goose](https://github.com/aaif-goose/goose).

## Install

In Bash on Linux with Python 3.12+ and curl:

```sh
(set -o pipefail; curl --proto '=https' -fsSL https://github.com/j2-ms/buzz-server/releases/latest/download/install-buzz-server.py | /usr/bin/python3 -I -)
```

Run `buzz-server doctor`. If your shell can't find it, open a fresh login shell and try again. See the [release guide](docs/releases.md) for other shells and how the bootstrap is verified.

If runtime tools are missing, the installer lists them and offers pinned downloads (default Yes). Declining still installs the CLI. Managed downloads allow Ubuntu 24.04 and 26.04 amd64; compatibility of the pinned binaries on 26.04 has not been verified. For prerequisites, noninteractive flags, custom paths, updates and recovery, see the [release guide](docs/releases.md). `doctor` checks local prerequisites, not relay admission or provider credentials.

For agent configuration and operation, see the [agent setup guide](docs/quickstart.md). See [Contributing](CONTRIBUTING.md) for development and [Vision](VISION.md) for future scope.

Buzz Server is licensed under [Apache-2.0](LICENSE). Buzz and Goose are separately released and are not bundled in Buzz Server's release assets.
