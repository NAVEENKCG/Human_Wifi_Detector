import subprocess, re, time, threading, statistics, json, os, math
from collections import deque
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# -- Config ------------------------------------------------
WARMUP_SAMPLES   = 20      # calibration phase - no detection
WINDOW_SIZE      = 20      # rolling Z-score window
EMA_ALPHA        = 0.3     # smoothing factor (0=no change, 1=raw)
CONSECUTIVE_NEED = 2       # Z-spikes in a row before node.motion=True
COOLDOWN_SECS    = 4       # seconds before re-arming after event
PHONE_TIMEOUT    = 5       # seconds before phone marked offline
EXTENDER_TIMEOUT = 5
EVENT_LOG_FILE   = "events.jsonl"

# Node weights - floor-aware (router on ground floor = reference only)
NODE_WEIGHTS = {"laptop": 0.40, "phone": 0.35, "extender": 0.25}

# -- State --------------------------------------------------
nodes = {k: {
    "signal": 0, "smoothed": 0.0, "zscore": 0.0,
    "motion": False, "consecutive": 0,
    "timestamp": 0, "last_seen": 0,
    "calibrated": False, "baseline_mean": 0, "baseline_std": 0
} for k in NODE_WEIGHTS}

nodes["laptop"]["router"] = "Unknown"

windows    = {k: deque(maxlen=WINDOW_SIZE) for k in NODE_WEIGHTS}
warmup_buf = {k: [] for k in NODE_WEIGHTS}
ema_prev   = {k: None for k in NODE_WEIGHTS}
consec     = {k: 0 for k in NODE_WEIGHTS}
cooldown_until = 0

fused = {
    "state": "CALIBRATING",
    "motion": False,
    "confidence": 0.0,
    "source": "none",
    "active_nodes": 0,
    "cooldown": False,
    "timestamp": 0
}

# -- Signal processing -------------------------------------
# ── RSSI → Distance estimation (log-distance path loss model) ──
# Calibrate these for your home:
TX_POWER_DBM = -40    # RSSI at 1 metre (measure this once)
PATH_LOSS_EXP = 3.0   # 2=free space, 3-4=indoor with walls

def rssi_to_distance(rssi_percent):
    """Convert Windows signal % → estimated metres from router"""
    # Windows % to dBm approximation
    dbm = (rssi_percent / 2) - 100
    if dbm >= TX_POWER_DBM: return 0.5
    distance = 10 ** ((TX_POWER_DBM - dbm) / (10 * PATH_LOSS_EXP))
    return round(min(distance, 20.0), 2)  # cap at 20m

def estimate_positions():
    """
    Estimate 3D positions using extender as origin anchor.
    Returns x,y,z for each node in metres.
    Extender = fixed at (0,0,0) — it's wall-mounted.
    Router = fixed at (0,0,-4) — ground floor, approx 4m below.
    Phone + Laptop = estimated from RSSI distance to extender.
    """
    ext_sig = nodes["extender"]["signal"]
    lap_sig  = nodes["laptop"]["signal"]
    phn_sig  = nodes["phone"]["signal"]

    # Distance from extender for each mobile node
    lap_dist = rssi_to_distance(lap_sig)
    phn_dist = rssi_to_distance(phn_sig)

    # Without angle data we can only estimate a radius sphere.
    # Spread them angularly using their relative RSSI to router
    # as a weak angular hint (higher router RSSI = closer to stairwell)
    router_sig = nodes["laptop"]["signal"]  # laptop sees router directly

    return {
        "extender": {"x": 0,   "y": 0,   "z": 0,   "fixed": True,  "floor": 1},
        "router":   {"x": 0,   "y": 0,   "z":-4,   "fixed": True,  "floor": 0},
        "laptop":   {
            "x": round(lap_dist * 0.7, 2),
            "y": round(lap_dist * 0.3, 2),
            "z": round(lap_dist * 0.1, 2),
            "fixed": False, "floor": 1,
            "distance_from_extender": lap_dist
        },
        "phone": {
            "x": round(-phn_dist * 0.6, 2),
            "y": round(phn_dist * 0.5, 2),
            "z": round(phn_dist * 0.05, 2),
            "fixed": False, "floor": 1,
            "distance_from_extender": phn_dist
        }
    }

