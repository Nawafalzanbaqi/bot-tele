# 0011. Plugins are contracts now, trusted code for v1

- Status: Accepted
- Date: 2026-08-01

## Context

MediaHub needs extension points: downloaders, media processors, delivery
providers, storage providers, AI providers. The temptation is to build a full
plugin system — discovery from disk, dynamic loading, a permission model,
sandboxing, distribution.

Two facts argue against building that now:

1. **There is nothing to plug in yet.** Every planned extension for the next
   year is first-party.
2. **In-process Python plugins cannot be sandboxed.** A loaded module executes
   with the worker's privileges: it can read the bot token, the database and the
   filesystem. Real isolation requires a subprocess with typed IPC, or WASM —
   a substantial subsystem with its own lifecycle, serialisation and debugging
   story.

An "extension system" that implies safety it does not provide is worse than none
at all: it invites users to install code that exfiltrates their credentials.

## Decision

**Define the contracts now. Ship first-party, in-tree, reviewed plugins. Defer
loading untrusted code.**

- Every extension point is an existing port with a uniform contract shape:
  manifest, `capabilities()`, `supports()`, operations.
- The SDK (`mediahub.plugins.api`) is a standalone package importing no layer,
  exchanging primitives and DTOs only — never domain types.
- Discovery is over a **static registry**. No runtime loading from disk or PyPI.
- Every implementation must pass the SDK's shared **contract test suite**
  (cancellation, timeouts, error classification, progress monotonicity,
  workspace containment).
- Per-plugin circuit breakers isolate a broken provider from the rest.
- The documentation states plainly that a plugin has full device access.

## Consequences

**Better.** The extension points are real and exercised today (in-memory and
real adapters for the same ports), so a future provider is a known quantity, not
a hope. No sandbox to build, maintain or get subtly wrong. The contract test
suite means N implementations stay honest without N bespoke test suites.

**Worse.** Third parties cannot extend MediaHub without a fork or a pull
request. For a single-household, self-hosted product that is an acceptable and
arguably correct trade — but it is a real limitation and it is stated as one.

**Migration path, deliberately kept cheap.** Because the contracts exchange only
primitives and DTOs, moving plugin execution into a confined subprocess is a
change of *transport*, not of design: add IPC, confine the process, add a
manifest permission model, then consider signing and distribution. Designing for
the future here means not blocking that path — not building it in advance.
