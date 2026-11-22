# Linux service accounts

The service runs as the Linux account that runs `buzz-server`, under that account's `systemd --user` manager. The `owner` field in the agent configuration is a **Buzz public key**, not a Linux account. The generated agent key is separate from both.

For a dedicated account, ask the host administrator to provide an existing, unprivileged account with a private home, an authorized login path and a working user manager. Use an authorized SSH login or console session **as that account** for the entire installation and lifecycle in the [quickstart](quickstart.md). Use `loginctl enable-linger ACCOUNT` through the administrator if agents should keep running without an open session and start after host boot. To check without modifying anything, run `loginctl show-user ACCOUNT --property=Linger` and check `systemctl --user status` from the account's own session.

`buzz-server account-plan --account ACCOUNT` checks that a different, non-root account exists and points to this procedure. It does not create an account, change UID, configure SSH or grant access. There is no single operator account that can enumerate or switch into other service accounts through Buzz Server. Use one account per agent if you need the Unix file-ownership boundary between agents; this is not a sandbox. Anyone who can run arbitrary processes as an agent's account can access that account's prompts, data and credentials. Never grant generalized `sudo` to a client or agent for this setup.

`delete` removes the agent from Buzz Server management after verifying the service stopped. It does not remove the account or its stored identity, credentials, work files or past Buzz messages. Review backups and retention before removing a service account or its data outside Buzz Server.
