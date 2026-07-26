# Replay versus checkpointing

Durable execution engines recover a crashed workflow in one of two ways. This —
not throughput, not licensing — is the decision that shapes the code you write.

## Deterministic replay (Temporal, Cadence)

Recovery re-executes the workflow function from the top against a persisted
event history. Completed activities return their recorded results instead of
running again.

**What it buys.** Arbitrary control flow becomes durable for free. Loops,
conditionals, local variables and accumulated in-memory state all survive a
crash without anyone serialising them, because the code is simply re-run to the
same point. You also get unlimited-duration timers, queries against running
workflows, and a complete event history that doubles as an audit artifact.

**What it costs.** The workflow function must be deterministic. No wall-clock
reads, no randomness, no direct I/O — every source of non-determinism has to go
through SDK primitives. And because recovery replays *code*, changing that code
while instances are in flight requires explicit versioning or patching. In
practice this is the single largest source of Temporal operational pain, and it
is inherent to the model rather than a rough edge that will be filed off.

## Checkpointing (DBOS, Restate, Sayiir)

Each step's output is persisted as it completes. On recovery, completed steps
are skipped and execution continues from the first incomplete one.

**What it buys.** Ordinary code. No determinism rules, no replay semantics to
reason about, and a much simpler versioning story.

**What it costs.** Every step output must serialise, and must be small enough
to store cheaply. State held only in memory between steps is lost — if you want
it after a crash, it has to be a step output.

## Why this workload sits on the checkpointing side

The pipeline is four steps with a fixed shape. The outputs are a string, a
dict, a list of strings, and an opaque id — all JSON-native, all small once OCR
text is behind a storage key. There are no loops, no dynamic branching, and no
accumulated in-memory state between steps.

**Deterministic replay's payoff is entirely unused here, and its cost is paid
in full.** We would accept determinism constraints and workflow-versioning
ceremony in exchange for durable arbitrary control flow that this DAG does not
have. That is a bad trade, and it is the substantive argument for DBOS over
Temporal — not "Temporal is heavy".

## Where the thesis flips

Be clear about what would change the answer:

- Workflows whose intermediate state is large, or not serialisable at all.
- Long-lived workflows with many suspension points and frequently changing
  code, where Temporal's versioning story turns from a cost into an asset.
- A need to query running workflows, or to treat the event history itself as a
  compliance artifact — plausible for a regulated-archive product.
- Highly dynamic fan-out where the DAG is computed at runtime.

The first and third are the ones to watch in this domain.
