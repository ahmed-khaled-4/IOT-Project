"""Transport abstraction for the hybrid Phase 2 World Engine.

Two concrete transports are provided:

- :class:`engine.transport.mqtt_transport.MQTTTransport` — one gmqtt client
  per MQTT room, persistent TCP to HiveMQ, with LWT + QoS 2 commands.
- :class:`engine.transport.coap_transport.CoAPTransport` — one aiocoap server
  per CoAP room, exposing an Observable telemetry resource (RFC 7641) and a
  CON-PUT actuator resource.

Both inherit from :class:`RoomTransport` and implement identical methods
so that ``engine.main`` can treat every room uniformly.
"""

from .base import CommandHandler, RoomTransport, TransportKind
from .dup_filter import DupFilter

__all__ = ["RoomTransport", "CommandHandler", "TransportKind", "DupFilter"]
