# Production Checklist

Everything to verify before a MediaHub instance is left running unattended, and
the reason each item is on the list. An item without a reason is an item people
skip.

Work top to bottom. The ordering is deliberate: the early sections fail fast and
loudly, and the later ones are the things that only bite after months.

---

## 1. Before the first start

### Configuration

- [ ] **`MEDIAHUB_ENVIRONMENT=production`.** This switches on
      `enforce_production_hardening`, which refuses to boot with a placeholder
      secret, a placeholder database password, `debug` enabled,
      `logging.diagnose` enabled, or the in-memory backend. A misconfigured
      production process that *runs* is far more dangerous than one that never
      starts.
- [ ] **`security.secret_key` is not a placeholder.** Refused at boot, but check
      it deliberately rather than discovering it in a stack trace.
- [ ] **`logging.diagnose` is off.** It prints variable values into tracebacks,
      which means bot tokens and database passwords in the log.
- [ ] **`security.block_private_networks` is on** (the default). Turning it off
      lets a submitted link reach anything on the household network — the
      router, a NAS, a cloud metadata endpoint.
- [ ] **The Telegram allow-list is populated.** Deny by default: an instance
      with no ids configured allows nobody, which is the correct posture for a
      bot whose username is guessable. Enabling the gateway with an empty list
      is refused at boot.
- [ ] **`.env` is not world-readable** and is not in the image. It holds the bot
      token.

### Timings — the three relationships that must hold

- [ ] **`heartbeat_seconds × 2 ≤ lease_seconds`.** Enforced by a validator, but
      understand it: a heartbeat slower than the lease means every job is
      reclaimed while it is still running, which presents as random duplicate
      execution rather than as a misconfiguration.
- [ ] **`drain_grace_seconds` < the orchestrator's kill timeout.** *Not*
      enforced anywhere, and the most expensive item on this page. Compose kills
      after 10 s by default; the shipped `docker-compose.yml` sets
      `stop_grace_period: 45s` against a 30 s drain. Get this wrong and the
      graceful path never runs, every deploy loses in-flight work, and nothing
      in the log looks wrong.
- [ ] **`download_timeout_seconds` exceeds the time to fetch
      `max_item_bytes` on your connection.** There is **no cross-attempt
      download resume** (see the [Failure Matrix](failure-matrix.md) §3), so a
      large item on a slow line either finishes within the budget or never
      finishes at all. At 10 Mbit/s, 2 GiB takes ~28 minutes; the 3600 s default
      is adequate, 20 Mbit/s and 8 GiB is not.

### Storage

- [ ] **The workspace is on a volume that survives a restart**, not the
      container filesystem. The startup sweep is what reclaims bytes from a
      crashed process, and it cannot reclaim what the restart already discarded —
      it can only *fail to notice* a leak.
- [ ] **`workspace.min_free_bytes` is set** (1 GiB default). This reserve is
      never allocatable, so that when everything else has gone wrong the store
      can still commit the transaction that records it.
- [ ] **`workspace.max_lease_bytes` is set** (8 GiB default) and is smaller than
      the device. It bounds the damage one runaway source can do, whatever it
      claimed its size was.
- [ ] **`workspace.purge_on_start` matches the deployment.** `true` is correct
      for one process per workspace root and **exactly wrong** for a shared root:
      it wipes directories another live worker is writing into.
- [ ] **One worker per workspace root.** Reservation accounting is per process.
      Two processes sharing a root rely on the conservative half of the recovery
      policy, which is safe but wasteful.
- [ ] **Two workers on one host differ in `worker.index`.** Identical identities
      release each other's leases at startup.

### Delivery

- [ ] **Decide about a self-hosted Bot API server.** It raises the upload ceiling
      from 50 MB to 2 GB — and raises peak RSS during upload to roughly twice the
      artifact, because the client library buffers the whole file. **Do not
      combine a local API server with multi-gigabyte items on a Pi.** See the
      [Performance Report](performance-report.md) §2.
- [ ] **`delivery.default_provider` names a provider that is actually enabled.**
      The worker resolves its destination at composition and refuses to start
      otherwise — better than discovering it after a two-hour download.

---

## 2. First start

- [ ] **The process starts.** A refusal is informative: no queue, no workspace,
      no destination, or no handler for a stage in the plan are all configuration
      mistakes that would otherwise present as jobs being claimed and instantly
      failed, burning an attempt every time.
- [ ] **`GET /health/live` returns 200.** Dependency-free by design, so a
      database blip never causes a restart loop.
- [ ] **`GET /health/ready` returns 200.** Checks the store *and* that the
      workspace root is a writable directory. A `503` here means one of those two
      is wrong — the log line names which.
- [ ] **The startup line names the destination and the ceiling.** Look for
      `Acquisition pipeline wired` with `destination`, `ceiling_bytes` and
      `custodian`.
