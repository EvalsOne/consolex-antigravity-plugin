---
name: consolex-logout
description: "Disconnect this computer from ConsoleX. Only run when the owner explicitly types this command."
disable-model-invocation: true
---

The owner must explicitly type /consolex-logout. Relay the ConsoleX PreInvocation hook result. If it is absent, ask the owner to type the command; do not disconnect or modify credential files yourself.
