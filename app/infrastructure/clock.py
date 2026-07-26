from datetime import UTC, datetime


class SystemClock:
    """Real time, always timezone-aware UTC. Tests substitute a frozen clock."""

    def now(self) -> datetime:
        return datetime.now(UTC)
