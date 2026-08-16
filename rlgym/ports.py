"""Host-port allocation for ephemeral gym stacks.

The queue *is* the semaphore. `claim()` blocks on `get()` when every port is in use, so the
allocator alone bounds how many stacks exist -- there is no pool manager, slot table or lease
bookkeeping anywhere in the system.

MAX_GYMS = 3 is measured, not guessed. The Docker VM has 4 CPUs / 7.74 GiB; steady-state RSS
is ~1.08 GiB for Metabase and ~54 MiB for Postgres, so three stacks are ~3.4 GiB with headroom
for JVM boot spikes. Memory is the ceiling now, not API rate limits.
"""

from __future__ import annotations

import queue
from contextlib import contextmanager
from typing import Iterator

MAX_GYMS = 3
BASE_PORT = 3000

# 3001..3000+MAX_GYMS. 3000 is left free for a manual `--keep` stack so a dev session and a
# job can coexist.
_free: queue.Queue[int] = queue.Queue()
for _port in range(BASE_PORT + 1, BASE_PORT + 1 + MAX_GYMS):
    _free.put(_port)


@contextmanager
def claim(timeout: float | None = None) -> Iterator[int]:
    """Reserve a host port for the duration of the block.

    Blocks while all ports are taken. The `finally` is unconditional: a port must return to
    the pool even if provisioning raised, or the platform leaks capacity until restart.
    """
    port = _free.get(timeout=timeout)
    try:
        yield port
    finally:
        _free.put(port)


def available() -> int:
    """Ports not currently claimed. Approximate by nature -- for diagnostics only."""
    return _free.qsize()
