# Phase 2 Operator Runbook

Everything you must do **outside the Python/Docker code** to bring the
hybrid MQTT + CoAP campus online. This is a linear, copy-paste friendly
guide: UI click-paths for ThingsBoard, Node-RED and HiveMQ Control
Center, plus every JSON payload / CSV / REST body referenced in the
project.

Legend

| Tag      | Meaning                                             |
| -------- | --------------------------------------------------- |
| [shell]  | Run on the Docker host (PowerShell / bash)          |
| [TB-UI]  | Click in the ThingsBoard browser UI                 |
| [TB-API] | Curl / REST call against the ThingsBoard REST API   |
| [NR-UI]  | Click in the Node-RED browser UI                    |
| [HMQ-UI] | (Optional) Enterprise HiveMQ Control Center only    |

---

## 0. Prerequisites (install on the Docker host)

| Tool                | Why                                        |
| ------------------- | ------------------------------------------ |
| Docker Desktop 4.x+ | Runs the 13-container compose stack        |
| Python >= 3.11      | `bootstrap.py`, `rtt_benchmark`, `audit`   |
| `curl` or Postman   | Importing rule chain / dashboard JSON        |
| WSL or Git-Bash     | Only if you enable TLS (`gen_certs.sh`)    |
| A modern browser    | ThingsBoard, Node-RED; MQTT via WS :8000  |

```powershell
# Verify versions
docker --version
python --version
curl --version
```

---

## 1. Generate credentials (one-shot, idempotent)

Outside the repo you do **nothing** here - the two scripts live in
`infra/certs/` but they must be run before `docker compose up` so the
HiveMQ credentials and CoAP PSK files exist:

```powershell
# [shell]  Deterministic HiveMQ users + roles + ACL + CoAP PSKs
python infra\certs\gen_credentials.py

# [shell]  (optional) TLS PKI - only needed if you flip HiveMQ to 8883
bash infra/certs/gen_certs.sh
```

Expected output:

```
infra/hivemq/conf/credentials.xml       ( 210 users, 20 roles )
infra/certs/coap_psk.json               ( 100 PSKs  )
infra/certs/nodes/*.crt|*.key           ( only when gen_certs.sh ran )
infra/certs/hivemq-keystore.jks         ( only when gen_certs.sh ran )
```

> If you change `CAMPUS_SECRET` in your environment, rerun
> `gen_credentials.py` so the SSHA512 hashes and PSKs stay in sync.

---

## 2. Bring up the stack

```powershell
# [shell]
docker compose up -d --build
docker compose ps    # all 13 services should be "running"
```

Services become reachable after the indicated warm-up:

| Service            | URL                          | Ready after |
| ------------------ | ---------------------------- | ----------- |
| HiveMQ MQTT (TCP)  | `tcp://localhost:1885`       | ~10 s       |
| HiveMQ MQTT (WS)   | `ws://localhost:8000/mqtt` | ~10 s       |
| ThingsBoard UI     | http://localhost:9090        | ~60 s       |
| ThingsBoard MQTT   | `tcp://localhost:1884`       | ~60 s       |
| ThingsBoard RPC    | `tcp://localhost:17070` (maps to container 7070) | ~60 s |
| ThingsBoard CoAP   | `udp://localhost:15683`      | ~60 s       |
| Node-RED f01..f10  | http://localhost:1890..1899  | ~20 s       |
| CoAP (UDP, engine) | `udp://localhost:5684-5783`  | ~20 s       |

Default credentials:

| Console     | Username                      | Password      |
| ----------- | ----------------------------- | ------------- |
| ThingsBoard | `tenant@thingsboard.org`      | `tenant`      |

---

## 3. HiveMQ CE - sanity check (2 min, no web UI)

The image **`hivemq/hivemq-ce`** is **Community Edition**: it does **not**
include **HiveMQ Control Center**, so **`http://localhost:8080` will not
load a broker UI** (nothing listens on 8080 inside the container). The
`infra/hivemq/conf/config.xml` Control Center block is reference only;
the CE entrypoint does not merge the `conf-custom` mount into the live
`/opt/hivemq/conf/config.xml`.

**What works from the host**

| Check | How |
| ----- | --- |
| Broker up | `docker compose logs hivemq` ends with `Started HiveMQ` |
| TCP MQTT | `mosquitto_sub -h localhost -p 1885 -t 'campus/b01/#' -v` (expect traffic) |
| WebSocket | Connect a client to **`ws://localhost:8000/mqtt`** (subprotocol `mqtt`) |
| Client count | `docker compose logs hivemq \| findstr /i client` or use MQTTX on **1885** |

After the **ThingsBoard IoT Gateway** container is up (section 5.3), you
should see an MQTT client subscribed to `campus/b01/#` (often as user
`thingsboard` if you kept the sample HiveMQ credentials) in HiveMQ logs.

If `floor-fNN` clients flap **offline** briefly, that can be the
simulated fault injector (`FAULTS_PROBABILITY=0.005`) in `docker-compose`
— expected.

For a **graphical** broker dashboard you need **HiveMQ Enterprise**
(`hivemq/hivemq4` + license) or an external MQTT monitor pointed at
**localhost:1885**.

---

