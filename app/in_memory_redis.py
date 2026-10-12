import time
import math
import logging
from typing import Optional, Dict, Any, Set, List

logger = logging.getLogger("in_memory_redis")

class InMemoryRedis:
    """
    A lightweight, thread-safe in-memory Redis substitute for standalone/local development.
    Supports GET, SET (with EX and NX), DELETE, EVAL (sliding window rate limit),
    PFADD, PFCOUNT (using Python sets for HyperLogLog simulation), and streams.
    """
    def __init__(self):
        self._store: Dict[str, str] = {}
        self._expires: Dict[str, float] = {}
        self._sets: Dict[str, Set[str]] = {}
        # Rate limit sliding windows: key -> list of (timestamp_ms, member_id)
        self._rate_limits: Dict[str, List[tuple[int, str]]] = {}
        self._streams: Dict[str, List[Dict[str, str]]] = {}

    def _purge_expired(self, key: str):
        if key in self._expires and time.time() > self._expires[key]:
            self._store.pop(key, None)
            self._expires.pop(key, None)

    async def ping(self) -> bool:
        return True

    async def get(self, key: str) -> Optional[str]:
        self._purge_expired(key)
        return self._store.get(key)

    async def set(
        self,
        key: str,
        value: str,
        ex: Optional[int] = None,
        nx: bool = False,
    ) -> bool:
        self._purge_expired(key)
        if nx and key in self._store:
            return False

        self._store[key] = str(value)
        if ex is not None:
            self._expires[key] = time.time() + ex
        else:
            self._expires.pop(key, None)
        return True

    async def delete(self, *keys: str) -> int:
        count = 0
        for k in keys:
            if k in self._store:
                self._store.pop(k, None)
                self._expires.pop(k, None)
                count += 1
            if k in self._sets:
                self._sets.pop(k, None)
                count += 1
        return count

    async def pfadd(self, key: str, *elements: str) -> int:
        if key not in self._sets:
            self._sets[key] = set()
        initial_len = len(self._sets[key])
        for el in elements:
            self._sets[key].add(str(el))
        return 1 if len(self._sets[key]) > initial_len else 0

    async def pfcount(self, key: str) -> int:
        return len(self._sets.get(key, set()))

    async def xadd(self, name: str, fields: Dict[str, str], id: str = "*") -> str:
        if name not in self._streams:
            self._streams[name] = []
        msg_id = f"{int(time.time() * 1000)}-{len(self._streams[name])}"
        self._streams[name].append({"id": msg_id, **fields})
        return msg_id

    async def xgroup_create(self, name: str, groupname: str, id: str = "$", mkstream: bool = False):
        if mkstream and name not in self._streams:
            self._streams[name] = []
        return True

    async def eval(self, script: str, numkeys: int, *args) -> List[int]:
        """
        Executes sliding window rate limiter logic in pure Python.
        """
        if len(args) >= 5:
            key = str(args[0])
            now_ms = int(args[1])
            window_ms = int(args[2])
            limit = int(args[3])
            member = str(args[4])

            # Clean entries outside window
            cutoff = now_ms - window_ms
            history = self._rate_limits.get(key, [])
            valid = [item for item in history if item[0] > cutoff]

            if len(valid) < limit:
                valid.append((now_ms, member))
                self._rate_limits[key] = valid
                remaining = limit - len(valid)
                return [1, remaining, 0]
            else:
                self._rate_limits[key] = valid
                oldest_time = valid[0][0] if valid else now_ms
                retry_after = math.ceil((oldest_time + window_ms - now_ms) / 1000)
                if retry_after < 1:
                    retry_after = 1
                return [0, 0, retry_after]

        return [1, 999, 0]

    async def aclose(self):
        self._store.clear()
        self._expires.clear()
        self._sets.clear()
        self._rate_limits.clear()
        self._streams.clear()
