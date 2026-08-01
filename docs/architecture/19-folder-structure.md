# 19. Folder Structure

The final structure, including folders that do not exist yet. Reserving a
location now costs nothing and prevents the real failure mode: a feature arrives
under deadline, there is no obvious home, and it lands in the wrong layer
forever.

Legend: **(P1)** exists or Phase 03 · **(P2)** planned · **(P3)** reserved

---

## 19.1 Organising principle: layer first, context second

```
<layer>/<context>/<module>.py
```

Both axes are enforced ([04](04-dependency-graph.md)). The alternative —
context first, layer second (`catalogue/domain/…`) — was seriously considered:

| | Layer-first (**chosen**) | Context-first |
| --- | --- | --- |
| Layer violations | Visually obvious | Need tooling to see |
| A context's code | Spread over 4 trees | Together |
| Extracting a service | Gather from 4 places | Move one folder |
| Phase 01 compatibility | Already this way | Full restructure |
| Familiarity | Standard Clean Architecture | Needs explaining |

Chosen because the layer rule is the one that decays under pressure, and making
its violations visible in the file tree is worth more than the convenience of
co-location. The context axis is preserved as the consistent second level, so a
context's code is still predictable — and the enforcement test covers the axis
the tree does not.

**On the name `presentation/`:** it houses the worker and scheduler, which
present nothing. `entrypoints/` was considered and rejected: `interfaces/` would
collide with "ports/interfaces" (heavily used here), and renaming costs churn
across an enforced test suite and an accepted ADR for a naming improvement. The
sub-package name carries the meaning: `presentation/worker/` is unambiguous in
practice.

---

## 19.2 Repository root

```
mediahub/
├── src/mediahub/            # the application
├── tests/                   # the test suite
├── migrations/              # Alembic
├── docs/                    # architecture, ADRs, operations
├── deploy/               P2 # compose files, k8s manifests, systemd units
├── scripts/              P2 # dev/ops scripts (backup, restore, seed)
├── docker/                  # entrypoints
├── Dockerfile
├── docker-compose.yml
├── docker-compose.override.yml
├── docker-compose.prod.yml  P2
├── alembic.ini
├── pyproject.toml
├── Makefile
├── README.md
├── ARCHITECTURE.md
├── CHANGELOG.md          P2
└── SECURITY.md           P2
```

---

## 19.3 Domain layer

