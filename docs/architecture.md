# Orchestration at the 12-month target

What should run this pipeline at 100,000 documents/day, and why the answer is
not "the biggest engine available".

**Answer:** DBOS Transact, which is what this branch ships. The argument is
that capacity does not discriminate between the candidates, the recovery model
does, and this workload belongs on the checkpointing side of that split.

| document | question it answers |
|---|---|
| [capacity.md](orchestration/capacity.md) | How much work is 100k docs/day really? What actually constrains us? |
| [durable-execution-models.md](orchestration/durable-execution-models.md) | Deterministic replay or checkpointing — what does each cost, and which fits this DAG? |
| [engine-comparison.md](orchestration/engine-comparison.md) | The candidates side by side, including Sayiir. |
| [celery.md](orchestration/celery.md) | Why not Celery, short term and long term. |
| [recommendation.md](orchestration/recommendation.md) | What to ship, and the four triggers that would change the answer. |

Implementation choices for the pipeline as built — data model, retry policy,
tenancy, the webhook contract — are in [pipeline.md](pipeline.md).

## The short version

1. Worst-case load at the 12-month target is 62 step-executions/second and 390
   concurrent step slots. Postgres handles thousands of queue claims per
   second. **Every candidate clears the bar by two orders of magnitude**, so
   throughput cannot decide this.
2. What differs is how an engine recovers a crashed workflow. Deterministic
   replay makes arbitrary control flow durable for free, and charges
   determinism constraints plus workflow versioning. Checkpointing persists
   each step's output and charges serialisability.
3. This DAG has four steps, a fixed shape, no loops, and small JSON-native
   outputs. **Replay's payoff is unused; its cost is paid in full.**
4. Among checkpointing engines, DBOS is a library rather than a service and has
   governance strong enough to depend on.
5. The trigger to revisit is workflow complexity, not volume.
