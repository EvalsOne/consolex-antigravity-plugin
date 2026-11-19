---
name: consolex-save
description: "Save this Antigravity conversation as a ConsoleX task draft. Optionally give a task name or --open."
disable-model-invocation: true
---

The owner must explicitly type /consolex-save. Follow the one-time save instructions from the ConsoleX PreInvocation hook: choose the task method skill, ask before adding requirements to an existing skill, then call consolex_save with the supplied save_id and skill_path. Follow the returned review link; saving is not task activation. If no save note is present, ask the owner to type the command; do not upload anything. Pass acceptance_mode="open_final" when the owner requests open acceptance.