```
src/mediahub/domain/
├── common/                          # shared kernel — frozen, ADR to extend
│   ├── entity.py · value_object.py · events.py · errors.py
│   ├── pagination.py · time.py
│   └── fingerprint.py            P1
│
├── catalogue/                    P1  (Phase 01: domain/media)
│   ├── identifiers.py               # AssetId — public contract
│   ├── entities.py                  # MediaAsset
│   ├── value_objects.py             # SourceRef, AssetTitle, TechnicalProfile,
│   │                                #   RemoteArtifactRef, CustodyState
│   ├── enums.py · events.py · errors.py
│   ├── policies.py                  # CustodyPolicy
│   ├── specifications.py
│   ├── services.py                  # DuplicateResolver
│   └── repository.py
│
├── acquisition/                  P1  (Phase 01: domain/download)
│   ├── identifiers.py               # JobId
│   ├── entities.py                  # AcquisitionJob
│   ├── value_objects.py             # Lease, RetryPolicy, StageProgress,
│   │                                #   FailureReport, ArtifactHandle
│   ├── enums.py                     # JobStatus, JobStage, JobPriority
│   ├── events.py · errors.py
│   ├── policies.py                  # Admission, Retry, Expiry, Priority, Cleanup
│   ├── specifications.py
│   ├── factories.py
│   └── repository.py
│
├── delivery/                     P1
│   ├── identifiers.py · entities.py         # Delivery, DeliveryTarget
│   ├── value_objects.py                     # DeliveryReceipt, DeliveryCapabilities,
│   │                                        #   TargetAddress, RemoteMessageRef
│   ├── enums.py · events.py · errors.py
│   ├── policies.py · repository.py
│
├── processing/                   P2
│   ├── value_objects.py                     # ProcessingPlan, ProcessingStep, cost
│   ├── services.py                          # ProcessingPlanner  ← the intelligence
│   ├── policies.py · errors.py
│
├── sources/                      P2
│   ├── identifiers.py · value_objects.py    # SourceProbe, ProviderId
│   ├── services.py                          # SourceCanonicalizer (versioned)
│   ├── policies.py                          # UrlPolicy  ← the SSRF gate
│   ├── entities.py                          # ProviderHealth
│   └── repository.py
│
├── workspace/                    P2
│   ├── identifiers.py · entities.py         # WorkspaceLease
│   ├── value_objects.py                     # DiskBudget, ArtifactRole
│   ├── policies.py · errors.py · repository.py
│
├── access/                       P2
│   ├── identifiers.py · entities.py         # Principal, QuotaUsage, AuditEntry
│   ├── value_objects.py                     # Quota, PrincipalIdentity, Role
│   ├── policies.py                          # Authorization, RateLimit
│   └── repository.py
│
├── automation/                   P3
│   ├── entities.py                          # Subscription, SubscriptionRun
│   ├── value_objects.py                     # Schedule, SeenMarker
│   ├── specifications.py                    # ItemFilter
│   └── repository.py
│
└── enrichment/                   P3
    ├── entities.py · value_objects.py · repository.py
```

---

## 19.4 Application layer

```
src/mediahub/application/
├── common/
│   ├── use_case.py · ports.py               # Clock, UuidGenerator, EventPublisher
│   ├── unit_of_work.py · errors.py
│   ├── outbox.py                 P1         # transactional outbox contract
│   └── idempotency.py            P2
│
├── catalogue/
│   ├── dto.py · contracts.py                # ports offered to other contexts
│   ├── use_cases/  register · get · list · archive · forget · record_remote_copy
│   └── queries/    search_assets · asset_history
│
├── acquisition/
│   ├── dto.py · contracts.py
│   ├── use_cases/  submit_request · claim_job · advance_stage · checkpoint ·
│   │               fail_job · cancel_job · expire_jobs · requeue_dead_letter
│   └── queries/    job_detail · job_list · queue_stats
│
├── delivery/
│   ├── dto.py · ports.py                    # DeliveryProvider  ← the seam
│   ├── use_cases/  order_delivery · claim_delivery · record_receipt ·
│   │               fail_delivery · register_target · verify_remote_refs
│   └── queries/    delivery_status
│
├── download/                     P2
│   └── ports.py                             # DownloaderPort, ProgressSink,
│                                            #   CancellationToken
├── processing/                   P2
│   ├── ports.py                             # MediaProcessorPort
│   └── use_cases/  plan_processing · execute_plan
│
├── sources/                      P2
│   ├── ports.py                             # SourceResolverPort
│   └── use_cases/  probe_source · canonicalize
│
├── workspace/                    P2
│   ├── ports.py                             # WorkspacePort
│   └── use_cases/  reserve · release · sweep_orphans
│
├── access/                       P2
│   ├── ports.py
│   └── use_cases/  authenticate · authorize · consume_quota · audit
│
├── automation/                   P3
├── enrichment/                   P3
└── maintenance/                  P2
    └── use_cases/  reclaim_leases · sweep_workspace · apply_retention ·
                    backup_database · verify_remote_refs
```

---

## 19.5 Infrastructure layer