def ema_smooth(key, raw):
    if ema_prev[key] is None:
        ema_prev[key] = raw
    smoothed = EMA_ALPHA * raw + (1 - EMA_ALPHA) * ema_prev[key]
    ema_prev[key] = smoothed
    return round(smoothed, 2)

def zscore(val, win):
    if len(win) < 5: return 0.0
    m = statistics.mean(win)
    s = statistics.stdev(win) or 0.001
    return (val - m) / s

def adaptive_threshold(key):
    """2.2x above baseline noise - computed once during warmup"""
    n = nodes[key]
    if not n["calibrated"]: return 2.0
    return max(1.5, 2.2 * n["baseline_std"] / max(n["baseline_std"], 1))

def update_node(key, raw_signal):
    global cooldown_until
    n = nodes[key]
    now = time.time()

    # EMA smooth
    smoothed = ema_smooth(key, raw_signal)
    n["signal"]    = raw_signal
    n["smoothed"]  = smoothed
    n["timestamp"] = now
    n["last_seen"] = now

    # Warmup / calibration
    if not n["calibrated"]:
        warmup_buf[key].append(smoothed)
        if len(warmup_buf[key]) >= WARMUP_SAMPLES:
            buf = warmup_buf[key]
            n["baseline_mean"] = statistics.mean(buf)
            n["baseline_std"]  = statistics.stdev(buf) or 1.0
            n["calibrated"]    = True
        n["zscore"] = 0.0
        n["motion"] = False
        update_fused()
        return

    # Z-score
    windows[key].append(smoothed)
    z = zscore(smoothed, windows[key])
    n["zscore"] = round(z, 3)

    # Consecutive anomaly gate
    thresh = adaptive_threshold(key)
    if abs(z) > thresh:
        consec[key] += 1
    else:
        consec[key] = 0

    # Motion confirmed for this node
    in_cooldown = now < cooldown_until
    n["motion"] = (consec[key] >= CONSECUTIVE_NEED) and not in_cooldown
    if n["motion"]:
        consec[key] = 0  # reset after firing

    update_fused()

def update_fused():
    global cooldown_until
    now = time.time()
    in_cooldown = now < cooldown_until

    # Check calibration
    all_calibrated = all(nodes[k]["calibrated"] for k in NODE_WEIGHTS)
    if not all_calibrated:
        fused.update({"state": "CALIBRATING", "motion": False,
                      "confidence": 0.0, "source": "none",
                      "active_nodes": 0, "cooldown": False, "timestamp": now})
        return

    # Build active node list (fresh only)
    active, motion_nodes, conf = [], [], 0.0
    for k, w in NODE_WEIGHTS.items():
        fresh = (now - nodes[k]["last_seen"]) < (PHONE_TIMEOUT if k != "laptop" else 9999)
        if fresh:
            active.append(k)
            if nodes[k]["motion"]:
                motion_nodes.append(k)
                conf += w

    conf = round(min(conf, 1.0), 2)

    # 6-state ladder
    if in_cooldown:
        state = fused["state"]  # hold last state during cooldown
    elif conf == 0:
        watching = any(consec[k] == 1 for k in active)
        state = "WATCHING" if watching else "IDLE"
    elif conf <= 0.25:
        state = "ACTIVITY"
    elif conf <= 0.65:
        state = "MOTION"
    else:
        state = "CONFIRMED"

    # Log + cooldown on new confirmed events
    prev_motion = fused.get("motion", False)
    new_motion  = conf > 0
    if new_motion and not prev_motion and not in_cooldown:
        log_event(state, conf, motion_nodes)
        cooldown_until = now + COOLDOWN_SECS

    fused.update({
        "state": state,
        "motion": new_motion,
        "confidence": conf,
        "source": "+".join(motion_nodes) if motion_nodes else "none",
        "active_nodes": len(active),
        "cooldown": in_cooldown,
        "timestamp": now
    })

