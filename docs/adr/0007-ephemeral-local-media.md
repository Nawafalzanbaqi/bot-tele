# 0007. Local media is ephemeral; custody transfers to the destination

- Status: Accepted
- Date: 2026-08-01
- Supersedes the storage model implied by ADR-0003's example schema

## Context

The product requirement is explicit: **after successful delivery, delete the
local media automatically.** Keep only metadata, history, the remote file
reference, the message reference, the content hash and the source URL.

The Foundation modelled the opposite — a permanent library
(`MediaItem.storage_key`, `MediaStatus.AVAILABLE`, `storage.library_path`).
Bolting cleanup onto that model would leave every name and every state meaning
something subtly wrong, and "temporary" files would accumulate whenever a code
path forgot to delete.

The device makes this more than a preference: a 32 GB SD card cannot hold a
media library, and a filled card corrupts the database.

## Decision

Local bytes are **temporary custody**, never storage. Model it explicitly:

- `MediaAsset.custody ∈ {NONE, LOCAL_ONLY, LOCAL_AND_REMOTE, REMOTE_ONLY, LOST}`,
  with `REMOTE_ONLY` as the steady state.
- Deletion is authorised by **proof**: a committed `DeliveryReceipt` carrying a
  `RemoteArtifactRef` from a destination whose capabilities declare
  `can_serve_back=true`.
- Commit order is fixed and may not be "optimised": receipt → custody → delete.
- Local files live in job-scoped workspace leases, swept unconditionally by a
  janitor, so a missed cleanup is a temporary disk cost rather than a permanent
  leak.
- `retain_local` is an explicit per-asset override, with its cost visible.

## Consequences

**Better.** Disk usage is bounded by concurrent work, not by history. Backup is
one small file. Re-delivery costs one API call and zero bytes, because the
destination kept the bytes and returned a reusable reference. The product can
remember everything it has ever done on a Raspberry Pi.

**Worse.** The destination becomes the custodian. If the Telegram message is
deleted, the token rotated, or the account lost, **the media is gone** — MediaHub
keeps the source URL and can attempt re-acquisition, but the source may be gone
too. Mitigations (store `file_unique_id`, keep the source URL, add a second
serving destination such as a NAS, `retain_local`) reduce but do not eliminate
this.

**Accepted deliberately, and stated in user-facing documentation.** A product
that deletes files must say so where the user will read it, not only in an
architecture document.