- [ ] **The worker line names the identity and the plan.** Look for
      `Worker ready` with `worker`, `slots`, `plan` and `recovered`.
- [ ] **Run one real download end to end.** Confirm the file arrives, then
      confirm the workspace root is empty afterwards.

---

## 3. Verify the safety properties, once, deliberately

These take ten minutes and are worth far more than reading about them.

- [ ] **Pull the power mid-download.** The job should be `running` for at most
      two lease periods, then requeue and finish. The workspace root should be
      empty when it does.
- [ ] **`docker compose restart` mid-download.** The job should be handed back
      *without* costing an attempt, and resume within a poll interval.
- [ ] **Fill the disk** (`fallocate` a large file). Jobs should stay **queued**,
      not fail; the log should say `No workspace headroom` **once**, not once per
      job. Delete the file: claiming resumes within one idle poll.
- [ ] **`kill -9` the worker.** Identical to the power cut. Confirm no workspace
      directory survives.
- [ ] **Send `SIGTERM` twice** to a worker mid-stage. It should stop waiting for
      the drain and exit promptly.

If any of these behaves differently from the [Recovery Matrix](recovery-matrix.md),
stop and find out why before leaving the device alone.

---

## 4. Monitoring — what to alert on

The system is designed to be quiet. Anything below is worth waking up for
*because* it is not routine.

| Signal | Severity | Means | Action |
| --- | --- | --- | --- |
| `No workspace headroom above the emergency reserve` | **page** | The device is full. Jobs are queued, not lost. | Free space. |
| `A workspace lease could not be deleted and is now an orphan` | **page** | A delete failed — usually a read-only remount. Bytes will not come back until restart. | Check the filesystem; restart. |
| `An engine thread did not unwind within its drain budget` | **page** | Thread-pool capacity permanently lost. Enough of these and downloads stop silently. | Restart the worker. |
| `A claim loop ended without being asked to` | **page** | A defect. The process is exiting so it gets restarted. | Capture the traceback. |
| `Engine threads were abandoned during this process's life` | warn | Reported at shutdown. Confirms the above after the fact. | Investigate the source that wedged. |
| `/health/ready` → 503 | **page** | Store unreachable, or workspace root missing/read-only. | Check the mount and the store. |
| `The clock stepped backwards` | warn | NTP corrected the clock. Handled; renewal happens immediately. | None, unless frequent. |
| `Second stop request; giving up on the graceful drain` | info | Somebody was impatient, or the drain grace is too short. | Check `drain_grace_seconds`. |
| `Lease was reclaimed while settling` | warn | Two workers briefly disagreed. Handled correctly. | None, unless frequent — if it is, `lease_seconds` is too short. |
| `Claim cycle failed; the loop continues` | warn | Something unanticipated. One job cost, not the worker. | Read the traceback. |

Set `logging.json_format=true` wherever logs are shipped to a collector. Every
line already carries a correlation id, the job id and the worker identity.

---

## 5. Routine care

- [ ] **Restart on a schedule if `abandoned > 0` has ever been seen.** An
      abandoned engine thread is not recoverable in-process; a monthly restart
      costs one lease period and forecloses the failure entirely.
- [ ] **Watch free space as a trend, not a threshold.** The backpressure keeps a
      full disk from destroying the queue, but it also means a slowly filling
      device looks healthy right up until it stops taking work.
- [ ] **Keep yt-dlp current.** Extractors break when sites change; a stale
      engine presents as sources that "stopped working" for no visible reason.
- [ ] **After any change to timings, re-check the three relationships in §1.**

---

## 6. Before every deploy

- [ ] `make check` — lint, strict types, and the full suite (1,960 tests,
      ~47 s, no infrastructure required).
- [ ] The chaos and failure suites are part of that run. If a seed goes red, it
      is a bug and not a coincidence — the seeds are fixed.
- [ ] `stop_grace_period` still exceeds `drain_grace_seconds`.
- [ ] Migrations applied before the new image serves traffic.

---

## 7. Known limitations — accept these deliberately

Stated plainly, because each one is a design decision rather than an oversight,
and each one will eventually surprise somebody.

1. **No cross-attempt download resume.** The lease is per attempt; a dropped
   connection costs the whole transfer.
2. **Uploads are buffered in memory** by the Telegram client library. A ceiling
   turns the failure into a classified refusal, but the memory is still needed
   for anything that succeeds.
3. **Verify is not interruptible.** One pass over the artifact; a shutdown during
   it waits, or falls back to lease recovery.
4. **Sweeps run at startup only.** There is no scheduler process, so a
   long-running instance does not periodically re-check for orphans left by
   *other* identities.
5. **Reservation accounting is per process.** One worker per workspace root.
6. **An abandoned engine thread is permanent** until restart.
7. **The SQLite backend of the phase-02 design is not built.** This build uses
   PostgreSQL or the in-process store.
