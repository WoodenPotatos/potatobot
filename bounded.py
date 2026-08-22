"""Transient in-memory maps that cannot grow for the process lifetime.

These live in their own module so both `cogs.utils` and `feature_access` can use
them without either importing the other. Every short-lived per-user, per-channel
or per-interaction map in the bot must use one of these instead of a plain dict.
"""

import threading
import time


class BoundedCooldownMap(dict):
    """A monotonic-time map that evicts expired and then oldest entries."""

    def __init__(self, max_age=7200, max_entries=4096):
        super().__init__()
        self.max_age = max_age
        self.max_entries = max_entries

    def __setitem__(self, key, value):
        if key not in self and len(self) >= self.max_entries:
            cutoff = time.monotonic() - self.max_age
            for old_key, timestamp in list(self.items()):
                if timestamp < cutoff:
                    super().__delitem__(old_key)
            if len(self) >= self.max_entries:
                oldest = min(self, key=self.get)
                super().__delitem__(oldest)
        super().__setitem__(key, value)


class BoundedValueMap(dict):
    """An insertion-ordered map with a hard capacity, for non-numeric values.

    Eviction is oldest-first, which suits caches keyed by a Discord snowflake
    whose values are not comparable timestamps.
    """

    def __init__(self, max_entries=4096):
        super().__init__()
        self.max_entries = max_entries

    def __setitem__(self, key, value):
        if key in self:
            super().__delitem__(key)
        elif len(self) >= self.max_entries:
            super().__delitem__(next(iter(self)))
        super().__setitem__(key, value)


class BoundedTimestampMap:
    """A thread-safe monotonic-time map used from more than one thread.

    `BoundedCooldownMap` is only touched from the event loop, but interaction
    timing is written by the command tree and read by the response wrapper, so
    this variant owns its own lock and exposes just the operations needed.
    """

    def __init__(self, max_age=900, max_entries=4096):
        self._entries = {}
        self._lock = threading.Lock()
        self.max_age = max_age
        self.max_entries = max_entries

    def __len__(self):
        with self._lock:
            return len(self._entries)

    def __contains__(self, key):
        with self._lock:
            return key in self._entries

    def start(self, key, value):
        """Record a start time, discarding entries no completion ever claimed."""
        with self._lock:
            if len(self._entries) >= self.max_entries:
                cutoff = value - self.max_age
                for old_key, started in list(self._entries.items()):
                    if started < cutoff:
                        del self._entries[old_key]
                while len(self._entries) >= self.max_entries:
                    del self._entries[next(iter(self._entries))]
            self._entries[key] = value

    def pop(self, key, default=None):
        with self._lock:
            return self._entries.pop(key, default)
