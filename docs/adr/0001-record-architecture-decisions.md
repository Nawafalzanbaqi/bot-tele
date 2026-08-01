# 0001. Record architecture decisions

- Status: Accepted
- Date: 2026-08-01

## Context

MediaHub is intended to be maintainable for years. Over that span the people
change, and the reasoning behind a structural decision is the first thing lost.
What remains is code that looks arbitrary, so it gets "cleaned up" - and the
constraint it existed to satisfy comes back as a bug.

Code shows *how*. Git shows *when* and *by whom*. Neither reliably captures
*why this and not the obvious alternative*.

## Decision

Record every architecturally significant decision as a numbered ADR in
`docs/adr/`, using Michael Nygard's format.

A decision is architecturally significant if reversing it would touch many
modules, change a public contract, or require a data migration.

ADRs are immutable once accepted. Superseding one means writing a new ADR and
marking the old one superseded.

## Consequences

- Newcomers can read the reasoning instead of guessing at it.
- Debates that were already settled do not get re-litigated from scratch;
  they get re-opened with a new ADR when the forces actually change.
- Small cost per decision, paid at the moment when the reasoning is freshest.
- ADRs that are never written are worse than no process: the discipline only
  pays off if the significant decisions actually land here.
