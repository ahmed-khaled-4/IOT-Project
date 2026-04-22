from __future__ import annotations

import time
from collections import OrderedDict
from threading import Lock
from typing import Hashable, Optional, Tuple


class DupFilter:
    """A small thread-safe LRU/TTL cache used to detect and drop
    duplicate messages.

    Rationale
    ---------
    MQTT QoS 1 (and sometimes QoS 2) retransmissions raise the DUP flag
    on the wire. CoAP CON retransmissions reuse the same ``(source, token)``
    tuple. Both can cause the application to process the same event twice
    if we only look at payload contents. This filter keeps the last
    ``max_size`` identifiers for ``ttl_sec`` seconds; repeat hits are
    reported to the caller which then SHOULD discard the message.
    """

    def __init__(self, *, max_size: int = 1000, ttl_sec: float = 60.0):
        self._max_size = int(max_size)
        self._ttl_sec = float(ttl_sec)
        self._store: "OrderedDict[Hashable, float]" = OrderedDict()
        self._lock = Lock()

    def _evict_expired(self, now: float) -> None:
        cutoff = now - self._ttl_sec
        keys_to_drop = []
        for k, ts in self._store.items():
            if ts < cutoff:
                keys_to_drop.append(k)
            else:
                break
        for k in keys_to_drop:
            self._store.pop(k, None)

    def seen(self, key: Hashable) -> bool:
        """Return ``True`` if ``key`` was already seen within the TTL
        window. Always records the key as seen-now."""

        now = time.monotonic()
        with self._lock:
            self._evict_expired(now)
            if key in self._store:
                self._store.move_to_end(key)
                self._store[key] = now
                return True
            self._store[key] = now
            if len(self._store) > self._max_size:
                self._store.popitem(last=False)
            return False

    def size(self) -> int:
        with self._lock:
            return len(self._store)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


def build_mqtt_key(topic: str, packet_id: Optional[int], payload_hash: Optional[int] = None) -> Tuple:
    """Key for MQTT dedup. Prefer ``packet_id`` when available (QoS>0);
    fall back to a payload hash for QoS 0."""

    if packet_id is not None:
        return ("mqtt", topic, int(packet_id))
    return ("mqtt", topic, int(payload_hash or 0))


def build_coap_key(source: str, token: bytes | str) -> Tuple:
    """Key for CoAP dedup."""

    if isinstance(token, (bytes, bytearray)):
        token = token.hex()
    return ("coap", str(source), str(token))
