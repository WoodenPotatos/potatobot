"""One clock for every stored timestamp.

Twenty-six sites in `cogs/` and eleven in `core/database.py` called
`datetime.now()` -- the server's *local* time, with no offset -- while forty-four
others used `datetime.now(timezone.utc)`. A cooldown that subtracts one from the
other is wrong by the offset, and a cooldown that subtracts two naive local
values is wrong by an hour twice a year, when the clocks change under it.

Two rules, and the second is the one that matters for the rows already stored:

- **Every new timestamp is UTC-aware.** `utc_now()` is the only clock.
- **A stored value with no offset is local time, never UTC.** Months of rows were
  written by `datetime.now().isoformat()` on a host in Europe/Budapest. Reading
  one as UTC would move every existing cooldown by the offset, once, on the day
  this shipped. `parse_stored` therefore interprets a naive value as the host's
  local time -- `astimezone()` on a naive datetime does exactly that -- and
  converts. New rows carry `+00:00` and parse as written.

Calendar days are the exception to "everything is UTC". "Played today", "a
daily streak" and "the deck for today" mean the operator's day, and a member in
the same country as the host expects midnight to be midnight. `local_date`
gives the local day of an instant, so a day-gate keeps the boundary it always
had while the arithmetic underneath it stops caring about the clock change.
"""

from datetime import date, datetime, timezone


def utc_now() -> datetime:
    """The current instant, aware, in UTC. The only clock a write may use."""
    return datetime.now(timezone.utc)


def parse_stored(text: str | datetime) -> datetime:
    """An aware UTC datetime from a stored ISO-8601 string, or from the
    `datetime` a PostgreSQL `timestamptz` column reads back as.

    A value with no offset is a legacy *local* timestamp and is read as such;
    reading it as UTC would shift it by the host's offset. Raises `ValueError`
    on text that is not a timestamp, exactly as `fromisoformat` does.
    """
    parsed = text if isinstance(text, datetime) else datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()  # naive is local time; this makes it aware
    return parsed.astimezone(timezone.utc)


def stored_iso(value: str | datetime | None) -> str | None:
    """A stored timestamp as ISO-8601 text, whatever the engine handed back.

    SQLite returns the stored string, which passes through unchanged, so what
    leaves today is byte-identical; PostgreSQL returns a `datetime`, which
    becomes `.isoformat()`. For anything that leaves as text -- a JSON payload,
    the export, the dashboard wire -- where a `datetime` either fails to encode
    or, under Flask's encoder, silently becomes an RFC 822 date.
    """
    return value.isoformat() if isinstance(value, datetime) else value


def local_time(moment: datetime) -> datetime:
    """An aware instant expressed in the host's local zone, for display."""
    return moment.astimezone()


def local_date(moment: datetime) -> date:
    """The calendar day an instant falls on for the host -- 'today' as a member
    in the operator's country means it."""
    return moment.astimezone().date()
