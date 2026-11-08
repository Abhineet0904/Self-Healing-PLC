# Self Healing PLC

A deliberately small **synthetic** electrical-feeder OT training lab. It contains a real Modbus TCP server implemented in Python, a single-machine-equivalent process model, a dashboard, an event/anomaly page, and an optional autonomous process-level recovery watchdog. It is not a power-grid digital twin and must never be connected to real equipment.

## What's deployed vs. what's local-only

Only `app.py`, `Dockerfile`, `requirements.txt`, and `templates/` are needed for (and run as part of) the deployed service — that's the simulated OT system itself. `scripts/modbus_write_coil.py` and `scripts/start-pinggy-tcp.sh` are attacker/operator tooling meant to run on a separate machine you control during a live demo (e.g. your local Docker host or a Kali VM). They are never imported or executed by `app.py`; `.dockerignore` keeps them out of the built image as a matter of hygiene, not because they'd otherwise be dangerous.

## Process model

Frequency and voltage are produced by a single-machine-equivalent model: the classical generator swing equation, a first-order governor droop response, and a first-order AVR-style voltage lag — the standard simplified representation used in introductory power-system dynamics and several published OT security testbeds. It is **not** a multi-bus AC power-flow solver (no Newton-Raphson/Gauss-Seidel, no network topology, no reactive power dispatch); treat it as a plausible, physically-motivated single-feeder model, not a grid digital twin.

- **Breaker closed**: the feeder is treated as tied to an infinite bus (a large bulk grid); frequency/voltage track nominal with small realistic noise.
- **Breaker opens (attack)**: the feeder islands onto limited local backup generation. Frequency and voltage evolve per the swing equation and governor droop, and settle in a degraded-but-stable state below nominal — a real consequence of droop control, not an arbitrary number.
- **Reclosing is sync-checked**, like a real ANSI/IEEE device-25 sync-check relay: a Modbus close command (or the dashboard) is only honored when frequency and voltage are within tolerance of nominal. Otherwise the server returns a genuine Modbus exception response and logs the rejection.
- **Self-healing disabled**: droop control alone stabilizes the island but — correctly, per real primary-control behavior — never reaches exact nominal, so the breaker stays permanently blocked from reclosing. The dashboard's "Reset process" button models a manual operator-forced restart that bypasses the sync-check, standing in for "an operator found the issue and manually restarted it."
- **Self-healing enabled**: a validated backup-generation source comes online and a secondary (AGC-equivalent) correction actively drives the system back to exact nominal — standing in for the paper's peer-PLC takeover and validated logic restoration — then the sync-check passes and the breaker recloses autonomously. There's a built-in ~5 second minimum dwell time in the islanded state before an autonomous reclose, so a live demo actually shows a visible detect → recover → reintegrate sequence rather than an instant snap-back.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open `http://127.0.0.1:10000`. The Modbus TCP server listens on port `5020` by default. Use only a client on your own lab host/network.

## Modbus map

- Coil 0: simulated feeder breaker closed (1=closed, 0=open).
- Holding register 0: frequency in hundredths of Hz (5000 means 50.00 Hz).
- Holding register 1: voltage in hundredths per unit (100 means 1.00 pu).
- Holding register 2: synthetic load percentage.
- Holding register 3: synthetic generation percentage.

A write to coil 0 is logged. With self-healing disabled, opening the breaker leaves the simulated process disrupted. With self-healing enabled, the watchdog detects the abnormal state and restores the breaker and nominal frequency/voltage after a short delay. This is process-level recovery; it does not prove detection of PLC firmware/program compromise.

## Render

Create a new Render Web Service from this repository using the Dockerfile or `render.yaml`. The dashboard uses HTTP(S). **Render's normal web-service URL does not expose the Modbus TCP port 5020 publicly** — that part of the lab only works against `127.0.0.1` on a machine you're running Docker on yourself, never against the Render URL directly.

### Attacking the deployed Render URL: plain HTTPS, no tunnel

`app.py` exposes a training endpoint, `POST /api/ot/write-coil`, that performs the exact same coil-0 write (and goes through the exact same sync-check logic) as a real Modbus function-5 write — but as a plain JSON POST over the same public HTTPS port Render already forwards. No tunnel, no WebSocket, no extra dependency:

```bash
curl -X POST "https://YOUR-SERVICE.onrender.com/api/ot/write-coil" \
  -H "Content-Type: application/json" \
  -d '{"address":0,"value":false}'
```
`"value":false` opens the breaker (the attack). `"value":true` attempts to reclose it — this is rejected with HTTP 409 if frequency/voltage aren't yet back within the sync-check tolerance, exactly like a real Modbus close command would be.

