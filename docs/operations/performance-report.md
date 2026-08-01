# Performance Report

What the system costs, measured rather than assumed, and what each number means
for a device that has to run unattended for years.

Every figure below has a test behind it in `tests/benchmarks/`, and every test
asserts a **ceiling** rather than printing a number. That distinction is the
whole point: a benchmark that only reports is a benchmark nobody reads, and a
regression that doubles memory use should fail the build, not be discovered on
the device six months later.

---

## 1. How to read these numbers

**Reference hardware** (where the figures below were taken):

| | |
| --- | --- |
| CPU | AMD Ryzen (x86-64), NVMe SSD |
| OS | Windows 10 |
| Python | 3.14 |
| Command | `pytest tests/benchmarks -m benchmark` |

**Target hardware** is a Raspberry Pi 4 with a Class-10 SD card, which is a
fundamentally different machine: roughly 5–10× slower on single-core CPU work
and 10–30× slower on small random writes. Where it matters, the Pi column below
is an **extrapolation and is labelled as such** — it has not been measured on
the device, and pretending otherwise would be exactly the kind of confident
wrong number this document exists to replace.

The assertions in the test suite are deliberately loose (several times the
observed cost). They are regression detectors, not performance targets: a tight
bound on a shared CI machine only teaches people to skip the suite.

---

## 2. Memory

| Measurement | Result | Ceiling asserted | Test |
| --- | --- | --- | --- |
| Heap growth over 40 complete jobs | well under the bound | < 4 MiB | `test_running_many_jobs_does_not_grow_the_heap` |
| 50,000 progress observations retained | < 1 MiB | < 1 MiB | `test_progress_observations_are_coalesced_not_accumulated` |
| Peak allocation hashing a 32 MiB file | **2.1 MiB** | < 8 MiB for 16 MiB | `test_hashing_a_large_file_is_bounded_by_the_chunk_not_the_file` |

**What matters here.** The first row is the one that decides whether the process
can run for a year: a leak of a kilobyte per job is invisible for a week and
fatal over twelve months. It is a ceiling on *unbounded* growth rather than an
assertion of zero, because the queue and the journal legitimately retain a record
per job.

The third row is why the digest is streamed at 1 MiB granularity rather than
computed over a whole file: **peak allocation is the chunk, not the artifact.**
Hashing a 2 GiB download costs the same 2 MiB as hashing a 32 MiB one. On a Pi
with 1–4 GiB of RAM that is the difference between a working verify stage and an
OOM kill.

### The one place memory is *not* bounded

`python-telegram-bot` reads an entire file into memory before uploading it. The
provider declares this honestly (`supports_streaming=False`) and now carries a
**buffering ceiling**: a whole-stream read past the destination's limit raises
`ArtifactTooLargeError` instead of being killed by the kernel.

| Deployment | Upload ceiling | Peak RSS during upload |
| --- | --- | --- |
| Public Bot API | 50 MB | ~100 MB (file + encode buffer) |
| Self-hosted Bot API server | 2 GB | **~4 GB — do not run this on a Pi** |

If you enable a local Bot API server on a small device, lower
`download.max_item_bytes` to something the machine can actually hold. The
ceiling turns the failure from "the worker died and the job is stranded" into
"this job failed with a reason", but it cannot make the memory appear.

---

## 3. File descriptors

| Measurement | Result | Ceiling asserted | Test |
| --- | --- | --- | --- |
| Descriptors leaked over 30 complete jobs | 0 | ≤ 5 | `test_running_many_jobs_leaks_no_descriptors` |
| Descriptors leaked over 50 lease + write cycles | 0 | ≤ 5 | `test_a_lease_closes_every_handle_it_opened` |
| Descriptors leaked over 50 *aborted* writes | 0 | ≤ 5 | `test_an_aborted_write_closes_its_handle_too` |
| Engine threads abandoned during the suite | 0 | must be 0 | `test_the_engine_thread_ledger_starts_clean` |

Descriptor exhaustion is the worst failure mode this system has, because it
arrives all at once, months in, and breaks the logging that would explain it.
Production hardening closed one real leak here — the Telegram client opened a
thumbnail handle inline in the upload call and never closed it, costing one
descriptor per video delivered.

The engine-thread row is a different resource with the same shape. A yt-dlp
thread that will not unwind cannot be killed; it holds a slot of the thread pool
for the life of the process. `engine_thread_stats().abandoned` counts them and
never decreases, and a non-zero value is reported at shutdown — because the only
recovery is a restart, and you want to choose when.

---

## 4. Hashing and disk I/O

| Measurement | Reference hardware | Pi 4 estimate | Test |
| --- | --- | --- | --- |
| SHA-256 throughput | **373 MiB/s** | ~30–45 MiB/s | `test_hashing_throughput_is_worth_measuring` (asserts > 5 MiB/s) |
| Durable manifest write (`fsync` file + dir) | **5.1 ms** | 20–60 ms | `test_a_durable_manifest_write_is_not_free_but_is_bounded` |
| Lease open + release cycle | **13.7 ms** | 50–150 ms | `test_opening_and_releasing_a_lease_is_cheap` |
| Releasing a 64 MiB / 64-file lease | **35.5 ms** | 200–600 ms | `test_releasing_a_large_lease_is_prompt` |
| 20 size measurements over a 100-file lease | < 5 s | — | `test_measuring_a_lease_does_not_walk_the_world` |

