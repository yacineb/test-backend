# Recommendation and migration triggers

Ship **DBOS Transact**, which is what this branch does.

The reasoning, in one line each:

- Capacity does not discriminate between the candidates ([capacity.md](capacity.md)).
- The recovery model does, and this workload belongs on the checkpointing side
  ([durable-execution-models.md](durable-execution-models.md)).
- Among checkpointing engines, DBOS is the one that is a library rather than a
  service, with governance strong enough to depend on
  ([engine-comparison.md](engine-comparison.md)).

## When to revisit

Revisit when one of these becomes true:

1. **Document types multiply and their DAGs diverge** — the DAG stops being a
   constant and starts being data. → Temporal or Restate.
2. **Intermediate state grows past what is sensible to checkpoint**, or becomes
   non-serialisable. → Temporal.
3. **The event history becomes a compliance requirement** rather than a
   debugging convenience. → Temporal.
4. **Human-in-the-loop review enters the pipeline**, adding suspension points
   measured in days. → Either; the checkpointing engines handle this fine.

**None of the four triggers is a throughput number.** If the only thing that
changes is volume, the answer stays as it is.

## Cheaper moves available first

Before changing engine, these are the changes that actually buy something:

- Return a storage key from `ocr`, not the text. Removes the only payload that
  grows with document size, from both our projection and the DBOS checkpoint.
- Make the steps `async` once the providers are real HTTP calls. Removes the
  thread ceiling described in [capacity.md](capacity.md).
- Size the queue partition concurrency per tenant deliberately, rather than
  inheriting a global default.
