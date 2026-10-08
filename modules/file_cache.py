"""Recompute a value from files only when they changed (cheap polling for the GUI).

`os.stat` is far cheaper than reading and parsing, and needs no watcher
library, so it works the same on every platform. The signature is
(size, mtime_ns) per file; a periodic forced refresh (`max_age`) covers
filesystems with coarse timestamps, where an edit could keep both unchanged.
"""

from __future__ import annotations

import os
import time
from typing import Callable, Generic, Iterable, Optional, TypeVar

T = TypeVar("T")

Signature = tuple[Optional[tuple[int, int]], ...]


def file_signature(paths: Iterable[str]) -> Signature:
    """(size, mtime_ns) for each path, or None for a file that does not exist."""
    signature: list[Optional[tuple[int, int]]] = []
    for path in paths:
        try:
            info = os.stat(path)
            signature.append((info.st_size, info.st_mtime_ns))
        except OSError:
            signature.append(None)
    return tuple(signature)


class StatCache(Generic[T]):
    """Cache `compute()` and reuse it until the watched files change or `max_age` seconds pass.

    `paths` is called on every lookup, so a path that changes (tests, a moved
    app folder) is picked up. A `compute` that raises caches nothing.
    """

    def __init__(
        self,
        paths: Callable[[], Iterable[str]],
        compute: Callable[[], T],
        max_age: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Take a function listing the watched paths, the function computing the value, and the safety-refresh age."""
        self._paths = paths
        self._compute = compute
        self._max_age = max_age
        self._clock = clock
        self._has_value = False
        self._value: Optional[T] = None
        self._signature: Optional[tuple[tuple[str, ...], Signature]] = None
        self._computed_at = 0.0

    def get(self) -> T:
        """Return the cached value, recomputing it when a watched file changed or the safety age has passed."""
        paths = tuple(self._paths())
        key = (paths, file_signature(paths))
        now = self._clock()
        if self._has_value and key == self._signature and now - self._computed_at < self._max_age:
            return self._value  # type: ignore[return-value]
        value = self._compute()
        self._value, self._signature, self._computed_at, self._has_value = value, key, now, True
        return value

    def invalidate(self) -> None:
        """Forget the cached value (the next get() recomputes)."""
        self._has_value = False
