# 0004. Ship the download seam before the engine

- Status: Accepted
- Date: 2026-08-01

## Context

Downloading is MediaHub's headline feature, and it is explicitly out of scope
for this foundation. That leaves a question with three plausible answers:

1. **Leave a hole.** No port, no adapter, no jobs - add it all later. The rest
   of the system then gets designed without ever considering the shape of the
   thing it exists to serve, and the eventual integration reshapes half of it.
2. **Stub something that pretends to work.** Fastest demo, worst outcome: a
   fake success is indistinguishable from a real one until someone trusts it.
3. **Define the contract, implement nothing.** Build everything around the
   seam - state machine, persistence, API, events, tests - and make the missing
   capability explicit and typed.

## Decision

Take the third option.

`application/download/ports.py` declares `DownloaderPort` together with
`DownloadRequest`, `DownloadOutcome` and a progress callback. The contract
speaks in primitives, so an adapter never imports the domain.

`infrastructure/downloader/null_downloader.py` implements the port by refusing
every request with `DownloaderNotConfiguredError`, which the API maps to
`501 Not Implemented` with the code `downloader_not_configured`.

`RequestDownload` durably queues jobs without invoking the port at all.
`DownloadJobRepository.claim_next_queued` is specified now for
`SELECT ... FOR UPDATE SKIP LOCKED`, so the future worker cannot hand one job
to two processes.

## Consequences

- The container is always fully wired; there is no `None` to guard against and
  no `AttributeError` waiting at depth.
- mypy checks the port's shape today, so the first real engine has an exact
  contract to satisfy rather than a description to interpret.
- The job lifecycle - retries, cancellation, progress, terminal states - is
  complete and tested before any transfer code exists, which is when those
  rules are cheapest to get right.
- The API tells the truth: `202 Accepted` for a queued job, `501` for anything
  that would require an engine.
- Cost: the port may need adjustment once a real engine meets a real provider.
  Designing it against a concrete first implementation would fit better - and
  would also bake that provider's quirks into the contract.
