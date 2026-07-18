## Real skill sandboxing (post-contest)
What: run learned skills in a restricted subprocess/namespace instead of in-process with an AST import check.
Why: contest ships an honest footgun-guard (AST import whitelist), not a security boundary; a real boundary makes teach mode safe for other users.
Pros: turns the README security caveat into a solved problem. Cons: real design work (subprocess IPC, capability passing); zero contest value.
Context: the whitelist loader lands during Build Week in the skills loader; the sandbox would replace its enforcement layer. Blocked by: contest submission (Jul 21).
