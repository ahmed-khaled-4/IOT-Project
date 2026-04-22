from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, Optional


CommandHandler = Callable[[str, Dict[str, Any]], Awaitable[None] | None]


class TransportKind(str, Enum):
    MQTT = "mqtt"
    COAP = "coap"


class RoomTransport(ABC):
    """Abstract bearer used by :mod:`engine.main` to publish telemetry /
    heartbeat and to receive downstream commands, regardless of whether
    the underlying protocol is MQTT or CoAP.
    """

    kind: TransportKind

    @abstractmethod
    async def start(self) -> None:
        """Open sockets, connect, subscribe. Non-blocking (returns once
        background tasks are scheduled)."""

    @abstractmethod
    async def stop(self) -> None:
        """Disconnect gracefully. Idempotent."""

    @abstractmethod
    def publish_telemetry(self, room: Any, payload: Dict[str, Any]) -> None:
        """Publish room telemetry. Non-blocking."""

    @abstractmethod
    def publish_heartbeat(self, room: Any) -> None:
        """Publish a liveness heartbeat."""

    def publish_response(self, room: Any, payload: Dict[str, Any]) -> None:
        """Optional: ack a command back upstream. Default no-op."""

    def publish_status(self, room: Any, status: str) -> None:
        """Optional: publish a status/LWT-equivalent message."""

    @abstractmethod
    def register_command_handler(self, handler: CommandHandler) -> None:
        """Install the callback that receives validated commands.

        Handler signature: ``handler(room_id: str, payload: dict)``.
        """

    @property
    @abstractmethod
    def connected(self) -> bool:
        ...
