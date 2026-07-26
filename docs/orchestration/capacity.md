# Capacity: why throughput is not the trigger

Numbers from `scripts/simulate_pipeline.py`, which models the provider mocks
exactly. The detail that drives everything: `time.sleep()` runs *before* the
failure check, so a failed attempt costs the same wall-clock as a successful
one.

## Per document

With the shipped policy (5 attempts, 1/2/4/8s backoff):

| quantity | value |
|---|---|
| wall-clock p50 | 28.5s |
| wall-clock p95 | 56.9s |
| step-seconds consumed | 37.5s |
| step executions incl. retries | 5.9 |
| success rate | 98.4% |

Wall-clock and step-seconds differ because `metadata` and `chunking` overlap.
The first sizes latency; the second sizes the worker pool.

## At the 12-month target (100,000 documents/day)

| shape | rate | step-executions/s | concurrent step slots |
|---|---|---|---|
| uniform over 24h | 1.16 docs/s | 6.9 | 43 |
| 8h business day | 3.47 docs/s | 20.7 | 130 |
| 8h day, 3× burst | 10.42 docs/s | 62.0 | 390 |

Postgres `SELECT ... FOR UPDATE SKIP LOCKED` sustains thousands of claims per
second on a single node. The worst case above needs **62**.

Today's 1,000 documents/day is one hundredth of the first row.

## The conclusion

Every candidate engine clears this bar with two orders of magnitude to spare.
**Choosing on throughput means choosing on a number that does not
discriminate.** The trigger to change orchestrator is workflow complexity, not
volume — see [recommendation.md](recommendation.md).

## The constraint that does bind

Not throughput: **threads**. The provider mocks call a blocking `time.sleep()`,
so every in-flight step holds one. At today's load that is ~0.4 concurrent
steps and irrelevant; at 390 it is too many OS threads to be comfortable.

The mock's sleep stands in for network I/O, so the fix is not more threads — it
is that these become `await`ed HTTP calls once the providers are real, at which
point 390 concurrent coroutines is nothing. See [pipeline.md](../pipeline.md).

Queue polling interval (`PIPELINE_QUEUE_POLLING_INTERVAL_SECONDS`, default
1.0s) is pure added latency and lands twice per document, spending ~2s of the
~63s headroom the p95 target leaves.
