# Verification levels

- Syntax: parsing, compilation, or import checks. Use only to catch malformed code.
- Focused behavior: one test or direct invocation covering the changed path. This is the default.
- Module regression: the relevant module or test file when nearby behavior may be affected.
- Broader suite: use when shared interfaces, configuration, or cross-module behavior changed.

Passing a narrower level does not imply that broader integration behavior was exercised.