### What the manifest write buys, and what it costs

Production hardening made manifest writes **durable**: the staging file is
`fsync`ed before the rename, and the directory is `fsync`ed after it. Before,
the rename was atomic but not durable — a power cut could leave the new name
visible with none of its bytes behind it, which is the corrupt-but-parseable
state the whole recovery design is built to avoid.

The cost is two syncs on a ~400-byte file, twice per lease. At 5 ms on this
machine and a pessimistic 60 ms on a Pi, that is **~0.24 s per job** in the worst
case — against downloads measured in minutes. It is the cheapest insurance in
the system.

### Verify is one pass, and it is not interruptible

Integrity costs exactly one read of the artifact, and the digest is taken from
the stream when the engine wrote through the workspace. When an external tool
wrote the file directly (the yt-dlp path), verify hashes it once and caches the
result, so asking twice costs one read rather than two.

**Operational consequence:** that pass cannot be cancelled part-way. On a Pi
reading a 2 GiB artifact from an SD card at ~30 MiB/s, verify takes roughly
**70 seconds**, and a shutdown requested during it waits for it. Size
`worker.drain_grace_seconds` accordingly, or accept that a deploy during verify
falls back to lease-expiry recovery — which is correct, just slower.

---

## 5. Throughput

| Measurement | Result | Ceiling asserted | Test |
| --- | --- | --- | --- |
| Pipeline overhead per job (everything except the transfer) | **~69 ms** | < 500 ms | `test_the_pipeline_adds_little_per_job` |
| Durable progress writes per 10,000 observations | **≤ 25** | ≤ 25 | `test_progress_writes_are_throttled_hard` |
| `MeasuredReader` overhead vs. a plain chunked read | negligible | < +5 s over 4 MiB | `test_the_measured_reader_adds_little_over_a_plain_read` |

The per-job overhead covers claim, lease, five stages, five checkpoints,
delivery, settlement and release. At ~69 ms against downloads measured in
minutes, **the framework is not the cost** — which is the only defensible reason
to have a five-stage checkpointed pipeline at all.

### Progress throttling is an SD-card lifetime decision

A download engine reports per chunk. A durable write per chunk would be thousands
of writes for one file, and on a device whose database lives on flash that is not
slow, it is *fatal*. Three rules bring 10,000 observations down to at most 25
writes:

1. **Coalescing** — an update that arrives before the previous one was written
   simply replaces it.
2. **Throttling** — a write is due on elapsed time, on percentage movement, or
   on a stage change.
3. **Silence when nothing moved** *(added by production hardening)* — an
   observation reporting no more bytes than the last one written is not written
   at all. Before this, a stalled transfer produced one durable write every five
   seconds, for hours, all saying the same number. The lease is still renewed
   throughout, so liveness is unaffected.

At the default 5 s interval, a one-hour download costs at most ~720 progress
writes, and a stalled one costs none.

---

## 6. Recovery and cleanup latency

| Measurement | Result | Ceiling asserted | Test |
| --- | --- | --- | --- |
| Lease sweep after a crash | < 1 s | < 1 s | `test_recovering_a_crashed_job_is_a_single_sweep` |
| Lease sweep over 50 jobs | < 5 s | < 5 s | `test_a_sweep_over_many_jobs_stays_quick` |
| Restart recovery of own leases | immediate | — | `test_a_restarted_worker_recovers_without_waiting_for_the_lease` |
| Workspace reclaimed after a finished job | immediate, to zero bytes | `used_bytes == 0` | `test_a_finished_job_gives_its_bytes_back_immediately` |
| Workspace reclaimed after a *failed* job | immediate, to zero bytes | `used_bytes == 0`, no orphans | `test_cleanup_happens_even_when_the_job_failed` |

The last two rows are the product's central promise stated as a measurement:
**bytes come back whether the job succeeded or not.** The lease context manager
deletes the directory on any exit path, including `BaseException`.

Wall-clock recovery *times* — as opposed to sweep cost — are governed by
`lease_seconds` and are tabulated in the [Recovery Matrix](recovery-matrix.md).

---

## 7. Suite cost

| Suite | Tests | Wall clock |
| --- | --- | --- |
| Full suite with coverage | 1,960 | ~47 s |
| `tests/benchmarks` | 22 | ~11 s |
| `tests/failure` | 90 | ~3 s |
| `tests/chaos` | 88 | ~8 s |

The whole suite needs no database, no network and no Docker. That is not a
shortcut — it is the return on the ports-and-adapters investment, and it is why
the failure and chaos suites can afford to run on every commit.

---

## 8. Re-running this

```bash
pytest tests/benchmarks -m benchmark          # the ceilings
pytest tests/benchmarks -m benchmark --durations=25   # the numbers
```

If a ceiling fails, the message names the measurement and the observed value.
Resist the urge to raise the bound: the bounds are several times the observed
cost, so a failure means something genuinely changed shape, not that the machine
was busy.
