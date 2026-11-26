# Vision: Buzz Server

This is the intended product, not a list of features available in the current release.

Operators who run Buzz agents on a headless Linux host should be able to manage them entirely through the **Server CLI**: create, list, start, stop, configure, and delete them. No desktop client is required. The Buzz Desktop plugin is an optional graphical interface for the same operations. An agent doesn't belong to the interface that created it. The host runs it; people reach it through Buzz, for example from Buzz Mobile.

## Two ways to manage the same agents

**Without Desktop:** I install the Server CLI at the console of a headless Linux host. I create an agent, configure it, start it, and check its status. I get a reply in Buzz and can reconfigure, stop, and delete the agent. None of these steps requires Buzz Desktop to be installed or to have ever connected to the host.

**With Desktop:**

1. I enable the plugin in Buzz Desktop and connect it over SSH to the host where Buzz Server is installed.
2. In **Create Agent**, I select the host as the **Run Target**. The **Connection Test** is the first visible sign of success: the selected host is reachable and the client can talk to Buzz Server. It does not yet prove that an agent can reply.
3. I create the agent. I choose the harness and inference provider, set up provider access, and set the system prompt, profile, and avatar. The host starts the agent and runs it independently of Desktop.
4. I close the first Desktop client. From Buzz Mobile, I mention the agent and it replies. Mobile needs neither SSH nor the plugin.
5. I connect a second authorized Desktop client to the same host. It shows the existing agent without importing anything from the first client. I change its profile, for example, verify the change in Buzz, and get another reply on Mobile. If I then delete the agent, it stops running on the host. Merely removing its entry from the second client would not count as deletion.

The **host** is the source of truth for the management state of its remote agents. Buzz remains the platform for agent identity, published profiles, and messages. A Desktop client may need its own connection settings, but must not hold the only copy of any management state. To meet the acceptance criteria, agents must reply without the original Desktop client and between management operations. Restarts need not be seamless, and a deleted agent must not reply.

The CLI and Desktop show the same agents: I can edit an agent in Desktop that I created at the console, and vice versa. The plugin does not keep a separate agent list with its own lifecycle.

## Dedicated Linux accounts

An operator should have the option to assign an agent its own unprivileged Linux service account. The agent runs under that account. With appropriate file permissions, its working files, memory, and keys are protected from other unprivileged agents. This is not a full sandbox. The agent's **service account** is neither the operator's SSH account nor the agent's Buzz identity.

**Proposal for v1:** An administrator creates the service account in advance; Buzz Server assigns it to the agent. Desktop does not get general root access. Deleting an agent does not automatically delete its Linux account. Automatically creating or removing accounts is a separate decision for later, with implications for permissions and data retention.

## Move, don't copy

A future version should let me move a local agent entirely to a remote host. Afterwards I still talk to **the same agent**: the same Buzz identity and profile, configuration, memory, saved sessions, and working files. The credentials and dependencies needed to keep it running must also be available at the destination. Not every secret will be exportable: when a provider requires a fresh sign-in, the move must tell me instead of silently losing state or access.

A move transfers operation; it does not create a clone. The source and destination must never run an agent under the same identity at the same time. The destination host must demonstrate that it has taken over successfully. If the move fails, I should be able to return to running the agent locally. Which harnesses to support, which state formats are portable, and how to handle a turn still in progress remain open.

## Proposed scope for the first version

- First, demonstrate the full **CLI workflow** for a newly created agent. For Desktop integration, also demonstrate a successful connection test, a reply in Mobile, and management of the same agent from two Desktop clients without depending on the first. A successful SSH login alone is not enough.
- Treat full migration as a separate follow-up. The first version must not bind the agent identity to the first Desktop client.
- No dedicated tailnet, automatic Tailscale setup, or new relay is required.

## Upstream and open decisions

Buzz Server is an independent product, not a fork of Buzz Desktop. [Buzz PR #3449](https://github.com/block/buzz/pull/3449) is a reference for target selection and host checks, not a codebase or prerequisite. Its description specifies a read-only remote server view and no undeploy operation in v1 of the provider protocol. For the plugin, we will check which supported extension points cover the required operations. If one is missing, a small change to Buzz Desktop is an option to discuss with its maintainers before contributing it. The CLI remains independent of that work.

Decide before implementation: How are operator permissions checked? Which credentials stay on the host and which must be set up again at the destination? What does deletion remove irreversibly, and what is retained? For which harness can we reliably demonstrate the first complete move?

See [Ubiquitous Language](UBIQUITOUS_LANGUAGE.md).
