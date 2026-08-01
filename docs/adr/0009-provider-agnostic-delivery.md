# 0009. Delivery is a bounded context; Telegram is one provider

- Status: Accepted
- Date: 2026-08-01

## Context

Telegram is the only interface users have today, and the pressure to build the
product *as a Telegram bot* is real: it is faster, and every feature is one
handler away.

It is also how such products die. Telegram vocabulary (`chat_id`, `file_id`,
`message_id`) spreads into the core, business rules end up inside message
handlers, and adding a Web UI means re-implementing the product.

A second force pushed the same way: **re-delivery**. Sending an
already-acquired asset to another destination must not re-download it. If
delivery were a stage inside the acquisition job, that operation would require
fabricating a fake job.

## Decision

**Delivery is its own bounded context** with its own aggregate, its own queue
lane and its own state machine.

- `DeliveryProvider` is a port; Telegram is one implementation beside S3, NAS,
  webhook and email.
- The domain speaks `DeliveryTarget`, `RemoteArtifactRef`, `RemoteMessageRef`,
  `DeliveryCapabilities`. Telegram's vocabulary exists only inside its adapter,
  and an architecture test asserts that.
- Telegram's inbound side is a **separate driver** in the presentation layer
  that translates an update into exactly one application command — the same
  commands the HTTP API, CLI and automation use.
- `DeliveryCapabilities.can_serve_back` distinguishes destinations that can
  return the bytes (Telegram, S3) from those that cannot (webhook, email), which
  is what authorises local deletion (ADR-0007).

## Consequences

**Better.** Re-delivery is a first-class command costing one API call and zero
bytes. A new destination is a new adapter plus one registry line — no change in
`domain/`, `application/` or the pipeline. Deleting the Telegram packages breaks
Telegram and nothing else. The Processing planner consumes `max_bytes` as a
number and never learns what Telegram is, so a 2 GB-capable destination needs no
planner change.

**Worse.** More indirection than a bot would need: a `Delivery` aggregate, a
target registry, a capability model and a second queue lane, for a system that
today talks to exactly one provider. Telegram's real constraints (per-bot
`file_id`, upload ceiling, `429` semantics) still have to be modelled — as
generic concepts (`principal` on a ref, `max_bytes`, `retry_after`) rather than
as Telegram fields, which is slightly more work and considerably more durable.

**Accepted** because "Telegram is not the product" was a stated requirement, and
an architecture that cannot survive deleting its only current interface has not
met it.
