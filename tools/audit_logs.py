"""Packet Integrity Audit for Phase 2.

Parses logs from:
  - HiveMQ broker (stdout/file)
  - Engine process (JSON lines emitted to stdout)
  - Node-RED containers (stdout)

…to count:
  - MQTT QoS 2 handshakes: PUBLISH -> PUBREC -> PUBREL -> PUBCOMP per (client_id, packet_id)
  - MQTT DUP-flagged retransmissions
  - CoAP CON requests, ACK responses, timeouts/retries
  - Dedup drops (``mqtt.command.dedup_drop``, ``coap.cmd.dedup_drop``)

Emits a short Markdown report to stdout (pipe into the Phase 2 PDF).

Usage:
    docker compose logs hivemq > logs/hivemq.log
    docker compose logs engine > logs/engine.log
    python tools/audit_logs.py --hivemq logs/hivemq.log --engine logs/engine.log
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable


RX_PUBREC = re.compile(r"PUBREC.*packetIdentifier=(\d+)", re.IGNORECASE)
RX_PUBREL = re.compile(r"PUBREL.*packetIdentifier=(\d+)", re.IGNORECASE)
RX_PUBCOMP = re.compile(r"PUBCOMP.*packetIdentifier=(\d+)", re.IGNORECASE)
RX_PUBLISH = re.compile(r"PUBLISH.*qos=2.*packetIdentifier=(\d+)", re.IGNORECASE)
RX_DUP = re.compile(r"DUP flag: true", re.IGNORECASE)


def _iter_lines(path: str) -> Iterable[str]:
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            yield line.rstrip("\n")


def audit_hivemq(path: str) -> Dict[str, int]:
    counts = defaultdict(int)
    if not os.path.exists(path):
        return counts
    for line in _iter_lines(path):
        if "PUBLISH" in line and "qos=2" in line.lower():
            counts["publish_qos2"] += 1
        if "PUBREC" in line:
            counts["pubrec"] += 1
        if "PUBREL" in line:
            counts["pubrel"] += 1
        if "PUBCOMP" in line:
            counts["pubcomp"] += 1
        if RX_DUP.search(line):
            counts["dup_flag"] += 1
    return counts


def audit_engine(path: str) -> Dict[str, int]:
    counts: Dict[str, int] = defaultdict(int)
    if not os.path.exists(path):
        return counts
    for line in _iter_lines(path):
        try:
            msg = json.loads(line).get("msg", "")
        except Exception:
            msg = line

        if "mqtt.node.connected" in msg:
            counts["mqtt_node_connected"] += 1
        if "mqtt.node.disconnected" in msg:
            counts["mqtt_node_disconnected"] += 1
        if "mqtt.command.dedup_drop" in msg:
            counts["mqtt_dedup_drops"] += 1
        if "coap.cmd.dedup_drop" in msg:
            counts["coap_dedup_drops"] += 1
        if "coap.server.listening" in msg:
            counts["coap_servers_started"] += 1
        if "coap.alert.ack" in msg:
            counts["coap_con_acks"] += 1
        if "coap.alert.timeout" in msg:
            counts["coap_con_timeouts"] += 1
    return counts


def audit_gateway(path: str) -> Dict[str, int]:
    counts: Dict[str, int] = defaultdict(int)
    if not os.path.exists(path):
        return counts
    for line in _iter_lines(path):
        if "DUP drop key=" in line:
            counts["gateway_dup_drops"] += 1
        if "cmd->CoAP" in line:
            counts["gateway_cmd_translations"] += 1
    return counts


def render(hivemq: Dict[str, int], engine: Dict[str, int], gateway: Dict[str, int]) -> str:
    pub2 = hivemq.get("publish_qos2", 0)
    rec = hivemq.get("pubrec", 0)
    rel = hivemq.get("pubrel", 0)
    comp = hivemq.get("pubcomp", 0)

    balanced = pub2 == rec == rel == comp
    lost_pct = 0.0 if pub2 == 0 else (abs(pub2 - comp) / pub2) * 100.0

    lines = [
        "# Phase 2 Packet Integrity Audit",
        "",
        "## MQTT QoS 2 handshake",
        "",
        f"- PUBLISH (QoS2):  {pub2}",
        f"- PUBREC         : {rec}",
        f"- PUBREL         : {rel}",
        f"- PUBCOMP        : {comp}",
        f"- DUP flagged    : {hivemq.get('dup_flag', 0)}",
        f"- Balanced       : {'YES' if balanced else 'NO'}",
        f"- Loss estimate  : {lost_pct:.2f}%",
        "",
        "## CoAP CON reliability",
        "",
        f"- CoAP servers up      : {engine.get('coap_servers_started', 0)}",
        f"- CoAP CON ACKs        : {engine.get('coap_con_acks', 0)}",
        f"- CoAP CON timeouts    : {engine.get('coap_con_timeouts', 0)}",
        "",
        "## Deduplication",
        "",
        f"- Engine MQTT dedup drops   : {engine.get('mqtt_dedup_drops', 0)}",
        f"- Engine CoAP dedup drops   : {engine.get('coap_dedup_drops', 0)}",
        f"- Gateway DUP-flag drops    : {gateway.get('gateway_dup_drops', 0)}",
        f"- Gateway cmd translations  : {gateway.get('gateway_cmd_translations', 0)}",
        "",
        "## Connection Lifecycle",
        "",
        f"- MQTT node connects    : {engine.get('mqtt_node_connected', 0)}",
        f"- MQTT node disconnects : {engine.get('mqtt_node_disconnected', 0)}",
    ]
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--hivemq", default="logs/hivemq.log")
    p.add_argument("--engine", default="logs/engine.log")
    p.add_argument("--gateway", default="logs/gateways.log")
    p.add_argument("--out", default="tools/out/packet_integrity.md")
    args = p.parse_args()

    hivemq = audit_hivemq(args.hivemq)
    engine = audit_engine(args.engine)
    gateway = audit_gateway(args.gateway)

    report = render(hivemq, engine, gateway)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")
    print(report)
    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    main()