def log_event(state, conf, sources):
    event = {
        "timestamp": time.time(),
        "state": state,
        "confidence": conf,
        "source": "+".join(sources),
        "nodes": {k: {"signal": nodes[k]["signal"],
                      "zscore": nodes[k]["zscore"]} for k in NODE_WEIGHTS}
    }
    try:
        with open(EVENT_LOG_FILE, "a") as f:
            f.write(json.dumps(event) + "\n")
    except Exception:
        pass

# -- Laptop polling loop ------------------------------------
def laptop_loop():
    fail_count = 0
    while True:
        try:
            out = subprocess.check_output(
                "netsh wlan show interfaces",
                shell=True, timeout=3
            ).decode(errors="ignore")
            sig  = re.search(r"Signal\s*:\s*(\d+)%", out)
            ssid = re.search(r"SSID\s*:\s*(.+)", out)
            if sig:
                update_node("laptop", int(sig.group(1)))
                nodes["laptop"]["router"] = ssid.group(1).strip() if ssid else "Unknown"
                fail_count = 0
            else:
                fail_count += 1
        except Exception:
            fail_count += 1
        if fail_count >= 3:
            nodes["laptop"]["motion"] = False
        time.sleep(1)

# -- API ----------------------------------------------------
@app.route("/rssi")
def rssi():
    return jsonify({**nodes["laptop"], "fused_state": fused["state"]})

@app.route("/phone", methods=["POST"])
def phone_post():
    d = request.json or {}
    update_node("phone", int(d.get("signal", 0)))
    return jsonify({"status": "ok", "calibrated": nodes["phone"]["calibrated"]})

@app.route("/extender", methods=["POST"])
def extender_post():
    d = request.json or {}
    update_node("extender", int(d.get("signal", 0)))
    return jsonify({"status": "ok", "calibrated": nodes["extender"]["calibrated"]})

@app.route("/fused")
def get_fused():
    now = time.time()
    node_data = {}
    for k in NODE_WEIGHTS:
        n = nodes[k].copy()
        n["online"] = (now - n["last_seen"]) < (PHONE_TIMEOUT if k != "laptop" else 9999)
        node_data[k] = n
    return jsonify({
        **fused,
        "nodes": node_data,
        "positions": estimate_positions()
    })

@app.route("/history")
def history():
    events = []
    if os.path.exists(EVENT_LOG_FILE):
        with open(EVENT_LOG_FILE) as f:
            lines = f.readlines()
        for line in lines[-50:]:
            try: events.append(json.loads(line))
            except Exception: pass
    return jsonify({"events": events[::-1]})

@app.route("/set_threshold/<float:val>")
def set_threshold(val):
    for k in NODE_WEIGHTS:
        nodes[k]["_manual_threshold"] = val
    return jsonify({"threshold": val})

@app.route("/clear_history", methods=["POST"])
def clear_history():
    open(EVENT_LOG_FILE, "w").close()
    return jsonify({"status": "cleared"})

@app.route("/")
def index(): return app.send_static_file("index.html")

@app.route("/phone.html")
def phone_page(): return app.send_static_file("phone.html")

@app.route("/extender.html")
def ext_page(): return app.send_static_file("extender.html")

if __name__ == "__main__":
    threading.Thread(target=laptop_loop, daemon=True).start()
    print("=" * 48)
    print("  Human Wi-Fi Detector - upgraded backend")
    print("  http://localhost:5000")
    print("  Calibration: first 20 readings (20s)")
    print("=" * 48)
    app.run(host="0.0.0.0", port=5000, debug=False)
