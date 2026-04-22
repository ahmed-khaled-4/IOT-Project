# SWAPD453 - Phase 2: Hybrid MQTT + CoAP Campus

Phase 2 evolves the Phase 1 `World Engine` (200-room asyncio simulator) into
a distributed hybrid IoT network with:

- **100 MQTT Nodes** (`gmqtt`, persistent TCP to HiveMQ, each with its own
  ClientID and Last Will & Testament)
- **100 CoAP Nodes** (`aiocoap`, Observable Resources per RFC 7641)
- **HiveMQ CE** as the central "Campus Hub" MQTT backbone
- **10 Node-RED Floor Gateways** performing bi-directional MQTT <-> CoAP
  translation, 60-second edge thinning, DUP-flag deduplication, and
  offline autonomy
- **ThingsBoard CE** as the on-prem cloud platform with asset hierarchy,
  rule engine, alarms, and the NOC dashboard
- **TLS + DTLS** identity for every node, **HiveMQ ACLs** restricting each
  node to its floor's topic subtree, **QoS 2** commands and **CoAP CON**
  Sentinel alerts.

---

## Architecture

```
             +-----------------------------+
             |       ThingsBoard CE        |  (9090 UI; host 1884 -> TB MQTT)
             +--------------^--------------+
                            |
              ThingsBoard IoT Gateway (tb-gateway; subscribes HiveMQ)
                            |
+---------------+    +------+------+    +---------------+
| HiveMQ CE     |<---+ Floor       |    | HiveMQ CE     |
| host 1885 / WS|    | Gateways    +--->| (same broker) |
+-------+-------+    | (Node-RED)  |    +-------+-------+
        ^            | f01..f10    |            ^
        |            +------+------+            |
        |                   |                  |
   100 persistent           | CoAP Observe   |
   TCP MQTT                 v   / PUT CON    |
   clients           +-----------------+      |
   (gmqtt)           | World Engine    |------+
                     | asyncio         |
                     | 100 MQTT + 100  |
                     | CoAP servers    |
                     +-----------------+
```

Floor layout (20 rooms per floor x 10 floors):

| Rooms       | Transport | Topic                                        |
| ----------- | --------- | -------------------------------------------- |
| r{f}01..r{f}10 | MQTT      | `campus/b01/f{f}/r{...}/...`                  |
| r{f}11..r{f}20 | CoAP      | `coap://engine:{5684+idx}/f{f}/r{...}/...`    |

---

## Run

### 1. Bring up the campus stack (HiveMQ + ThingsBoard + 10 gateways + engine)

```powershell
docker compose up --build
```

Wait ~60 s for ThingsBoard to initialize. The following services are then
reachable from the host:

| Service                   | URL / endpoint                         |
| ------------------------- | -------------------------------------- |
| HiveMQ MQTT (TCP)         | `mqtt://localhost:1885`                |
| HiveMQ MQTT (WebSocket)   | `ws://localhost:8000/mqtt`             |
| ThingsBoard UI            | http://localhost:9090                 |
| Node-RED F01..F10 editors | http://localhost:1890..1899 (container port 1880) |

**HiveMQ Community Edition** does not ship **Control Center** (no browser UI on 8080). Use an MQTT client (e.g. [MQTTX](https://mqttx.app/), `mosquitto_sub`), WebSocket on port **8000**, or `docker compose logs hivemq`.

Default credentials:

- ThingsBoard:
  - System admin: `sysadmin@thingsboard.org` / `sysadmin`
  - Tenant admin: `tenant@thingsboard.org` / `tenant`

### 2. Provision ThingsBoard (200 devices, asset hierarchy, rule chain, dashboard)

```powershell
python infra\thingsboard\bootstrap.py `
  --url http://localhost:9090 `
  --user tenant@thingsboard.org `
  --password tenant
```

Produces `infra\thingsboard\devices.csv` with access tokens.

### 3. Generate security artifacts

```powershell
# HiveMQ ACL roles/users + CoAP PSKs (deterministic, idempotent)
python infra\certs\gen_credentials.py

# TLS PKI (requires openssl + keytool; WSL/bash recommended)
bash infra/certs/gen_certs.sh
```

### 4. Sanity-check the engine locally (optional)

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Fast smoke test - 20 rooms, 15 seconds, against HiveMQ
$env:MQTT_HOST="localhost"
$env:MQTT_PORT="1885"
python -m engine.main --rooms 20 --duration-sec 15
```

### 5. Benchmarks & audits

```powershell
python tools\rtt_benchmark.py --hivemq-host localhost --hivemq-port 1885 --count 100
python tools\audit_logs.py --hivemq logs\hivemq.log --engine logs\engine.log
```

---

## Topic / URI Scheme

MQTT (HiveMQ):

| Purpose       | Topic                                       |
| ------------- | ------------------------------------------- |
| Telemetry     | `campus/b01/f{f}/r{rc}/telemetry`           |
| Heartbeat     | `campus/b01/f{f}/r{rc}/heartbeat`           |
| Command       | `campus/b01/f{f}/r{rc}/cmd` (QoS 2)         |
| Response      | `campus/b01/f{f}/r{rc}/response`            |
| Status / LWT  | `campus/b01/f{f}/r{rc}/status` (retained)   |
| Floor summary | `campus/b01/f{f}/summary` (retained, 60 s)  |
| Fleet health  | `campus/b01/fleet/health`                   |
| Fleet command | `campus/b01/fleet/cmd`                      |

CoAP:

| Purpose       | URI                                                  |
| ------------- | ---------------------------------------------------- |
| Telemetry     | `coap://engine:{port}/f{f}/r{rc}/telemetry` (Observe)|
| Heartbeat     | `coap://engine:{port}/f{f}/r{rc}/heartbeat`           |
| HVAC actuator | `coap://engine:{port}/f{f}/r{rc}/actuators/hvac` (PUT CON)|
| Sentinel alert| `coap://gateway-fNN:5683/alerts/{room_id}` (POST CON)|

---

## Repository layout

```
engine/                  # asyncio World Engine (hybrid)
  transport/             # base, mqtt_transport, coap_transport, dup_filter
  security.py            # TLS / PSK loader
  topics.py              # centralised topic builder
  ...
gateways/
  flows/gw_fNN.json      # 10 Node-RED floor gateway flows (generated)
  gen_flows.py           # flow generator (re-run to update)
  settings.js, package.json
infra/
  hivemq/conf/           # HiveMQ config.xml + credentials.xml (ACLs)
  thingsboard/           # bootstrap.py, rule-chain-alarms.json,
                         # dashboard-noc.json, integration-hivemq.json (PE ref),
                         # tb-gateway/*.json (CE HiveMQ bridge)
  certs/                 # gen_certs.sh, gen_credentials.py, coap_psk.json
tools/
  rtt_benchmark.py       # Round-trip latency benchmark
  audit_logs.py          # QoS 2 / CON integrity audit
docker-compose.yml       # 13-container stack (hivemq, tb, tb-gateway, 10 gw, engine)
config/config.yaml       # engine configuration
REPORT_PHASE2.md         # 5-page performance & reliability report
```

---

## Further reading

- Phase 2 Performance & Reliability Report: `REPORT_PHASE2.md`
- RFC 7641 CoAP Observe: https://datatracker.ietf.org/doc/html/rfc7641
- HiveMQ ACLs: https://www.hivemq.com/docs/hivemq/4.30/user-guide/security.html
- ThingsBoard IoT Gateway: https://thingsboard.io/docs/iot-gateway/
- ThingsBoard PE integrations (not in CE): https://thingsboard.io/docs/integrations/