```
src/mediahub/infrastructure/
├── di/
│   ├── container.py                         # the composition root
│   └── registry.py               P2         # plugin registry
│
├── persistence/
│   ├── sqlite/                   P1         # replaces sqlalchemy/ (Postgres)
│   │   ├── engine.py                        # WAL, pragmas, busy_timeout
│   │   ├── base.py · types.py               # UtcDateTime, UuidText decorators
│   │   ├── models/  catalogue · acquisition · delivery · access · workspace ·
│   │   │            queue · outbox · audit
│   │   ├── mappers/  one per aggregate
│   │   ├── repositories/  one per aggregate
│   │   ├── queue.py                         # the claim statement
│   │   ├── outbox.py · unit_of_work.py
│   │   └── search/  fts5.py      P2
│   └── memory/                   P1         # test/demo adapters, same contracts
│
├── download/                     P2
│   ├── ytdlp/ · http/ · torrent/ P3
│   └── shared/  progress.py · limits.py · resume.py
│
├── processing/                   P2
│   └── ffmpeg/  processor.py · probe.py · steps/
│
├── delivery/
│   ├── telegram/                 P2         # ← the ONLY place `file_id` appears
│   │   ├── provider.py · client.py · rate_limiter.py
│   │   ├── errors.py · mappers.py
│   ├── s3/ · nas/ · webhook/     P3
│
├── sources/                      P2
│   ├── ytdlp_resolver.py · http_resolver.py · canonicalizers/
│
├── workspace/                    P2
│   ├── filesystem.py · tmpfs.py · containment.py · disk.py
│
├── messaging/
│   ├── logging_event_publisher.py P1
│   ├── outbox_publisher.py       P2
│   └── in_process_bus.py         P2
│
├── scheduling/                   P2
│   ├── clock_source.py · cron.py · singleton_lock.py
│
├── ai/                           P3
│   ├── whisper_local/ · ollama/ · openai/
│
├── security/                     P2
│   ├── url_resolver.py                      # DNS + IP checks (policy is domain)
│   ├── secret_redaction.py · api_keys.py · subprocess_confinement.py
│
├── observability/                P2
│   ├── metrics.py · tracing.py · health.py · heartbeat.py
│
└── system/
    ├── clock.py · id_generator.py           # P1 (UUIDv7 in Phase 03)
```

---

## 19.6 Presentation layer

```
src/mediahub/presentation/
├── api/                          P1
│   ├── app.py · lifespan.py · dependencies.py     ← composition root
│   ├── errors.py                                  # RFC 9457
│   ├── middleware/  correlation · access_log · auth P2 · rate_limit P2
│   ├── routers/     health · metrics P2
│   ├── sse/         progress.py  P2
│   └── v1/
│       ├── router.py
│       ├── schemas/  common · media · downloads · deliveries P2 ·
│       │             targets P2 · subscriptions P3
│       └── routers/  media · downloads · deliveries P2 · targets P2 ·
│                     search P2 · admin P2
│
├── telegram/                     P2         # inbound gateway (a DRIVER)
│   ├── __main__.py                          ← composition root
│   ├── gateway.py · router.py
│   ├── handlers/  submit · status · cancel · resend · search · help
│   ├── formatters/  job · asset · error
│   └── auth.py
│
├── worker/                       P2         # not "presentation" in the visual
│   ├── __main__.py                          ← composition root   sense; it is a
│   ├── loop.py · executor.py                                     driver
│   ├── stages/  download · verify · fingerprint · plan · process ·
│   │            order_delivery · finalise
│   ├── heartbeat.py · shutdown.py · progress_registry.py
│
├── scheduler/                    P2
│   ├── __main__.py                          ← composition root
│   ├── loop.py · tasks/  lease_reaper · expiry · workspace_sweep ·
│                         disk_guard · retention · backup · subscriptions
│
├── cli/                          P3
│   ├── __main__.py                          ← composition root
│   └── commands/  submit · status · assets · admin · backup · doctor
│
└── web/                          P3
    ├── app.py · routes/ · templates/ · static/
```

`doctor` (CLI) is worth reserving: a single command that runs the deep health
checks, validates configuration, verifies the FFmpeg binary and checks disk —
the first thing to ask a self-hosting user to run.

