# Back up and restore a dedicated Goose agent

This procedure recovers a dedicated agent's Buzz identity, configured profile
and prompt, and a single named private working file on a clean Ubuntu host.
Use its original relay so existing channel membership and history remain
available. Use the same release and pinned tools until the restored agent
has replied. Then use explicit worker upgrade if needed.

## Prepare a private backup

| Owner | Include |
| --- | --- |
| Operator | The original agent public key, release and tool pins, full config JSON including sender policy, and the configured UTF-8 prompt file. Record the selected source from `status`. For an approved snapshot, retain an exact operator copy made when you approved it. |
| Dedicated account | `state/agents/ID/identity.key` and the selected private working file, here `state/agents/ID/work/recovery-note.txt`, under the target data path. |

Keep these two sets in separate encrypted backups with access restricted to
the respective account and the authorized backup administrator. Use your
existing account-specific backup and transfer channel. Keep the agent key out
of the operator's backup, terminal output and command arguments.

`buzz-server account-plan --account ACCOUNT --id ID` shows the target data
path and expected system unit for the CLI's state directory. Use the same
CLI state directory you use for the agent. Stop the agent before saving a
consistent backup and verify the unit is disabled and inactive or failed
with an empty cgroup. Obtain system-service shutdown proof through the
operator or host administrator if the original host is inaccessible. Fence
the original host or revoke its ability to run and reach the relay before
ever starting the replacement. Preserve the agent's relay admission.

As the dedicated account, from the original `state/agents/ID` directory,
write into a private scratch directory used by the backup tool. The example
assumes `backup` is the absolute path to that existing mode-0700 directory.

```sh
set -eu
umask 077
test -f identity.key
test ! -L identity.key
test ! -L work
test -f work/recovery-note.txt
test ! -L work/recovery-note.txt
sha256sum identity.key work/recovery-note.txt > "$backup/files.sha256"
tar -cf "$backup/agent.tar" -- identity.key work/recovery-note.txt
```

Delete and uninstall retain the target's private files and identity, Goose
credentials, work, and the operator's binding and removal proof. See
[Dedicated account](service-accounts.md#dedicated-account). This backup
excludes installed units, old UID-bound operator records, old worker bundles,
Goose credentials, caches and saved sessions, logs, and other working files.
Sign in again through Goose under the new account. Provision the destination
account, CLI and pinned tools separately using the normal procedure. This
procedure does not restore relay data.

## Restore to the new account

1. From the destination operator login, run `buzz-server doctor --account
   ACCOUNT` and `buzz-server account-plan --account ACCOUNT --id ID`. Use
   a fresh ID in that CLI's inventory and the destination data path from
   the plan. Recreate the binding through `create` on the destination. The
   account name, UID and data path can differ from the original binding.
2. Restore the authenticated private archive to a mode-0700 staging directory
   accessible to the destination dedicated account through the authorized
   backup channel. As that account, validate both files before populating a
   new agent directory. Below `backup` points to the restored archive and
   manifest, and `agent` is the planned target data path plus
   `/state/agents/ID`. Set both variables to absolute paths first.

   ```sh
   set -eu
   umask 077
   test ! -e "$agent"
   test ! -L "$agent"
   stage=$(mktemp -d)
   cd "$stage"
   mkdir work
   tar -xOf "$backup/agent.tar" -- identity.key > identity.key
   tar -xOf "$backup/agent.tar" -- work/recovery-note.txt > work/recovery-note.txt
   sha256sum identity.key work/recovery-note.txt > files.sha256
   cmp -s files.sha256 "$backup/files.sha256"
   test "$(wc -c < identity.key)" -eq 64
   mkdir -p "${agent%/*}"
   mkdir "$agent"
   mkdir "$agent/work"
   install -m 0600 identity.key "$agent/identity.key"
   install -m 0600 work/recovery-note.txt "$agent/work/recovery-note.txt"
   ```

   Destroy the temporary decrypted staging copy according to the backup
   policy. If validation or staging fails, leave the agent stopped. Restore
   from a complete authorized backup before retrying.
3. From the operator login, restore the config JSON and the prompt with mode
   0600. Adjust the JSON's prompt path for the destination and retain the
   original owner, profile, relay and sender policy. Run

   ```sh
   buzz-server create ID --account ACCOUNT --config "$config" --expect-identity "$public_key"
   ```

   Supply the original **public** key. Creation refuses a missing or mismatched
   restored key and leaves the unit uninstalled. An interrupted create reserves
   the ID. Correct the incomplete restore and retry with the same inputs.
   Compare the reported identity, sender policy and configured profile with
   the operator backup. If restoring an approved prompt, explicitly upgrade
   the worker and activate the exact saved snapshot before first start.
   Activation assigns a new local prompt revision ID.
4. Run `buzz-server inference configure ID` and complete Goose authorization.
   Verify the old instance is fenced, then run `buzz-server start ID` and
   [check a new reply](quickstart.md#first-reply). Verify original authorship
   and thread linkage. Through the dedicated account, compare the restored
   working file's digest with the private manifest. Record elapsed recovery
   time from starting with the prepared backup through reply readback.

Repeat the stage validation with one of the two files removed in a synthetic
copy of the backup. The validation must fail before creating or starting a
service. Also test the public-key guard with a synthetic mismatch. Never
exercise either failure on the only copy of a private backup.
