import os, threading, time, socket, struct, random
from datetime import datetime, timezone
from flask import Flask, jsonify, render_template, request




app = Flask(__name__)
lock = threading.RLock()


# A Render web service exposes exactly one public port (HTTP/HTTPS). Raw
# Modbus TCP on MODBUS_PORT is therefore only reachable locally/in Docker,
# never from Render's public URL. /api/ot/write-coil below is a plain JSON
# POST route that rides that same public HTTPS port — no tunnel, no
# WebSocket, nothing extra to install — and calls the exact same set_coil()
# logic (including the sync-check interlock) as a real Modbus write would.
# It is intentionally unauthenticated by default, mirroring the original
# proof-of-concept this project started from; see README for how to add a
# shared-token check back if you want one for your demo.

# ---------------------------------------------------------------------------
# Physics model
#
# This is a SINGLE-MACHINE-EQUIVALENT lumped model: one aggregated generator
# against one aggregated load on one bus. It uses the classical generator
# swing equation, a first-order governor droop response, and a first-order
# AVR-style voltage lag. This is the standard simplified representation used
# in introductory power-system dynamics teaching and in several published OT
# security testbeds. It is NOT a multi-bus AC power-flow solution (no
# Newton-Raphson / Gauss-Seidel load flow, no network topology, no reactive
# power dispatch). All constants below are illustrative, not calibrated to
# any real asset, and all data produced by this service is synthetic.
# ---------------------------------------------------------------------------


F_NOM = 50.0          # nominal grid frequency, Hz
H = 4.0               # generator inertia constant, s
D = 1.0               # load damping coefficient, pu power / pu freq
R_DROOP = 0.04        # governor droop, 4%
T_GOV = 3.0           # governor actuator time constant, s
T_AVR = 2.0           # voltage regulator (AVR) time constant, s
DT = 0.5              # integration step, s
SYNC_F_TOL = 0.25     # Hz tolerance for a sync-check relay to permit reclosing
SYNC_V_TOL = 0.04     # pu tolerance for a sync-check relay to permit reclosing
PGEN_ISLAND_ALONE = 0.60   # local backup generation capacity, pu (no peer support) — undersized
                            # relative to typical load, as real standby/backup generation often is
PGEN_ISLAND_PEER = 0.90    # local + validated peer/backup generation capacity, pu — adequate
MIN_RECOVERY_DWELL = 5.0   # s — minimum time in Recovery_Mode before an autonomous reclose,
                            # standing in for the paper's trust-evaluation / peer-confirmation /
                            # shadow-validation window (Fig. 2) rather than an instant reflex


state = {
    'breaker_closed': True, 'self_healing': False,
    'frequency_hz': 50.0, 'voltage_pu': 1.0,
    'delta_f': 0.0, 'Pm': 0.65, 'Pload': 0.65,
    'load_pct': 65.0, 'generation_pct': 65.0,
    'active_controller': 'PLC-A', 'last_recovery': None, 'started': time.time(),
    'events': [], 'anomalies': [], 'attack_count': 0,
    'islanded_since': None, '_last_anomaly_log': 0.0,
}
MAX_EVENTS = 300



def log(kind, message):
    entry = {'time': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'kind': kind, 'message': message}
    with lock:
        state['events'].insert(0, entry); del state['events'][MAX_EVENTS:]
        if kind in ('ANOMALY', 'ATTACK', 'RECOVERY'):
            state['anomalies'].insert(0, entry); del state['anomalies'][MAX_EVENTS:]
    print(f"[{entry['time']}] {kind}: {message}", flush=True)



def _sync_ok_locked():
    """Caller must hold `lock`. True if frequency/voltage are close enough to
    nominal for a sync-check relay to permit a breaker close, mirroring a
    real ANSI/IEEE device-25 sync-check function."""
    return (abs(state['frequency_hz'] - F_NOM) <= SYNC_F_TOL
            and abs(state['voltage_pu'] - 1.0) <= SYNC_V_TOL)



