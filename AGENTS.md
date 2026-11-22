# Repository instructions

- Read [README](README.md), [CONTRIBUTING](CONTRIBUTING.md), and the [glossary](UBIQUITOUS_LANGUAGE.md) before changing their scope or terminology.
- Keep Buzz Server a standalone, standard-library Python CLI for headless Linux. Use the upstream [Buzz](https://github.com/block/buzz) binaries and [Goose](https://github.com/aaif-goose/goose). Do not add a Desktop plugin, migration, relay provisioning, or privileged account management to a routine CLI change.
- Preserve agent identity on reconfiguration and private state on deletion. Keep user-service actions fail-closed, and preserve the versioned JSON/stdout/stderr contract. Do not log or commit credentials or private prompts.
- Use an isolated worktree and follow [the test guide](docs/testing.md). Use synthetic fixtures, run shared-host jobs serially with resource and time limits, and do not contact production relays or model APIs without explicit approval.
- Submit verified changes for independent review. Do not infer permission to merge, publish a release, or deploy from repository access or a passing test.
