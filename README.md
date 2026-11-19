# ConsoleX for Antigravity

Run your [ConsoleX](https://consolex.ai) tasks in Antigravity and check the results against your saved acceptance standard. The plugin puts the verdict and a link to the run in the conversation.

## Install the preview

You need Antigravity with native plugin support, the `agy` CLI, `python3` (3.9 or later), and a ConsoleX account on a paid plan. This preview requires a ConsoleX backend that supports the `antigravity` runtime. The delivery flow was verified in Antigravity App 2.33.0 against a development backend.

```sh
agy plugin install https://github.com/EvalsOne/consolex-antigravity-plugin
```

Open Antigravity, find **ConsoleX** under **Customizations → Installed**, and start a new conversation in your task's project. Type `/consolex-login` and approve the code in ConsoleX. If your preview uses a development server, type `/consolex-login <API URL>` with that server's address.

## Run a task

```text
/consolex-tasks
/consolex-start <task UUID>
```

When the task has automatic checks enabled, an explicit delivery like this is submitted for checking:

```markdown
## ConsoleX delivery
- Deliverable: this reply

## Needs your confirmation
- None
```

To deliver a local text file, use `- File: <path>` in place of the reply declaration. ConsoleX can request a bounded revision when the task's settings allow it.

| Command | Action |
| --- | --- |
| `/consolex-status` | Show your connection and tasks |
| `/consolex-tasks [search]` | Find a task |
| `/consolex-start <UUID> [-- request]` | Start a task in this conversation |
| `/consolex-add <UUID>` | Add a reusable `/cx-…` shortcut |
| `/consolex-remove <UUID or shortcut>` | Remove a local shortcut |
| `/consolex-check [off]` | Check the latest delivery or turn off automatic checks for this session |
| `/consolex-save [--open] [name]` | Prepare a task draft for your review |
| `/consolex-logout` | Disconnect this computer |
| `/consolex-checks` | Explain delivery and stage checks |

## What the plugin reads and sends

The plugin reads the active conversation's local transcript through Antigravity hooks. For a delivery check it sends ConsoleX the declared reply or text files, model and session identifiers, confirmation items, and captured tool calls and results. It skips private model thinking and redacts credential patterns from tool evidence. Set `CONSOLEX_TOOL_EVIDENCE=off` to omit tool evidence. Saving a conversation as a task is an explicit command and creates a reviewable draft.

Sign-in is shared with the other ConsoleX coding-agent plugins through `~/.consolex/credentials.json`. Antigravity session state is stored separately. Task-tool access remains subject to Antigravity's permissions and the task's ConsoleX settings.

## License

[MIT](LICENSE) · [Antigravity plugin documentation](https://antigravity.google/docs/plugins/)
