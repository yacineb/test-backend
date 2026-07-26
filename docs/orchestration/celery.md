# Why not Celery

Celery is the option most people would reach for here, so it deserves a real
answer rather than a line in a table.

This is not a quality judgement. Celery is mature, widely operated, and easy to
hire for. It is the wrong *shape*: it is a task queue, and this is a stateful
workflow with an external suspension point.

## Short term (1,000 documents/day)

**Two durability domains for 0.07 jobs/second.** The broker holds queue state,
Postgres holds document state, and nothing keeps them consistent. A worker that
acknowledges to Redis and then dies before committing has lied to you. The fix
is enqueue-after-commit or a transactional outbox — more machinery than the
~200 lines of hand-rolled queueing it was meant to save, bought to serve a load
a single idle process handles.

**Redis as a broker is not durable by default.** Make it durable and you are
operating a second stateful service; leave it and you lose documents on
restart. RabbitMQ is durable and is a third container in `docker compose`.

**The fan-out needs a `chord`**, which needs a result backend, and whose
completion counter lives in the broker. Chords interacting with per-task
retries is a well-known sharp edge — a retried task inside the group can leave
the chord hanging.

**Celery retries a task; it does not resume a workflow.** If `external_call`
fails after `metadata` and `chunking` succeeded, there is no checkpoint to
resume from: what finished lives in a broker counter, not in inspectable state.
You end up writing `document_steps` as the real state machine, which is the
thing you wanted the framework for.

**A killed worker loses work.** With the default `acks_late=False` the task is
acknowledged on delivery and vanishes. With `acks_late=True` you need
idempotency the provider steps do not have.

## Long term (100,000 documents/day)

**Throughput is not the objection.** Celery scales to this comfortably; see
[capacity.md](capacity.md), where the requirement is 62 step-executions per
second and everything on the list clears it.

The objection is that at this volume the measured 1.6% give-up rate is ~1,600
documents a day needing operator replay, and **Celery offers no per-step
history to replay _from_**. You would rebuild checkpointing yourself.

**Observability is task-shaped, not document-shaped.** Flower shows task
events. Answering "where is document X, and why" means correlating logs.

**`awaiting_partner` has no home in Celery at all.** Parking for hours is not
something a task queue does; the state machine and the timeout sweep are yours
to build and to get right.

## The honest counterpoint

If a team already runs Celery in production, the marginal operational cost is
far lower than the list above suggests, and familiarity is worth real money.
Most of these objections are about *adding* a broker, not about living with one
you already operate. That argument does not apply to a greenfield service.
