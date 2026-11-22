# Contributing

Use a topic branch and open a pull request for review. Describe the operator-visible change, the checks you ran, and what remains untested. File ordinary bugs in [GitHub Issues](https://github.com/j2-ms/buzz-server/issues) with versions, steps, and sanitized output.

For a security concern, use private vulnerability reporting under the repository's Security tab if available. Otherwise, open an issue asking for a private contact without describing the finding. Do not post credentials, private prompts, or vulnerability details in an issue or pull request.

## Develop and verify

Use Python 3.12+ on Linux. The CLI uses the standard library; there is no dependency installation or build step. See the [release guide](docs/releases.md) for local tools and the reference test environment.

```sh
python3 -B -m unittest discover -s tests -v
```

Add a regression test for behavior changes. For changes to installation, the worker, or systemd, also run the [disposable lifecycle test](docs/testing.md) in an isolated Linux environment. Use synthetic identities and data. Keep lifecycle, real relay, and live model results separate in the PR.

## Code map

- [`cli.py`](buzz_server/cli.py) handles commands and output. [`config.py`](buzz_server/config.py), [`storage.py`](buzz_server/storage.py), and [`identity.py`](buzz_server/identity.py) own configuration and local state.
- [`supervisor.py`](buzz_server/supervisor.py) manages user units. [`runtime.py`](buzz_server/runtime.py) launches [`buzz-acp`](https://github.com/block/buzz/tree/desktop-v0.5.30/crates/buzz-acp), which connects to [Goose](https://github.com/aaif-goose/goose) over ACP.
- See the [vision](VISION.md), [glossary](UBIQUITOUS_LANGUAGE.md), and [agent instructions](AGENTS.md) before changing the project boundaries.
