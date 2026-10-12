"""Anonymous counts per day: how often the site, the chat and each tool are used, never by whom.

The counts live in memory and go to the log with the first count of every hour and when the server stops, so
the Railway logs show what people use. Search the logs for "Tellers".
"""

import atexit
from collections import Counter
from contextvars import ContextVar
from datetime import datetime

from .common import now_local

# The chat sets this while it looks something up, so its lookups are counted apart from the ones through MCP.
via: ContextVar[str] = ContextVar("via", default="mcp")

_counts: Counter[str] = Counter()
_since: datetime = now_local()
_hour: tuple = (_since.date(), _since.hour)


def tel(what: str) -> None:
    """Count one event, like "site" or "chat via link"."""
    _turn()
    _counts[what] += 1


def tool(name: str) -> None:
    tel(f"{via.get()} {name}")


def _turn() -> None:
    global _since, _hour
    now = now_local()
    if (now.date(), now.hour) == _hour:
        return
    write()
    if now.date() != _hour[0]:
        _counts.clear()
        _since = now.replace(hour=0, minute=0, second=0, microsecond=0)
    _hour = (now.date(), now.hour)


def write() -> None:
    # A plain line on stdout: the log handler of the MCP library wraps long lines, which splits them in the Railway logs.
    if _counts:
        counts = ", ".join(f"{what} {n}" for what, n in sorted(_counts.items()))
        print(f"Tellers {_since.date().isoformat()} sinds {_since.strftime('%H:%M')}: {counts}", flush=True)


atexit.register(write)
