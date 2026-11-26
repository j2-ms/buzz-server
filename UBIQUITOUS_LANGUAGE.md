# Ubiquitous Language

These terms mean the same thing in the interface, the documentation and the implementation. The English names follow **Create Agent**; the final UI labels are still open.

| Term | Meaning |
| --- | --- |
| **Buzz Server** | The standalone product for running and managing Buzz Agents on a headless Linux host. Not a Buzz relay. |
| **Server-CLI** | The complete management interface on the host. It can create, list, start, stop, configure and delete agents without a desktop client. |
| **Host** | The machine an agent runs on. From the desktop client's point of view, a **Remote Host** is a Linux host with Buzz Server that it reaches over SSH. |
| **Desktop Client** | A specific installation of Buzz Desktop with the management plugin. An optional GUI for the same management operations; it keeps no agent inventory of its own. |
| **Operator** | A person with permission to manage agents on a host. Being able to reach the host over SSH does not by itself grant permission to manage it. |
| **Run Target** | The choice of where the agent should run: on the local machine or on a Remote Host. Not an Inference Provider. |
| **Connection Test** | Checks that the desktop reaches the chosen host and can talk to Buzz Server. It proves neither that an agent is running nor that the model answers. |
| **Agent** | The assistant you address in Buzz, with an Agent Identity, a profile, a runtime configuration and, where present, Agent State. **Local** and **Remote** describe where it runs, not two kinds of identity. |
| **Agent Identity** | The stable public Buzz key under which the agent is addressed and answers. Not a display name, not a provider login and not a Linux account. |
| **Agent Profile** | Details visible in Buzz such as display name, avatar and description. Changing the profile does not create a new Agent Identity. |
| **Runtime Configuration** | The agent's operating parameters: the chosen harness, provider and model, the system prompt, and references to credentials. Not the state the agent has built up. |
| **Harness** | The runtime component that runs the agent or connects its execution to Buzz. The Inference Provider supplies model inference; it is neither the harness nor the host. |
| **Agent State** | Durable state the agent has built up: memory, saved sessions and working files. Public Buzz messages, process memory and credentials are separate from it. |
| **Linux Account** | The operating system account an agent runs under. A separate unprivileged **service account** per agent can keep its files and credentials apart from those of other agents. Not the operator's SSH login and not the Agent Identity. |
| **Create Agent** | Create a new agent with its own identity on the chosen Run Target. Not moving an existing agent. |
| **Reconfigure Agent** | Change the configuration or profile of an existing agent, through the Server-CLI or an authorized desktop. |
| **Move Agent** | Shift operation, configuration and durable state to another Run Target while keeping the same Agent Identity. Never a second active agent under that key. |
| **Delete Agent** | Take the managed agent out of operation on the host and remove it from management. Not merely hiding it on a desktop, and no promise to destroy earlier Buzz messages or all data. The retention and deletion rules are still open. |

**Managing and addressing are separate:** The operator manages agents through the Server-CLI, or through a desktop and SSH. Desktop and mobile address the agent through Buzz. No management connection has to be open for that.

See the [Vision](VISION.md) for the flow with two desktops and mobile.
