---
name: consolex-login
description: "Connect this computer to ConsoleX. Optionally give a ConsoleX API address."
---

The PreInvocation hook handles an explicitly typed /consolex-login. Relay its sign-in instructions and browser link. If the hook note is absent, call consolex_command with command="login" and the API address, if supplied. Do not claim a connection until ConsoleX confirms it.
