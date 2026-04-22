"""Security helpers: load TLS contexts and DTLS PSKs for the hybrid engine.

The PKI artifacts live under ``infra/certs``:

  - ``ca/ca.crt``                 Campus root CA
  - ``nodes/<room_id>.{crt,key}`` Per-MQTT-node client cert
  - ``coap_psk.json``             {room_id -> 64-char hex PSK}
  - ``node_credentials.json``     {room_id -> {username,password}}

Functions here build :class:`ssl.SSLContext` objects that ``gmqtt``
accepts via its ``ssl=`` kwarg, and deliver a plain dict for
:mod:`aiocoap.credentials` to load when DTLS is enabled.
"""

from __future__ import annotations

import json
import os
import ssl
from pathlib import Path
from typing import Dict, Optional, Tuple


def _certs_root() -> Path:
    env = os.environ.get("CERTS_ROOT")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "infra" / "certs"


def load_mqtt_credentials() -> Dict[str, Tuple[str, str]]:
    """Return ``{room_id -> (username, password)}`` read from
    ``infra/certs/node_credentials.json``. Returns an empty dict if the
    file does not exist (anonymous/dev mode)."""

    path = _certs_root() / "node_credentials.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k: (v["username"], v["password"]) for k, v in data.items()}


def load_coap_psk() -> Dict[str, bytes]:
    """Return ``{room_id -> 32-byte PSK}`` from ``coap_psk.json``."""

    path = _certs_root() / "coap_psk.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k: bytes.fromhex(v) for k, v in data.items()}


def build_mqtt_tls_ctx(room_id: Optional[str] = None) -> Optional[ssl.SSLContext]:
    """Build an ``SSLContext`` for HiveMQ mTLS.

    - If ``infra/certs/ca/ca.crt`` is missing, returns ``None`` (TLS off).
    - If ``room_id`` is provided and a per-node cert exists, the context
      is configured with client authentication using that cert.
    """

    root = _certs_root()
    ca = root / "ca" / "ca.crt"
    if not ca.exists():
        return None

    ctx = ssl.create_default_context(cafile=str(ca))
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED

    if room_id is not None:
        cert = root / "nodes" / f"{room_id}.crt"
        key = root / "nodes" / f"{room_id}.key"
        if cert.exists() and key.exists():
            ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))

    return ctx
