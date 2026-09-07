---
name: python-testing
description: 遵循项目现有测试约定，设计并执行聚焦的 Python 验证。
---

# Python testing

Use this workflow when Python behavior needs verification.

1. Inspect the project's existing test layout, runner, fixtures, and naming conventions.
2. Select the narrowest test that exercises the changed behavior. Prefer an existing runner and dependencies.
3. Cover the normal path and the boundary or regression that motivated the change.
4. Keep tests deterministic and independent of network access, local secrets, wall-clock timing, or user-specific paths.
5. Run the focused test first. Broaden to the relevant module or suite only when useful.
6. Treat compilation or import success as a syntax check, not proof of runtime correctness.
7. Report the command, result, and any coverage that remains intentionally out of scope.

If the appropriate verification depth is unclear, read `references/verification-levels.md`
and choose the smallest level proportionate to the change.
