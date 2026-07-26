"""The retry policy is a measurement, not a preference.

scripts/simulate_pipeline.py models the mocks exactly - the sleep is paid
before the failure check - over 200k pipelines:

    attempts  backoff        p50     p95     p99     give-up
    1         -              18.7s   26.6s   28.7s   80.16%
    3         none           25.3s   42.6s   51.1s   14.09%
    5         none           26.6s   48.5s   60.4s    1.60%
    5         1/2/4/8s       28.5s   56.9s   73.7s    1.64%   <- shipped
    5         5/10/20/40s    35.3s   95.1s  137.3s    1.66%   <- misses target

The target is p95 < 120s. See docs/orchestration/capacity.md.
"""

from app.config import PipelineSettings


def test_policy_matches_the_simulated_numbers():
    settings = PipelineSettings()

    assert settings.max_attempts == 5
    assert settings.retry_interval_seconds == 1.0
    assert settings.retry_backoff_rate == 2.0


def test_retries_are_enabled_on_every_step():
    """DBOS ships retries_allowed=False and max_attempts=3. Leaving those
    defaults would take the give-up rate from 1.6% to 14% - at the 12-month
    target, 14,000 dead documents a day."""
    from app.pipeline.tasks import RETRY_POLICY

    assert RETRY_POLICY["retries_allowed"] is True
    assert RETRY_POLICY["max_attempts"] == PipelineSettings().max_attempts
    assert RETRY_POLICY["backoff_rate"] == PipelineSettings().retry_backoff_rate
