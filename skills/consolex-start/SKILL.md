---
name: consolex-start
description: "Start a ConsoleX task in this Antigravity conversation. Give its UUID, name or link, optionally followed by -- and a request."
---

Use the activation receipt from the ConsoleX hook. If no activation receipt is present, call consolex_start_task with the task and the additional request, if any. Omit _activation_context: the PreToolUse hook supplies it. Continue only after status="started" for the correct task. If the tool or hooks are unavailable, report that the task was not started and do not execute or publish it. Follow the latest approved method and read its supporting skills. Use the returned task reference and execution id for ConsoleX tools and stage submissions. Reading a skill file alone never starts a task.
