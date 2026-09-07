---
name: bug-diagnosis
description: 通过复现、定位根因、最小修复与回归验证处理软件缺陷。
---

# Bug diagnosis

Use this workflow for a concrete failure, regression, hang, crash, or incorrect result.

1. Restate the observable symptom and identify what evidence would distinguish likely causes.
2. Inspect the smallest relevant path before editing. Do not assume the first suspicious line is the root cause.
3. Reproduce the problem with the narrowest deterministic command or input available.
4. Trace inputs, state transitions, and boundaries until the failing invariant is clear.
5. Make the smallest coherent fix that addresses the cause rather than hiding the symptom.
6. Re-run the reproducer, then one nearby regression check.
7. Report the cause, the exact behavior changed, and the evidence that the failure no longer occurs.

If the problem cannot be reproduced, preserve the collected evidence and explain the remaining uncertainty instead of claiming a fix.
