---
name: consolex-checks
description: "Explain how ConsoleX task delivery checks work in Antigravity."
---

Only an explicitly started ConsoleX task is checked. To request an automatic final check, finish with a heading "## ConsoleX delivery", a "- Deliverable: this reply" line (or "- File: <local path>" lines), and "## Needs your confirmation" with "- None" or unresolved decisions. An ordinary reply is never automatically checked. Report the actual returned verdict and run link. Revise only when the owner authorizes it or the active task explicitly opts into bounded automatic revisions. A reporting continuation must not modify files or submit another delivery. Use consolex_checkpoint only for an explicitly activated staged task, with its own execution id. Tools keep Antigravity permissions and the task's ConsoleX authorization. No scheduler or isolated-run command is provided by this plugin.
