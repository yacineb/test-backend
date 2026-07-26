"""Where the retry policy and the worker sizing come from.

    uv run python scripts/simulate_pipeline.py

Models the provider mocks exactly. The one detail that drives every number:
`time.sleep()` runs BEFORE the failure check, so a failed attempt costs the
same wall-clock as a successful one. Retrying is not cheap here.

Structure: ocr -> (metadata || chunking) -> external_call.
Target from the statement: p95 of total pipeline time < 120s.
"""

import random
import statistics

N = 200_000
P_FAIL = 1 / 3
DOCS_PER_DAY_TARGET = 100_000

STEPS = {
    "ocr": (1, 15),
    "metadata": (1, 10),
    "chunking": (1, 12),
    "external_call": (1, 5),
}


def run_step(name, max_attempts, backoff):
    """Return (elapsed, attempts, ok). Elapsed includes failed attempts."""
    low, high = STEPS[name]
    elapsed = 0.0
    for attempt in range(max_attempts):
        elapsed += random.uniform(low, high)
        if random.random() >= P_FAIL:
            return elapsed, attempt + 1, True
        if attempt + 1 < max_attempts:
            elapsed += backoff(attempt)
    return elapsed, max_attempts, False


def pipeline(max_attempts, backoff, parallel=True):
    """Return (wall_clock, step_seconds, attempts, ok).

    wall_clock sizes latency; step_seconds sizes the worker pool. They differ
    because the two middle steps overlap.
    """
    wall, busy, attempts = 0.0, 0.0, 0

    elapsed, n, ok = run_step("ocr", max_attempts, backoff)
    wall += elapsed
    busy += elapsed
    attempts += n
    if not ok:
        return wall, busy, attempts, False

    t_meta, n_meta, ok_meta = run_step("metadata", max_attempts, backoff)
    t_chunk, n_chunk, ok_chunk = run_step("chunking", max_attempts, backoff)
    wall += max(t_meta, t_chunk) if parallel else t_meta + t_chunk
    busy += t_meta + t_chunk
    attempts += n_meta + n_chunk
    if not (ok_meta and ok_chunk):
        return wall, busy, attempts, False

    elapsed, n, ok = run_step("external_call", max_attempts, backoff)
    return wall + elapsed, busy + elapsed, attempts + n, ok


def percentile(values, p):
    return statistics.quantiles(values, n=1000)[p * 10 - 1]


def measure(max_attempts, backoff, parallel=True):
    walls, busies, attempts, failures = [], [], [], 0
    for _ in range(N):
        wall, busy, n, ok = pipeline(max_attempts, backoff, parallel)
        busies.append(busy)
        attempts.append(n)
        if ok:
            walls.append(wall)
        else:
            failures += 1
    return walls, busies, attempts, failures


def report(label, max_attempts, backoff, parallel=True):
    walls, busies, attempts, failures = measure(max_attempts, backoff, parallel)
    flag = "" if percentile(walls, 95) < 120 else "  <-- MISSES p95 TARGET"
    print(
        f"{label:<38} p50={statistics.median(walls):6.1f}s "
        f"p95={percentile(walls, 95):6.1f}s p99={percentile(walls, 99):6.1f}s "
        f"give-up={100 * failures / N:5.2f}%{flag}"
    )
    return statistics.mean(busies), statistics.mean(attempts)


NO_BACKOFF = lambda attempt: 0.0  # noqa: E731
FIXED_1S = lambda attempt: 1.0  # noqa: E731
EXPO_1S = lambda attempt: 2.0**attempt  # 1, 2, 4, 8  <- shipped  # noqa: E731
EXPO_5S = lambda attempt: 5.0 * 2**attempt  # 5, 10, 20, 40  # noqa: E731


def main():
    print(f"n={N:,}  p_fail=1/3 per attempt  target: p95 < 120s\n")

    print("--- retry policy (metadata || chunking in parallel) ---")
    for attempts in (1, 2, 3, 4, 5, 6):
        report(f"{attempts} attempt(s), no backoff", attempts, NO_BACKOFF)
    print()
    report("5 attempts, fixed 1s", 5, FIXED_1S)
    busy, execs = report("5 attempts, expo 1/2/4/8s  [SHIPPED]", 5, EXPO_1S)
    report("5 attempts, expo 5/10/20/40s", 5, EXPO_5S)

    print("\n--- what the fan-out buys ---")
    report("5 attempts, expo, parallel", 5, EXPO_1S, parallel=True)
    report("5 attempts, expo, serial", 5, EXPO_1S, parallel=False)

    print("\n--- capacity at the 12-month target (100k docs/day) ---")
    print(f"mean step-seconds per document : {busy:.1f}s")
    print(f"mean step executions per doc   : {execs:.1f}")
    for label, hours, burst in (
        ("uniform over 24h", 24, 1),
        ("8h business day", 8, 1),
        ("8h day, 3x burst", 8, 3),
    ):
        rate = DOCS_PER_DAY_TARGET / (hours * 3600) * burst
        print(
            f"  {label:<20} {rate:5.2f} docs/s -> "
            f"{rate * execs:6.1f} step-exec/s, "
            f"{rate * busy:5.0f} concurrent step slots"
        )
    print("\nToday's load (1k docs/day) is 1/100th of the first row above.")


if __name__ == "__main__":
    main()