---

## 19.7 Plugin SDK

```
src/mediahub/plugins/
├── api/                          P2         # standalone; imports NO layer
│   ├── manifest.py · types.py · errors.py
│   ├── downloader.py · processor.py · delivery.py · source.py · ai.py
│   └── version.py                           # SDK semver
├── testing/                      P2         # shared contract test suites
│   └── contracts/  downloader · delivery · processor
└── builtin/                      P2         # first-party plugins (in-tree, v1)
```

`plugins/testing/` shipping with the SDK is deliberate: a third party writing a
plugin gets the conformance suite, and first-party plugins run the same one.

---

## 19.8 Shared

```
src/mediahub/shared/
├── config/
│   ├── settings.py · sections/  api · database · workspace · queue · worker ·
│   │                            download · processing · delivery · telegram ·
│   │                            security · logging · monitoring · retention ·
│   │                            features
│   └── secrets.py                P2         # _FILE resolution
└── logging/
    ├── setup.py · context.py · intercept.py
    └── redaction.py              P2         # ← mandatory secret filter
```

Splitting `settings.py` into `sections/` is a Phase 03 task: one file with
fifteen sections becomes a merge-conflict magnet and obscures ownership.

---

## 19.9 Tests

```
tests/
├── conftest.py                              # doubles: FrozenClock, fakes
├── unit/          domain/<context>/ · application/<context>/ ·
│                  infrastructure/ · shared/
├── architecture/  layers · contexts · third_party · vocabulary · docstrings
├── contract/      repository · unit_of_work · queue · downloader ·
│                  delivery · workspace          ← every port, every impl
├── integration/   api/ · pipeline/ · telegram/ · scheduler/
├── security/      ssrf · traversal · injection · redaction · authz
├── chaos/         crash_recovery · disk_full · db_locked · clock_jump
├── performance/   api_latency · queue_claim · memory · progress_writes
├── smoke/         compose_up · migrations · e2e_pipeline
└── fixtures/      media/ (tiny generated files) · providers/ (recorded JSON) ·
                   payloads/ (malicious corpora)
```

---

## 19.10 Documentation

```
docs/
├── architecture/  00–20 + README        ← this specification
├── adr/           0001…                 ← decisions
├── operations/                       P2
│   ├── runbook.md · backup-restore.md · troubleshooting.md ·
│   ├── token-rotation.md · disk-full.md · upgrade.md
├── api/                              P2 # generated OpenAPI snapshots per version
├── plugins/                          P3 # SDK guide + the "plugins are trusted" warning
└── user/                             P3 # setup, Telegram commands, FAQ
```

`operations/` is not optional for a self-hosted product. The user *is* the
operator, and "what do I do when the disk is full" must be written down before
it happens, not discovered at 1 a.m.

---

## 19.11 Where does a new file go?

| I am adding… | It goes in | Because |
| ------------ | ---------- | ------- |
| A business rule | `domain/<context>/` | Rules are the core |
| An operation a user can invoke | `application/<context>/use_cases/` | One intention, one class |
| A read | `application/<context>/queries/` | May bypass aggregates |
| A third-party integration | `infrastructure/<area>/<vendor>/` | Adapters are outermost |
| An HTTP endpoint | `presentation/api/v1/routers/` | Versioned contract |
| A Telegram command | `presentation/telegram/handlers/` | Thin translation only |
| A pipeline step | `presentation/worker/stages/` | Execution, not rules |
| A periodic task | `presentation/scheduler/tasks/` | One clock owner |
| A setting | `shared/config/sections/` | The only home for config |
| A new capability provider | `infrastructure/<area>/<name>/` + registry line | Plugin |
| A cross-context contract | `application/<context>/contracts.py` | Public surface |

If none of these fit, the design is unclear — that is a conversation before it
is a file.
