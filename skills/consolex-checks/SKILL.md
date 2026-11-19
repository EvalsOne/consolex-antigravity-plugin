---
name: consolex-checks
description: "Explain how ConsoleX task delivery checks work in Antigravity."
---

The hook must confirm a ConsoleX task before it can be checked. To request an automatic final check, finish with a heading "## ConsoleX delivery", a "- Deliverable: this reply" line (or "- File: <local path>" lines), and "## Needs your confirmation" with "- None" or unresolved decisions. An ordinary reply is never automatically checked. Report the actual returned verdict and run link. Each native Automation trigger checks its delivery once and leaves any revisions for the owner to request. In a manual run, revise only with the owner's authorization or the task's bounded automatic revision setting. A reporting continuation must not modify files or submit another delivery. Use `consolex_checkpoint` only for an activated staged task with its own execution ID. Tools keep Antigravity permissions and the task's ConsoleX authorization. To prepare a native Automation, copy the `/consolex-schedule` command from the ConsoleX task.