This endpoint is **unauthenticated by default** — intentionally, to match the proven working setup this is built on (see `selfhealingplc-v0`). That's fine for a supervised classroom demo on a URL you're not advertising, but it does mean anyone who finds the URL can flip your breaker. The curl example and any raw event list are deliberately **not** shown on the public dashboard (`/electricgrid`) itself — only in this README and on `/electricgrid/logs`, which nothing links to from outside. If you want a lightweight gate for your presentation, add a shared-secret check:

```python
import os, hmac
ATTACK_TOKEN = os.getenv('ATTACK_TOKEN')  # set this in Render's Environment tab

@app.post('/api/ot/write-coil')
def external_coil_write():
    if ATTACK_TOKEN and not hmac.compare_digest(request.headers.get('X-Attack-Token', ''), ATTACK_TOKEN):
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 401
    ...  # rest unchanged
```
and send `-H "X-Attack-Token: YOUR_TOKEN"` with the curl command. Left out by default so the command in your message works as-is.

I tested this endpoint directly (Flask's test client, in-process: attack → islanding → sync-check-rejected reclose → manual forced reset → attack again with self-healing on → autonomous recovery) and it behaves exactly as described above.

The old `scripts/start-pinggy-tcp.sh` + `scripts/modbus_write_coil.py` pair (raw Modbus TCP over a tunnel) still works unchanged for local-Docker testing — it's just no longer needed for the Render URL.

## Visual simulation

`templates/electricgrid.html` has a detailed single-line electrical diagram (plain inline SVG, polled from `/api/status` every second), styled closer to an actual one-line diagram rather than a schematic box-and-arrow sketch:

- **Transmission tower → feeder breaker → busbar → step-down transformer → load**, drawn as a real single-line diagram would: a lattice tower silhouette for the grid tie, a breaker with two stationary contact terminals, a thick busbar with tick marks at each tap, a ground symbol, and an overlapping-circle transformer symbol before the load.
- **Sync-check relay**, labeled "25" (its real ANSI/IEEE device number) with a supervisory dashed line to the breaker — shown green/"CLEAR TO CLOSE" or red/"BLOCKING CLOSE" depending on whether frequency/voltage are currently within tolerance, making the interlock from the backend visible rather than just logged.
- **Live instrument meters** (Hz / pu) sit directly on the bus, the way real SCADA mimic diagrams show readings in place rather than in a separate panel.
- **Backup generator**: a generator-circle ("G") symbol below the bus, powered line animates amber (degraded, unaided) or blue (validated peer capacity, self-healing engaged).
- **Load**: a motor-circle ("M") symbol plus a small process-area building icon; dims to greyscale with a flashing **STOPPED** label when the feeder is down and self-healing is off.
- **PLC-A / PLC-B boxes** at the top, with dashed control-supervision lines down to the breaker/generator, highlighting whichever controller currently has authority — echoing Figure 1 of the paper.
- **Status banner**: "NORMAL OPERATION" (blue) → "SERVICE DOWN" (pulsing red, attack and no healing) → "SELF-HEALING IN PROGRESS" (amber, mid-recovery) → "RESTORED" (green, ~6s after an autonomous reclose).

All of it is driven purely by fields already in `/api/status` (`breaker_closed`, `self_healing`, `active_controller`, `last_recovery`, `frequency_hz`, `voltage_pu`) — no new backend state was needed, just a richer SVG and a slightly larger `updateDiagram()`.

The public dashboard (`/electricgrid`) intentionally does **not** show the curl attack command or a raw event feed — those live only in this README and on `/electricgrid/logs` (nothing links to it from outside). It color-codes entries: **ATTACK in red**, **ANOMALY in orange**, **RECOVERY in green**; everything else stays neutral blue. `/logs` now just redirects to `/electricgrid/logs`.

You may still see repeated `NETWORK` entries ("Modbus TCP connection from 127.0.0.1:...") — something inside Render's own infra probing the exposed Modbus port (loopback source, harmless). That's exactly why the public dashboard no longer shows the raw event feed at all.

## Second scenario: reactor coolant loop (`/nuclearplant`)

A second, visually distinct training scenario at `/nuclearplant` (with its own dedicated log page, `/nuclearplant/logs`, kept separate from the grid's), built the same way as the grid page but attacking a **coolant valve** instead of a breaker. It is a deliberately toy, fictional model — arbitrary units, no real reactor type, no real thermal-hydraulics or safety limits. It exists to (a) give a second attack demo where "closing something" is visually obvious, and (b) make the paper's independent-safety-layer idea (Section 4.1.4) directly visible: a **SIS automatic trip** (SCRAM) fires on high core temperature *regardless* of self-healing, while self-healing is a separate, slower, *validated* recovery path.

Attack it the same way, over plain HTTPS:
```bash
curl -X POST "https://YOUR-SERVICE.onrender.com/api/nuclear/write-valve" \
  -H "Content-Type: application/json" \
  -d '{"valve":"coolant_main","open":false}'
```
What happens, end to end (confirmed by test run): closing the valve drops coolant flow to 12%; core temperature climbs; around 4–5s in, the **independent SIS trip** fires and inserts all control rods regardless of self-healing — the reactor goes safely offline either way. Without self-healing the valve stays shut (manual `POST /api/nuclear/reset` needed, mirroring the grid page's forced-restart pattern). With self-healing enabled, once the trip is confirmed a validated backup pump engages (flow → 55%), and once temperature clears the thermal interlock (and a ~6s validation dwell has passed) the valve reopens autonomously and rods withdraw back toward nominal power.

Diagram highlights (same inline-SVG, same `/api/nuclear/status`-polling pattern as the grid page):
- **Reactor vessel** with 5 animated control rods that visibly insert/withdraw, and a core fill color that shifts blue → amber → red with temperature (pulsing at the trip threshold).
- **"SIS" lamp** that lights up red the instant the independent trip fires.
- **Primary loop** (vessel → hot leg → steam generator → cold leg, with the attack valve and main pump in the cold leg) inside a dashed **containment** boundary, plus a simplified **secondary loop** (turbine/condenser) outside it for visual complexity.
- **Backup pump** icon that spins and highlights only once self-healing's emergency cooling actually engages.
- Live **TEMP / PWR% / FLOW%** meters on the diagram itself, same philosophy as the grid page's Hz/pu meters.
- Same banner pattern: normal → "COOLANT LOSS DETECTED" → "SIS TRIP" → "SELF-HEALING IN PROGRESS" → "RESTORED".

Events are tagged `subsystem: 'grid'` or `'reactor'` internally, so `/api/electricgrid/logs` and `/api/nuclearplant/logs` each return only their own subsystem's entries — a real filter, not string-matching. Same unauthenticated-by-default posture, same `/api/nuclear/reset` / `/api/nuclear/self-healing` pattern as the grid endpoints. Full attack → trip → (optionally) self-healing → recovery sequence tested end-to-end via Flask's test client before packaging.

## Safety and limitations

- All process values are fabricated; no real grid physics, protection relays, physical PLCs, or reactor physics are modeled. The reactor page in particular is not based on, and must not be read as representing, any real reactor design, safety limit, or thermal-hydraulic behavior — it's an illustrative toy control loop.
- There is no authentication on either dashboard, the `/api/*` operator endpoints, `/api/ot/write-coil`, or `/api/nuclear/write-valve` by default — anyone with the URL can use all of them. See above for an optional token gate (the same pattern applies to the nuclear endpoint).
- A public, permanently-running write endpoint is still worth minimizing exposure for even in a synthetic lab — this is a teaching tool, not a hardened service. Don't link it from anywhere public beyond your presentation.
- State and logs are held in memory and may be lost on restart/sleep.
- This is a proof-of-concept, not a safety controller or production OT defense.


## Pinggy TCP tunnel package

This variant adds `scripts/start-pinggy-tcp.sh` and a small Modbus TCP client. The helper is intended for a temporary tunnel from a machine where the Modbus service is reachable. A Render Web Service does not provide an inbound arbitrary TCP listener, and a process running inside Render generally cannot bind a public TCP port for clients. Therefore this script is **not a way to expose Render's internal port from the Render service itself**. Use it only on a host you control that can reach the Modbus server, such as a local Docker host or a separate VM. A tunnel cannot magically bridge a Render service's private network to your laptop; for a Render-hosted deployment, use an appropriate controlled TCP-capable host or a deliberate relay architecture.

### Local Docker example

```bash
docker build -t self-healing-plc .
docker run --rm -p 10000:10000 -p 5020:5020 self-healing-plc
# In a second terminal, start the temporary TCP tunnel:
./scripts/start-pinggy-tcp.sh
```

Pinggy prints the assigned public TCP hostname and port. From your external Kali machine, run the included client (Python 3 is sufficient):

```bash
python3 scripts/modbus_write_coil.py --host YOUR_PINGGY_HOST --port YOUR_PINGGY_PORT --coil 0 --state open
```

This sends a real Modbus TCP function-5 write to the **synthetic lab**. Do not use it against any system you do not own or have explicit authorization to test. Stop the tunnel with Ctrl+C immediately after the demonstration. Pinggy's service options/syntax can change; consult its official documentation if the reverse tunnel command is rejected.
