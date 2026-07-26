# The field

Candidates, judged on the axes that actually differ. Throughput is omitted
because every option clears the requirement — see [capacity.md](capacity.md).

| | model | infrastructure | fan-out/join | external wait | governance |
|---|---|---|---|---|---|
| **DBOS Transact** | checkpoint | library + Postgres | queues + handles | `send`/`recv`, durable timeouts | DBOS Inc., MIT, 2.x, 466 releases |
| **Temporal** | replay | multi-service cluster | native | native signals | CNCF-scale, many maintainers |
| **Restate** | checkpoint | single binary server | native | native awakeables | Restate Inc., venture-backed |
| **Sayiir** | checkpoint | library + Postgres | `.fork()/.branch()/.join()` | `wait_for_signal(timeout=)` | solo maintainer, MIT |
| **Procrastinate / PgQueuer** | none (task queue) | library + Postgres | build it yourself | build it yourself | small teams, established |
| **Celery** | none (task queue) | broker + workers | `chord` (fragile) | not supported | very mature, very widely used |
| **Hand-rolled** | checkpoint | none | `WHERE` clause | status column + sweep | you |

The `model` column is the one that matters most; see
[durable-execution-models.md](durable-execution-models.md).

## On Celery

The long-form argument is in [celery.md](celery.md), because Celery is the
option most people would reach for. The summary: it is a
task queue, and this is a stateful workflow with an external suspension point.
Two durability domains for 0.07 jobs/second, a fragile `chord` for the fan-out,
no checkpoint to resume from, and no home at all for `awaiting_partner`.

Not a quality judgement — a shape judgement. If a team already runs Celery in
production the marginal cost is far lower than that list suggests.

## On Sayiir

*Disclosure: this project is maintained by the author of this submission.*

It is the closest fit on the list on features alone, which is worth stating
plainly rather than hiding. Its four primitives map one-to-one onto the four
requirements: `.fork().branch().branch().join()` is literally this DAG's shape;
`wait_for_signal(timeout=timedelta(hours=24))` is the `awaiting_partner` state
*including* the ghosted-partner timeout; checkpointing is the recovery model
argued for in [durable-execution-models.md](durable-execution-models.md). Its
signal buffering — signals sent before a workflow reaches `wait_for_signal` are
consumed rather than dropped — closes the webhook-arrives-early race by
construction, which is otherwise the implementer's problem to get right.

It is nonetheless not what this submission ships, for reasons that have nothing
to do with the design:

- **Bus factor.** A single maintainer is a genuine risk for a dependency this
  load-bearing, and it is the same objection that would be raised in any
  production review.
- **Maturity signals are inconsistent.** The documentation presents v1.0 while
  `sayiir-core` publishes 0.1.0 on crates.io; the README describes the project
  as under active development. Those should agree.
- **Crash recovery is under-documented.** The architecture page describes claim
  affinity via `last_failed_worker` tagging "rather than hard visibility
  timeouts", which leaves the obvious question — what reclaims a task whose
  worker was `SIGKILL`ed mid-step — unanswered on the page. Claims do expire
  and are reclaimed; the documentation simply does not say so, and it is the
  first thing any reviewer will ask of a queue.

None of that is disqualifying for the technology. It is disqualifying for
*this* decision, where the cost of being wrong is borne by a reviewer who
cannot audit the engine.
