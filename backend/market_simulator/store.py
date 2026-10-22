"""Bounded per-login ephemeral snapshots, never persisted to Redis/SQL."""
from collections import deque
from copy import deepcopy
from datetime import timedelta
from secrets import token_urlsafe
from threading import RLock
import sys
from .models import SimulatorError


def size_of(obj, seen=None):
    seen = set() if seen is None else seen
    if id(obj) in seen:
        return 0
    seen.add(id(obj))
    size = sys.getsizeof(obj)
    if isinstance(obj, dict):
        size += sum(size_of(k, seen)+size_of(v, seen) for k, v in obj.items())
    elif isinstance(obj, (list, tuple, deque)):
        size += sum(size_of(v, seen) for v in obj)
    return size


class SnapshotStore:
    def __init__(self, max_total=128, max_bytes=64*1024*1024):
        self._items, self._rates = {}, {}
        self._lock = RLock()
        self.max_total, self.max_bytes = max_total, max_bytes

    def _clean(self, now):
        for sid in list(self._items):
            if self._items[sid][2] <= now:
                del self._items[sid]
        for key in list(self._rates):
            while self._rates[key] and (now-self._rates[key][0]).total_seconds() >= 1:
                self._rates[key].popleft()
            if not self._rates[key]:
                del self._rates[key]

    def _bytes(self):
        return size_of(self._items)+size_of(self._rates)

    def rate(self, owner, session, operation, now):
        with self._lock:
            self._clean(now)
            key = (owner, session, operation)
            limit = 1 if operation == 'create' else 10
            if len(self._rates.get(key, ())) >= limit or len(self._rates) >= 4096 or self._bytes()+2048 > self.max_bytes:
                raise SimulatorError('RATE_LIMIT', '操作过于频繁，请稍后重试', 429)
            self._rates.setdefault(key, deque()).append(now)

    def put(self, owner, session, data, now):
        with self._lock:
            self._clean(now)
            if len(self._items) >= self.max_total or sum(v[0] == owner for v in self._items.values()) >= 4:
                raise SimulatorError('SNAPSHOT_LIMIT', '临时基准数量已达上限，请关闭旧面板', 429)
            frozen = deepcopy(data)
            sid = token_urlsafe(24)
            frozen.update(snapshot_id=sid, expires_at=now+timedelta(minutes=30))
            entry = (owner, session, frozen['expires_at'], frozen)
            if self._bytes()+size_of(entry)+size_of(sid)+1024 > self.max_bytes:
                raise SimulatorError('MEMORY_LIMIT', '临时基准内存已达上限', 429)
            self._items[sid] = entry
            return sid

    def get(self, owner, session, sid, now):
        with self._lock:
            self._clean(now)
            entry = self._items.get(sid)
            if not entry or entry[:2] != (owner, session):
                raise SimulatorError('SNAPSHOT_UNAVAILABLE', '基准不可用，请更新基准', 404)
            return deepcopy(entry[3])

    def delete(self, owner, session, sid, now):
        with self._lock:
            self.get(owner, session, sid, now)
            del self._items[sid]