def process_loop():
    while True:
        time.sleep(DT)
        just_recovered = False
        should_log_anomaly = False
        with lock:
            # Load fluctuates around a fixed baseline: real telemetry is never
            # perfectly flat, but a mean-reverting walk keeps the generation
            # deficit during islanding consistent run to run.
            state['Pload'] += (0.65 - state['Pload']) * 0.05 + random.gauss(0, 0.003)
            state['Pload'] = max(0.60, min(0.70, state['Pload']))
            healing = state['self_healing']

            if state['breaker_closed']:
                # Grid-tied: treat the wider system as an infinite bus. A single
                # feeder cannot move bulk-grid frequency/voltage on its own, so
                # both relax quickly toward nominal with small measurement noise.
                state['islanded_since'] = None
                state['delta_f'] += (0.0 - state['delta_f']) * 0.6 + random.gauss(0, 0.004)
                state['voltage_pu'] += (1.0 - state['voltage_pu']) * 0.5 + random.gauss(0, 0.002)
                state['Pm'] += (state['Pload'] - state['Pm']) * 0.5
            else:
                # Islanded on local backup generation: classical swing equation.
                if state['islanded_since'] is None:
                    state['islanded_since'] = time.time()

                pgen_cap = PGEN_ISLAND_PEER if healing else PGEN_ISLAND_ALONE
                pm_target = state['Pm'] - (1.0 / R_DROOP) * (state['delta_f'] / F_NOM)
                pm_target = max(0.0, min(pgen_cap, pm_target))
                state['Pm'] += (pm_target - state['Pm']) * (DT / T_GOV)

                accel_power = state['Pm'] - state['Pload'] - D * (state['delta_f'] / F_NOM)
                state['delta_f'] += (accel_power / (2 * H)) * DT * F_NOM

                if healing:
                    # Secondary (AGC-equivalent) correction: stands in for the
                    # paper's validated peer-controller takeover, which actively
                    # restores nominal rather than leaving a permanent droop offset.
                    state['delta_f'] += (0.0 - state['delta_f']) * 0.15

                v_target = 1.0 - 0.35 * max(0.0, state['Pload'] - state['Pm'])
                if healing:
                    v_target += (1.0 - v_target) * 0.5
                state['voltage_pu'] += (v_target - state['voltage_pu']) * (DT / T_AVR)

            state['frequency_hz'] = round(F_NOM + state['delta_f'], 3)
            state['voltage_pu'] = round(max(0.0, state['voltage_pu']), 3)
            state['load_pct'] = round(state['Pload'] * 100, 1)
            state['generation_pct'] = round(state['Pm'] * 100, 1)

            abnormal = ((not state['breaker_closed'])
                        or abs(state['frequency_hz'] - F_NOM) > 0.1
                        or abs(state['voltage_pu'] - 1.0) > 0.02)
            now = time.time()
            if abnormal and (now - state['_last_anomaly_log'] > 3.0):
                should_log_anomaly = True
                state['_last_anomaly_log'] = now

            # Autonomous reclose: only once self-healing has actually restored
            # the system to within sync-check tolerance AND spent at least
            # MIN_RECOVERY_DWELL seconds in Recovery_Mode (see note above).
            dwell_ok = (state['islanded_since'] is not None
                        and now - state['islanded_since'] >= MIN_RECOVERY_DWELL)
            if healing and not state['breaker_closed'] and dwell_ok and _sync_ok_locked():
                state['breaker_closed'] = True
                state['active_controller'] = 'PLC-B (recovery controller)'
                state['last_recovery'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
                state['islanded_since'] = None
                just_recovered = True

        if should_log_anomaly:
            log('ANOMALY', f'Feeder islanded — frequency {state["frequency_hz"]:.2f} Hz, '
                            f'voltage {state["voltage_pu"]:.2f} pu outside operating envelope')
        if just_recovered:
            log('RECOVERY', 'Validated peer/backup generation restored nominal frequency and '
                             'voltage; sync-check satisfied; breaker reclosed autonomously')

threading.Thread(target=process_loop, daemon=True).start()



def snapshot():
    with lock:
        hide = ('events', 'anomalies', '_last_anomaly_log')
        return ({k: v for k, v in state.items() if k not in hide}
                | {'events': state['events'][:80], 'anomalies': state['anomalies'][:80],
                   'uptime_seconds': int(time.time() - state['started'])})



@app.get('/')
def index(): return render_template('index.html')



@app.get('/logs')
def logs_page(): return render_template('logs.html')



@app.get('/api/status')
def api_status(): return jsonify(snapshot())



@app.get('/api/logs')
def api_logs():
    with lock: return jsonify({'events': state['events'][:200], 'anomalies': state['anomalies'][:200]})



@app.post('/api/self-healing')
def toggle_healing():
    enabled = bool(request.get_json(silent=True) and request.get_json().get('enabled'))
    with lock: state['self_healing'] = enabled
    log('CONFIG', f'Self-healing {"enabled" if enabled else "disabled"} from dashboard')
    return jsonify({'ok': True, 'self_healing': enabled})



@app.post('/api/reset')
def reset():
    # Represents a manual operator-forced restart: bypasses the sync-check
    # relay the way a human operator manually re-energizing a feeder would.
    with lock:
        state.update(breaker_closed=True, frequency_hz=50.0, voltage_pu=1.0,
                      delta_f=0.0, Pm=0.65, Pload=0.65,
                      active_controller='PLC-A', last_recovery=None, islanded_since=None)
    log('OPERATOR', 'Manual intervention: operator forced a restart and re-energized the '
                     'feeder, bypassing the automatic sync-check')
    return jsonify({'ok': True})



@app.post('/api/ot/write-coil')
def external_coil_write():
    # Training endpoint: lets an attacker script drive the same coil-0 write
    # that a real Modbus TCP function-5 write would, directly over HTTPS —
    # no tunnel needed. Restricted to boolean writes on coil 0 only.
    data = request.get_json(silent=True) or {}
    address = data.get('address')
    value = data.get('value')
    if address != 0 or not isinstance(value, bool):
        return jsonify({'ok': False, 'error': 'Only boolean writes to coil 0 are permitted'}), 400

    accepted = set_coil(address, value, source='External attacker interface')
    if not accepted:
        # Mirrors the Modbus path: a close request while out of sync-check
        # tolerance is refused, not silently granted.
        return jsonify({
            'ok': False,
            'error': 'Close command rejected by sync-check relay: frequency/voltage not yet within tolerance',
            'frequency_hz': state['frequency_hz'], 'voltage_pu': state['voltage_pu'],
        }), 409

    return jsonify({'ok': True, 'coil': address, 'breaker_closed': value,
                     'message': 'Synthetic feeder state updated'})




# Minimal Modbus TCP server for this isolated training simulator.
# Coils: 0=feeder breaker closed. Holding registers: 0=frequency x100, 1=voltage x100,
# 2=load percent, 3=generation percent. No real equipment is connected.


def get_coil(addr):
    with lock: return bool(state['breaker_closed']) if addr == 0 else False



def set_coil(addr, value, source='Modbus TCP client'):
    if addr != 0: return False
    with lock:
        old = state['breaker_closed']
        if value and not old and not _sync_ok_locked():
            # Mirrors a real sync-check relay refusing to close an out-of-synchronism breaker.
            log('CONTROL', f'{source} CLOSE command REJECTED by sync-check relay '
                            f'(feeder at {state["frequency_hz"]:.2f} Hz, {state["voltage_pu"]:.2f} pu)')
            return False
        state['breaker_closed'] = bool(value)
        if old and not value:
            state['attack_count'] += 1
    log('ATTACK' if not value else 'CONTROL', f'{source} wrote coil 0 (feeder breaker closed={bool(value)})')
    return True



def get_register(addr):
    with lock:
        values = [int(state['frequency_hz'] * 100), int(state['voltage_pu'] * 100),
                  int(state['load_pct']), int(state['generation_pct'])]
        return values[addr] if 0 <= addr < len(values) else 0



def handle_modbus_adu(frame: bytes, source: str):
    """Parse one complete Modbus TCP ADU (7-byte MBAP header + PDU) and
    return the response ADU bytes, or None if the frame is too short to
    contain a header. Shared by both the raw-TCP server and the WebSocket
    endpoint so the two transports speak byte-identical Modbus."""
    if len(frame) < 7:
        return None
    tid, pid, length, unit = struct.unpack('>HHHB', frame[:7])
    pdu = frame[7:7 + max(0, length - 1)]
    fn = pdu[0] if pdu else 0
    response = b''
    try:
        if fn in (1, 3):
            addr, qty = struct.unpack('>HH', pdu[1:5]); qty = max(1, min(qty, 125 if fn == 3 else 2000))
            if fn == 1:
                bits = [get_coil(addr + i) for i in range(qty)]
                packed = bytearray((qty + 7) // 8)
                for i, b in enumerate(bits):
                    if b: packed[i // 8] |= 1 << (i % 8)
                response = bytes([fn, len(packed)]) + bytes(packed)
            else:
                vals = [get_register(addr + i) for i in range(qty)]
                response = bytes([fn, qty * 2]) + b''.join(struct.pack('>H', v) for v in vals)
        elif fn == 5:
            addr, val = struct.unpack('>HH', pdu[1:5])
            if val not in (0x0000, 0xFF00) or not set_coil(addr, val == 0xFF00, source=source): raise ValueError()
            response = pdu[:5]
        elif fn == 6:
            addr, val = struct.unpack('>HH', pdu[1:5])
            response = pdu[:5]
            log('CONTROL', f'{source} wrote holding register {addr}={val}; register writes are recorded but do not alter fixed model parameters')
        else:
            response = bytes([fn | 0x80, 1])
    except Exception:
        response = bytes([fn | 0x80, 2])
    return struct.pack('>HHHB', tid, pid, len(response) + 1, unit) + response



def modbus_client(conn):
    try:
        while True:
            header = conn.recv(7)
            if not header: break
            while len(header) < 7:
                more = conn.recv(7 - len(header))
                if not more: return
                header += more
            tid, pid, length, unit = struct.unpack('>HHHB', header)
            if length < 2 or length > 260: break
            pdu = b''
            while len(pdu) < length - 1:
                chunk = conn.recv(length - 1 - len(pdu))
                if not chunk: return
                pdu += chunk
            if not pdu: break
            response = handle_modbus_adu(header + pdu, source='Modbus TCP client')
            if response: conn.sendall(response)
    except (ConnectionError, OSError, struct.error): pass
    finally: conn.close()



def modbus_server():
    host = '0.0.0.0'; port = int(os.getenv('MODBUS_PORT', '5020'))
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port)); srv.listen(30)
    log('SYSTEM', f'Synthetic Modbus TCP server listening on {host}:{port} (reachable locally/in Docker only)')
    while True:
        conn, addr = srv.accept(); log('NETWORK', f'Modbus TCP connection from {addr[0]}:{addr[1]}')
        threading.Thread(target=modbus_client, args=(conn,), daemon=True).start()

threading.Thread(target=modbus_server, daemon=True).start()




if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '10000')), threaded=True)
