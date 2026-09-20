from datetime import datetime, timezone

from fastapi import Request


class Clock:
    """Wall clock. Replaced with a controllable fake in tests via app.state.clock."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


def get_clock(request: Request) -> Clock:
    return request.app.state.clock