## 4. Node-RED - verify each floor gateway (2 min / floor)

Host ports `1890 + (floor-1)` map to floor 1..10 (container port is always
1880). Repeat for every floor or at least spot-check f01, f05, f10.

1. [NR-UI] Open `http://localhost:1890` (floor 01).
2. Confirm a single tab **"Floor 01 Gateway"** is present and the bar
   under the `mqtt-broker-config` node reads **"connected"**.
3. Check the palette - if `coap request` nodes show as red boxes, the
   contrib module wasn't installed (see Troubleshooting 10.1).
4. [NR-UI] **Menu -> Manage palette -> Nodes** -> confirm:
   - `node-red-contrib-coap@~0.4.0` installed
   - `node-red-contrib-aedes@~0.13.0` installed
5. [NR-UI] Click the Debug (bug) tab in the sidebar. You should see
   lines like:

   ```
   f01  coap->mqtt    r111  temp=23.1
   f01  edge-thin     summary published n=12 avg=22.8
   ```

6. [NR-UI] Open the `f01-func-autonomy` function node - confirm
   that the `flow.get('thinBucket')` logic and the 60 s `setTimeout`
   are untouched (they're the edge-thinning brain).
7. [NR-UI] **Deploy** -> **Full** only if you changed something. The
   read-only volume `gw_fNN.json` is already deployed on container
   start.

Per-floor port map:

| Floor | Node-RED UI              |
| ----- | ------------------------ |
| f01   | http://localhost:1890    |
| f02   | http://localhost:1891    |
| f03   | http://localhost:1892    |
| f04   | http://localhost:1893    |
| f05   | http://localhost:1894    |
| f06   | http://localhost:1895    |
| f07   | http://localhost:1896    |
| f08   | http://localhost:1897    |
| f09   | http://localhost:1898    |
| f10   | http://localhost:1899    |

---

## 5. ThingsBoard - provision everything

ThingsBoard needs these (in order):

1. Device profiles + assets + devices + access tokens + **gateway bridge
   device** + **thermal profile alarms** -> `bootstrap.py` (automatic)
2. **NOC Dashboard** -> import `dashboard-noc.json` in the TB UI (optional seed)
3. **Custom rule chain** `rule-chain-alarms.json` -> optional (§5.2); CE gets
   **HighTemp** from device-profile alarm rules after `bootstrap.py`
4. **HiveMQ → ThingsBoard path** -> **`tb-gateway` service** (docker compose)
   subscribes campus topics and forwards to TB. **There is no
   "Integrations center" in ThingsBoard CE** — that menu and **MQTT
   Platform Integrations** are **ThingsBoard PE** only. The JSON in
   `integration-hivemq.json` is a **reference** for PE or for manual
   decoder logic; on CE you do **not** import it in the UI.

Run item 1 from the shell; bring up **`tb-gateway`** (compose) for HiveMQ → TB.
Import the dashboard (step 2) in the UI when you want the NOC widgets.

### 5.1 Run bootstrap.py (Python, automatic)

```powershell
# [shell]
python infra\thingsboard\bootstrap.py `
    --url http://localhost:9090 `
    --user tenant@thingsboard.org `
    --password tenant
```

Expected tail (includes one line per thermal profile after profiles are created):

```
device profiles OK: MQTT-ThermalSensor, CoAP-ThermalSensor
thermal profile OK: name='MQTT-ThermalSensor' HighTemp declarative (MAJOR if temperature > 30) + shared highTempThreshold=30
thermal profile OK: name='CoAP-ThermalSensor' HighTemp declarative (MAJOR if temperature > 30) + shared highTempThreshold=30
floor f01: 20 devices provisioned
...
floor f10: 20 devices provisioned
wrote infra\thingsboard\devices.csv (200 devices)
```

CSV schema (machine-readable, one row per device):

```csv
room_id,floor,room_code,device_type,profile,access_token
b01-f01-r101,f01,101,mqtt,MQTT-ThermalSensor,<AccessToken>
b01-f01-r111,f01,111,coap,CoAP-ThermalSensor,<AccessToken>
...
```

### 5.2 Import the Rule Chain (`rule-chain-alarms.json`) — optional on CE

**Default path:** `bootstrap.py` (§5.1) now adds a **declarative** `HighTemp` alarm on
the MQTT and CoAP **device profiles**, evaluated by ThingsBoard’s built-in root rule
flow — **you do not need this section for basic HighTemp + telemetry.**

Use `rule-chain-alarms.json` only if you want the **custom** chain (e.g. JS threshold
from metadata, extra branches). **Avoid “Make root”** unless you intend to replace
the tenant’s entire root rule chain (easy to break RPC / default routing).

#### Option A - UI

1. [TB-UI] Log in to `http://localhost:9090` as tenant admin.
2. [TB-UI] **Rule chains** (left sidebar) -> **+ icon (top right)** ->
   **Import rule chain**.
3. Drag `infra/thingsboard/rule-chain-alarms.json` in -> **Import**.
4. Open the freshly imported chain **"Phase 2 Campus Alarms"**.
5. [TB-UI] **...** menu top-right -> **Make root**.  This routes all
   device messages through the alarm logic.
   - If you prefer to leave the default Root Rule Chain alone, instead
     do **Device Profile -> MQTT-ThermalSensor -> Rule chain = Phase 2
     Campus Alarms** and repeat for `CoAP-ThermalSensor`.

#### Option B - REST

```powershell
# [shell]  Import rule chain via REST (use curl.exe — not curl — on Windows)
$TOKEN = (Invoke-RestMethod -Method Post `
    -Uri http://localhost:9090/api/auth/login `
    -ContentType 'application/json' `
    -Body '{"username":"tenant@thingsboard.org","password":"tenant"}').token

curl.exe -X POST http://localhost:9090/api/ruleChain/import `
    -H "X-Authorization: Bearer $TOKEN" `
    -H "Content-Type: application/json" `
    --data-binary "@infra/thingsboard/rule-chain-alarms.json"
```

### 5.3 Bridge HiveMQ → ThingsBoard CE (`tb-gateway`, not "Integrations")

**ThingsBoard Community Edition does not ship Platform Integrations**
(there is no **Integrations center** menu, and you cannot create
**HiveMQ-MQTT-Integration** in the CE UI). That feature is
[ThingsBoard PE](https://thingsboard.io/products/thingsboard-pe/).

This stack uses **ThingsBoard IoT Gateway** instead: the
`tb-gateway` service in `docker-compose.yml` reads
`infra/thingsboard/tb-gateway/tb_gateway.json` and
`infra/thingsboard/tb-gateway/mqtt.json`, subscribes to HiveMQ on
`campus/b01/+/+/telemetry` (plus heartbeat/status), and forwards
telemetry to the **existing room devices** created by `bootstrap.py`.

1. [shell] After ThingsBoard is healthy, ensure the gateway container is
   up (it is part of the default compose file):

   ```powershell
   docker compose up -d tb-gateway
   docker compose logs tb-gateway --tail 80
   ```

   Look for a successful connection to **ThingsBoard** (MQTT to
   `thingsboard:1883`) and subscription logs for the HiveMQ connector.

2. [shell] `bootstrap.py` (§5.1) must have been run **after** this change
   at least once: it creates device profile **`Campus-Gateway-MQTT`**
   (**DEFAULT** profile, **MQTT** transport — CE has no `GATEWAY`
   profile type) and device **`Campus-HiveMQ-Gateway`** with
   **`additionalInfo.gateway: true`** (ThingsBoard only enables
   **`v1/gateway/*`** MQTT for that flag — without it, HiveMQ traffic is
   converted but **never pushed** to room devices). Access token defaults
   to **`phase2-campus-tb-gateway-token-change-me`** (override with
   **`TB_GATEWAY_ACCESS_TOKEN`** in compose and re-run bootstrap so TB
   credentials match the container). After bootstrap, run
   **`docker compose restart tb-gateway`** if the gateway container was
   already up, so it reconnects with the updated device definition.

3. [TB-UI] **Devices** → open **`Campus-HiveMQ-Gateway`** — state should
   become **active** when `tb-gateway` is connected (telemetry still
   lands on the **room** devices, not on this gateway device).

4. **PE / reference only** — `integration-hivemq.json` documents the
   uplink/downlink JS used in **ThingsBoard PE** MQTT integrations. If
   you run TB PE, you can recreate that integration there; it is **not**
   applicable to the CE image in this repo.

> **Downlink** (RPC → `.../cmd`) is still handled by Node-RED floor
> gateways and HiveMQ; the IoT Gateway config here is **uplink-focused**.

Verify (telemetry should land on the **room** device `b01-f01-r101`, not
on `Campus-HiveMQ-Gateway`):

1. **`tb-gateway` must be running** and subscribed (`docker compose ps`,
   `docker compose logs tb-gateway --tail 50`). If you edited
   `infra/thingsboard/tb-gateway/mqtt.json`, run
   `docker compose restart tb-gateway`.

2. Payloads must include a top-level **`ts`** (Unix time in **milliseconds**).
   **ThingsBoard IoT Gateway 3.7** (`thingsboard/tb-gateway:3.7.0`) ignores
   per-key `tsField` and sets every datapoint time from
   `data.get("ts", data.get("timestamp"))`. ThingsBoard then stores that
   number as **ms**; if you only send **`timestamp`** in **seconds**, the
   UI shows **1970**. The engine emits **`ts`**, **`timestamp`** (seconds),
   and **`tb_ts_ms`** (duplicate ms for scripts).

3. **Point `mosquitto_pub` at HiveMQ, not ThingsBoard.** Campus topics
   (`campus/b01/...`) are accepted by **HiveMQ** (compose maps host **`1885`**
   → broker `1883`). **`tb-gateway`** subscribes there and forwards to TB.
   ThingsBoard’s own MQTT port (**`1884`** by default) expects **device access
   tokens** and `v1/devices/me/telemetry` — it will **not** ingest campus JSON.

| Where the MQTT client runs | `-h` (host) | `-p` (port) |
| ---------------------------- | ----------- | ----------- |
| **Your PC** (native `mosquitto_pub` binary in the shell) | `127.0.0.1` or `localhost` | **`1885`** |
| **`docker run eclipse-mosquitto … mosquitto_pub`** (client runs **inside** a new container) | **`hivemq`** on compose network **`iot-phase2_campus-net`**, **not** `127.0.0.1` | **`1883`** |
| Same `docker run` pattern, but you refuse to attach a network | `host.docker.internal` (Docker Desktop) or the host gateway IP | **`1885`** (published HiveMQ on the host) |
| **Another long-lived service container** on `campus-net` | `hivemq` | **`1883`** |

Inside a throwaway container, **`127.0.0.1` is that container’s loopback**, so
`-h 127.0.0.1 -p 1885` does **not** reach HiveMQ on your machine — you may see
**`Error: Bad file descriptor`** or a connection failure. Join the compose
network and use **`hivemq:1883`**, or stay off the network and use
**`host.docker.internal:1885`** (Docker Desktop; on Linux you may need
`--add-host=host.docker.internal:host-gateway`).

Do **not** use `host.docker.internal` when the MQTT client truly runs **on the
host process** (native `mosquitto_pub`) — from there, `127.0.0.1:1885` is correct.

**Native Mosquitto on the host** (if installed):

```powershell
# [shell]  Host binary — HiveMQ on published port 1885
$ts = [int][double]::Parse((Get-Date -UFormat %s))
$tms = $ts * 1000
$msg = '{"sensor_id":"b01-f01-r101","tb_profile":"MQTT-ThermalSensor","temperature":28.5,"humidity":44,"occupancy":false,"light_level":0,"hvac_mode":"ECO","lighting_dimmer":0,"timestamp":' + $ts + ',"ts":' + $tms + ',"tb_ts_ms":' + $tms + '}'
mosquitto_pub -h 127.0.0.1 -p 1885 -u thingsboard -P changeit -t "campus/b01/f01/r101/telemetry" -m $msg
```

**`docker run` Mosquitto client** (typical on Windows when you do not have
`mosquitto_pub` installed). Compose project name `iot-phase2` → network is
usually `iot-phase2_campus-net` (`docker network ls` to confirm):

```powershell
# [shell]  Client inside container — campus-net + hivemq:1883
$ts = [int][double]::Parse((Get-Date -UFormat %s))
$tms = $ts * 1000
$msg = '{"sensor_id":"b01-f01-r101","tb_profile":"MQTT-ThermalSensor","temperature":28.5,"humidity":44,"occupancy":false,"light_level":0,"hvac_mode":"ECO","lighting_dimmer":0,"timestamp":' + $ts + ',"ts":' + $tms + ',"tb_ts_ms":' + $tms + '}'
docker run --rm --network iot-phase2_campus-net eclipse-mosquitto:2 mosquitto_pub `
  -h hivemq -p 1883 -u thingsboard -P changeit -q 1 `
  -t "campus/b01/f01/r101/telemetry" `
  -m $msg
```

Then **Devices → b01-f01-r101 → Latest telemetry** should show
`temperature = 28.5` within a few seconds. In the UI, set the device
time window to **Last 5 minutes** or **Real-time** if you do not see
new points immediately.

### 5.4 Import the NOC Dashboard (`dashboard-noc.json`)

`dashboard-noc.json` is a **seed** dashboard with two widgets:
**Fleet Temperature (All Rooms)** and **Active Alarms**. Extend it in
edit mode once it loads (floor summary, RTT histogram, Toggle-Heat
RPC) — those widgets depend on assets/device profiles created by
`bootstrap.py`, so hand-wiring them in the UI is simpler than shipping
raw JSON for every bundle alias.

1. [TB-UI] **Dashboards** -> **+** -> **Import dashboard**.
2. Drag `infra/thingsboard/dashboard-noc.json`.
3. When prompted, accept the single **All Rooms** entity alias
   (resolves by device type).
4. Open the dashboard, drag the timewindow to **Real-time, 1 minute**.
   The seed **All Rooms** alias matches device **names starting with `b01-`**
   (campus rooms only). If you imported an older `dashboard-noc.json` that
   resolved **every** device in the tenant, the fleet chart will look empty
   or wrong — re-import this file or change the alias to **Entity name**
   `b01-` under **Entity aliases**.
5. Expect within 30 s:
   - **Fleet Temperature (All Rooms)** draws at least one line per
     telemetry-producing device.
   - **Active Alarms** is empty until a HighTemp / DeviceOffline alarm
     is raised by the rule chain.

### 5.5 Set the shared `highTempThreshold` attribute

**`bootstrap.py` already posts** `highTempThreshold = 30` on the **MQTT-ThermalSensor**
and **CoAP-ThermalSensor** device profiles and installs a **declarative** `HighTemp`
alarm (temperature > 30 °C) evaluated by ThingsBoard’s **root** rule chain — **re-run
bootstrap** after upgrading the repo so existing tenants pick this up.

The optional `rule-chain-alarms.json` flow (JS filter on `shared_highTempThreshold`) is
only needed if you import that chain and assign it as the profile default; CE works
without it when using profile alarm rules.

For manual overrides, the **device profile** shared attribute still applies to
documentation and to any custom rule logic you add later. Default to 30 °C; override
per-room for demos.

```powershell
# [shell]  Fleet default via REST: shared attribute on the MQTT-ThermalSensor
# device profile (applies to all devices on that profile).
#
# PowerShell: do NOT use `curl` for this — it is an alias for Invoke-WebRequest
# and does not accept `-H` / `--data` like Linux curl. Use Invoke-RestMethod
# or call real curl as `curl.exe`.

$base = "http://localhost:9090"
$login = Invoke-RestMethod -Method Post -Uri "$base/api/auth/login" `
    -ContentType "application/json" `
    -Body '{"username":"tenant@thingsboard.org","password":"tenant"}'
$TOKEN = $login.token
$headers = @{
    "X-Authorization" = "Bearer $TOKEN"
    "Content-Type"    = "application/json"
}

$profiles = Invoke-RestMethod -Uri "$base/api/deviceProfiles?pageSize=100&page=0" -Headers $headers
$mqttProfileId = ($profiles.data | Where-Object { $_.name -eq "MQTT-ThermalSensor" }).id.id

Invoke-RestMethod -Method Post `
    -Uri "$base/api/plugins/telemetry/DEVICE_PROFILE/$mqttProfileId/SHARED_SCOPE" `
    -Headers $headers `
    -Body '{"highTempThreshold":30.0}'

# Equivalent using Windows curl binary (note the `.exe`):
# curl.exe -X POST "$base/api/plugins/telemetry/DEVICE_PROFILE/$mqttProfileId/SHARED_SCOPE" `
#   -H "X-Authorization: Bearer $TOKEN" -H "Content-Type: application/json" `
#   -d "{\"highTempThreshold\":30.0}"
```

Or, per room, via UI:

1. [TB-UI] **Devices -> b01-f01-r101 -> Attributes -> Shared attributes
   -> +**.
2. Key = `highTempThreshold` (Double), Value = `25.0`.
3. Save.

To demo a HighTemp alarm on your chosen room:

```powershell
# [shell]  fake a 40 C reading (include `ts` in ms for tb-gateway 3.7)
$ts = [int][double]::Parse((Get-Date -UFormat %s))
$tms = $ts * 1000
$msg = '{"sensor_id":"b01-f01-r101","tb_profile":"MQTT-ThermalSensor","temperature":40.0,"humidity":44,"occupancy":false,"light_level":0,"hvac_mode":"ECO","lighting_dimmer":0,"timestamp":' + $ts + ',"ts":' + $tms + ',"tb_ts_ms":' + $tms + '}'
docker run --rm --network iot-phase2_campus-net eclipse-mosquitto:2 mosquitto_pub `
  -h hivemq -p 1883 -u thingsboard -P changeit -q 1 `
  -t "campus/b01/f01/r101/telemetry" `
  -m $msg
```

The `Active Alarms` widget shows `HighTemp | MAJOR` within 2 s.

### 5.6 Test a QoS 2 command through the RPC widget

1. [TB-UI] Open the dashboard, click any tile in `Fleet Health`.
2. The **Toggle Heat** RPC button widget now points at that device.
3. Click it. Expected timeline (audit in `docker compose logs hivemq`):

   ```
   PUBLISH QoS2 campus/b01/f01/r101/cmd
   PUBREC 4012 / PUBREL 4012 / PUBCOMP 4012
   PUBLISH     campus/b01/f01/r101/response   { status:"ok" ... }
   ```

---

## 6. Ready-to-paste JSON payloads

Drop these into Postman / curl when you need to script anything outside
`bootstrap.py`.

### 6.1 Log in to ThingsBoard

```http
POST /api/auth/login HTTP/1.1
Host: localhost:9090
Content-Type: application/json

{ "username": "tenant@thingsboard.org", "password": "tenant" }
```

### 6.2 Create a shared attribute on a device

```http
POST /api/plugins/telemetry/DEVICE/{deviceId}/SHARED_SCOPE HTTP/1.1
X-Authorization: Bearer {token}
Content-Type: application/json

{ "highTempThreshold": 28.0 }
```

### 6.3 Push a server-side attribute (floor tag for the rule chain)

```http
POST /api/plugins/telemetry/DEVICE/{deviceId}/SERVER_SCOPE HTTP/1.1
X-Authorization: Bearer {token}
Content-Type: application/json

{ "floor": "f01", "transport": "mqtt" }
```

### 6.4 One-shot RPC (command toward a device)

```http
POST /api/plugins/rpc/oneway/{deviceId} HTTP/1.1
X-Authorization: Bearer {token}
Content-Type: application/json

{
  "method": "setHvacMode",
  "params": { "hvac_mode": "ON" },
  "timeout": 5000,
  "persistent": false
}
```

On **ThingsBoard PE**, the MQTT integration’s downlink converter would
rewrite that RPC to HiveMQ. On **CE**, Node-RED floor gateways subscribe
to the same `.../cmd` topics; expect traffic shaped like:

```
TOPIC : campus/b01/f01/r101/cmd
QOS   : 2
BODY  : { "method":"setHvacMode","params":{"hvac_mode":"ON"} }
```

### 6.5 Fetch the last 20 telemetry points for a device

```http
GET /api/plugins/telemetry/DEVICE/{deviceId}/values/timeseries?keys=temperature,humidity&limit=20
X-Authorization: Bearer {token}
```

### 6.6 Create an alarm type (used by the rule chain)

Already created implicitly by `TbCreateAlarmNode`, but if you want to
pre-seed the Alarms table:

```http
POST /api/alarm HTTP/1.1
X-Authorization: Bearer {token}
Content-Type: application/json

{
  "originator": { "id": "{deviceId}", "entityType": "DEVICE" },
  "type": "HighTemp",
  "severity": "MAJOR",
  "status": "ACTIVE_UNACK",
  "propagate": true,
  "details": { "temperature": 42.0 }
}
```

### 6.7 Node-RED admin login (only when `adminAuth` is set in settings.js)

```http
POST /auth/token HTTP/1.1
Host: localhost:1890
Content-Type: application/x-www-form-urlencoded

client_id=node-red-admin&grant_type=password&scope=*&username=admin&password=changeit
```

### 6.8 HiveMQ REST (CC API) - list currently-connected clients

```http
GET /api/v1/mqtt/clients?limit=200 HTTP/1.1
Host: localhost:8888
Authorization: Basic YWRtaW46aGl2ZW1xYWRtaW4=   ; admin:hivemqadmin
```

Only available with the HiveMQ Enterprise REST API extension. On CE,
read `docker compose logs hivemq` instead.

---

## 7. Benchmarks & audits

Run from the host, once the stack has been up for at least 2 minutes
(warm-up):

```powershell
# [shell]  100-room round-trip latency (QoS 2 command -> telemetry)
python tools\rtt_benchmark.py --hivemq-host localhost --hivemq-port 1885 --count 100

# [shell]  QoS 2 handshake + CoAP CON balance audit
docker compose logs hivemq > logs\hivemq.log
docker compose logs engine > logs\engine.log
python tools\audit_logs.py --hivemq logs\hivemq.log --engine logs\engine.log
```

Expected RTT: mean ~110 ms, p95 < 300 ms, target gate < 500 ms.

---

## 8. Teardown / reset

```powershell
# [shell]  stop + keep data
docker compose stop

# [shell]  full wipe (drops TB DB, HiveMQ retained store, gateway state)
docker compose down -v
```

---

## 9. Order-of-operations cheat-sheet

```
[shell]  gen_credentials.py
[shell]  (optional) gen_certs.sh
[shell]  docker compose up -d --build
[HMQ]  verify broker + MQTT traffic (section 3; CE has no web UI)
[NR-UI]  spot-check gateway-f01, f05, f10           (section 4)
[shell]  python bootstrap.py                        (5.1)
[TB-UI]  import rule-chain-alarms.json -> make root (5.2)
[shell]  docker compose up -d tb-gateway              (5.3)
[TB-UI]  import dashboard-noc.json                  (5.4)
[TB-UI]  set highTempThreshold shared attribute     (5.5)
[TB-UI]  click "Toggle Heat" -> observe response    (5.6)
[shell]  rtt_benchmark.py + audit_logs.py           (7)
```

---

## 10. Troubleshooting

### 10.1 Node-RED palette shows red `coap request` nodes

The `node-red-contrib-coap` module did not install inside the
container. Either rebuild, or install at runtime:

```powershell
# [shell]
docker compose exec gateway-f01 npm install --prefix /data node-red-contrib-coap
docker compose restart gateway-f01
```

### 10.2 "Integrations center" / HiveMQ integration missing in the UI

That is **expected on ThingsBoard CE**. **Platform Integrations**
(including external MQTT broker integrations such as
`HiveMQ-MQTT-Integration`) are a **ThingsBoard PE** feature, not
present in `thingsboard/tb-postgres` Community Edition.

This project bridges HiveMQ using the **`tb-gateway`** container
(ThingsBoard **IoT Gateway**) plus `infra/thingsboard/tb-gateway/*.json`
and the **`Campus-HiveMQ-Gateway`** device from `bootstrap.py`. You do
not look for an integration in the TB UI on CE.

If you must use the PE **MQTT integration** UI instead, switch the TB
image to a **PE** edition and follow `integration-hivemq.json` as a
template.

### 10.3 Broker / `floor-fXX` users but nothing publishes

`credentials.xml` was regenerated with a new `CAMPUS_SECRET` but the
engine still holds the old cache in `engine_data`. Fix:

```powershell
docker compose stop engine
docker volume rm iot-phase2_engine_data
docker compose up -d engine
```

### 10.4 RTT benchmark times out for CoAP rooms

UDP NAT is not reaching container ports. Confirm with:

```powershell
Test-NetConnection -ComputerName localhost -Port 5684 -InformationLevel Detailed
```

On Windows Docker Desktop, make sure **"Expose daemon on tcp://..."**
is off and WSL2 backend is selected.

### 10.5 Alarms not firing

1. Re-run **`bootstrap.py`** so **device-profile** alarm rules exist (§5.5);
   optional JS rule chain (§5.2) is not required for basic **HighTemp**.
2. For **custom JS rule chains** (§5.2 only): `highTempThreshold` / metadata
   issues can block the filter. Declarative profile alarms from
   **`bootstrap.py`** use a fixed **> 30 °C** rule and do not depend on that.
3. **tb-gateway** logs show converted payloads but TB still has no
   points — confirm **device names** match `bootstrap.py` / `devices.csv`
   (`b\\d{2}-f\\d{2}-r\\d+`) and MQTT payloads include **`sensor_id`**
   and **`tb_profile`** (the engine adds both).

### 10.6 `mosquitto_pub` connects but ThingsBoard never shows telemetry

1. **Wrong broker** — campus traffic must go to **HiveMQ** on the **host**
   port **`1885`** (or `hivemq:1883` from another container on
   `campus-net`). ThingsBoard’s MQTT (**`1884`**) is only for **native TB
   device** sessions (`v1/devices/me/telemetry` + access token), not for
   `campus/b01/...` JSON.
2. **Wrong host for where the client runs** — native `mosquitto_pub` on the
   **host** should use **`127.0.0.1:1885`**, not `host.docker.internal`. If you
   use **`docker run … mosquitto_pub`**, the client is **inside** a container:
   **`127.0.0.1` is wrong** (see §5.3 — use **`hivemq:1883`** on
   `iot-phase2_campus-net` or **`host.docker.internal:1885`**).
3. **`tb-gateway` down** — without it, HiveMQ never forwards to TB
   (`docker compose ps tb-gateway`, then `logs tb-gateway`).

### 10.7 `Bind for 0.0.0.0:1883 failed` (or 9090 / 7070)

Phase 2 **used to** publish HiveMQ on host **1883**; that collides with
many other stacks (e.g. **`mytb`** with `0.0.0.0:1883->1883/tcp`) or
host Mosquitto. The compose file now maps **host `1885` → broker `1883`**
so `docker compose up` succeeds without stopping those services. In-stack
traffic still uses **`hivemq:1883`** inside `campus-net`.

If you change the published port again, point host tools at the new host
port (`mosquitto_*`, `rtt_benchmark.py`, MQTTX).

For **9090** or **17070** conflicts, list listeners and stop the other
stack or remap in `docker-compose.yml`:

```powershell
docker ps --format "table {{.Names}}\t{{.Ports}}"
```

Clean up **Created** but never-started Phase 2 containers after a failed
`up`:

```powershell
docker compose down
docker stop mytb   # if it holds 9090/7070 (or 1883 if you remapped back)
docker compose up -d --build
```

### 10.8 ThingsBoard “does not start” or UI never opens

The `thingsboard/tb-postgres` image runs **embedded PostgreSQL**, then the
JVM. From container start to a working UI is often **60–120 seconds** on
first boot or after a laptop sleep — not a hung container.

1. **Check logs** for a successful finish line:

   ```powershell
   docker compose logs thingsboard --tail 40
   ```

   You want: `Started ThingsBoard in … seconds`. If you only see Postgres
   recovery (`database system was not properly shut down`), wait; if it
   repeats in a loop, do a graceful stop and retry:

   ```powershell
   docker compose stop thingsboard
   Start-Sleep -Seconds 15
   docker compose up -d thingsboard
   ```

2. **Port 9090 already in use** (very common if **`mytb`** or another TB
   stack is running). Either stop the other stack or remap **without**
   editing YAML (PowerShell):

   ```powershell
   $env:TB_HTTP_PORT = "19090"
   $env:TB_MQTT_PORT = "1886"
   $env:TB_RPC_PORT = "17071"
   docker compose up -d thingsboard
   ```

   Then open `http://localhost:19090` and use **1886** for TB device MQTT
   from the host. `bootstrap.py` and curl examples must use the same HTTP
   port you chose.

3. **`docker compose ps` shows `unhealthy`**: wait through
   **`start_period`** (2 min). If it stays unhealthy after that, read
   logs for `OutOfMemoryError` — give Docker Desktop **at least ~4 GB**
   RAM in **Settings → Resources**.

4. **Corrupt embedded DB** (rare; usually after force-kill or disk full):
   full reset **wipes the tenant** — only if you accept data loss:

   ```powershell
   docker compose stop thingsboard
   docker volume rm iot-phase2_tb_data
   docker compose up -d thingsboard
   ```

### 10.8 Dashboard imports but stays on the loading spinner

The imported dashboard renders **nothing** and the spinner keeps going.
Always caused by one of three structural issues in the dashboard JSON:

1. **No `states` / `layouts`** — TB needs at least a `default` state
   with a `layouts.main.widgets` map that references every widget by
   its UUID. Without a layout, widgets exist but aren't placed on the
   grid.
2. **Widgets missing `bundleAlias` + `typeAlias`** — these pick the
   actual widget implementation (e.g. `charts` + `basic_timeseries`,
   `alarm_widgets` + `alarms_table`). Custom `type` strings alone are
   ignored.
3. **Entity aliases keyed by a label instead of a UUID** — widgets
   look them up by UUID via `entityAliasId`, so the map key in
   `entityAliases` must match that UUID.

Fix path: **export** a working dashboard from the UI (`Dashboards →
open → menu → Export`) and compare against the failing file, or edit
the failing dashboard in the UI and re-export. The repo ships
`infra/thingsboard/dashboard-noc.json` as a minimal seed with correct
`states/layouts/bundleAlias`; extend it in edit mode rather than
hand-writing JSON for every widget.

If the dashboard opens but shows **“Failed to parse the payload”**
(WebSocket `errorCode: 2`), typical causes are: **`settings.stateControllerId`
set to `entity`** without a proper dashboard state / entity selection
(use `default` unless you built states for it), **invalid alarm filters**
(the alarms table uses `alarmFilterConfig.statusList` with search
statuses like `ACTIVE`, not raw `ACTIVE_UNACK` strings at the wrong
level), or **dashboard / widget timewindow** objects that omit required
fields—compare your JSON to a UI export.

### 10.9 Latest telemetry empty (engine running, HiveMQ has traffic)

On **CE**, telemetry reaches room devices through **`tb-gateway`**
(IoT Gateway), not through a Platform MQTT integration.

Checklist:

1. **`docker compose ps`** — `tb-gateway` must be **Up** and
   `thingsboard` **healthy**. `docker compose logs tb-gateway --tail 100`
   should show the MQTT connector connected to **HiveMQ** and no
   repeating auth/config errors. Default TB-side token is
   `phase2-campus-tb-gateway-token-change-me` unless you overrode
   **`TB_GATEWAY_ACCESS_TOKEN`** (must match `bootstrap.py` / device
   credentials in TB).
2. **`docker compose ps`** — `engine` must be **Up** (it publishes
   `campus/b01/+/+/telemetry`). `docker compose logs engine --tail 30`
   should show tick / fault lines, not MQTT auth failures.
3. **Payload shape** — room JSON must include **`sensor_id`** (room id)
   and **`tb_profile`** (`MQTT-ThermalSensor` or `CoAP-ThermalSensor`);
   the gateway `mqtt.json` mapping uses them for device routing. Re-run
   a current **engine** image if you still see old payloads without
   `tb_profile`.
4. **Gateway device not in MQTT “gateway” mode** — in TB logs / gateway
   stats you may see converters working but **`platformTsProduced`**
   stays **0**. Ensure **`Campus-HiveMQ-Gateway`** has
   **`additionalInfo.gateway: true`** (bootstrap sets it; in the UI use
   the device **Gateway** checkbox if you created the device by hand),
   then **`docker compose restart tb-gateway`**.
5. **Isolation test** — publish one message with the §5.3 verify
   command; if **b01-f01-r101** still shows no telemetry, inspect
   `tb-gateway` logs and TB device **Campus-HiveMQ-Gateway** state before
   debugging dashboards.
6. **Latest telemetry dates stuck in 1970** — ThingsBoard stores `ts` in
   **milliseconds**. **tb-gateway 3.7** reads **`ts`** else **`timestamp`**
   from the JSON and forwards it as the event time; **seconds-only**
   **`timestamp`** is misread as ms (1970). The engine adds **`ts`** (ms).
   After changing `mqtt.json`, **`docker compose restart tb-gateway`**.
7. **Most devices “Inactive” in ThingsBoard** — TB marks a device inactive
   when it has **not received telemetry** within the profile’s **inactivity
   timeout** (often several minutes). **MQTT rooms** (`rXX01–rXX10`) need
   **engine + tb-gateway**. **CoAP rooms** (`rXX11–rXX20`) also need the
   **floor Node-RED gateway** running so CoAP observe is republished to
   HiveMQ. Check `docker compose ps` for **`engine`**, **`tb-gateway`**, and
   **`gateway-f01`…`gateway-f10`**. If the simulator had run for a long time
   with an older engine build, reset persisted state:
   `docker compose stop engine` then remove volume **`iot-phase2_engine_data`**
   (or delete `world.db`) before `docker compose up -d engine`.

---

## 11. Template files recap (already in the repo)

| File                                                 | Purpose                                         |
| ---------------------------------------------------- | ----------------------------------------------- |
| `infra/thingsboard/rule-chain-alarms.json`           | HighTemp + DeviceOffline rule chain             |
| `infra/thingsboard/dashboard-noc.json`               | 200-room NOC dashboard                          |
| `infra/thingsboard/integration-hivemq.json`          | **PE reference** — MQTT integration uplink/downlink JS |
| `infra/thingsboard/tb-gateway/tb_gateway.json`       | IoT Gateway main config (TB host + connector list) |
| `infra/thingsboard/tb-gateway/mqtt.json`             | HiveMQ broker + topic → telemetry mapping (CE path) |
| `infra/thingsboard/bootstrap.py`                     | Profiles + assets + devices + tokens CSV        |
| `infra/thingsboard/devices.csv` (generated)          | 200 access tokens for the engine                |
| `infra/hivemq/conf/config.xml`                       | Listeners, QoS limits, CC admin                 |
| `infra/hivemq/conf/credentials.xml`                  | 210 users, 20 roles, ACLs per floor             |
| `infra/certs/coap_psk.json`                          | Per-room CoAP DTLS PSKs                         |
| `infra/certs/gen_credentials.py`                     | Deterministic generator for the two files above |
| `infra/certs/gen_certs.sh`                           | TLS PKI (only if 8883 used)                     |
| `gateways/flows/gw_f01.json` .. `gw_f10.json`        | 10 Node-RED floor gateway flows                 |
| `gateways/gen_flows.py`                              | Regenerates the 10 flows above                  |
| `gateways/package.json`                              | Pins `node-red-contrib-coap` and `-aedes`       |
| `gateways/settings.js`                               | Node-RED runtime settings (disabled UI auth)    |
