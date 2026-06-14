import subprocess, re, time, threading, statistics, json, os, math
from collections import deque
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# -- Config ------------------------------------------------
WARMUP_SAMPLES   = 20      # calibration phase - no detection
WINDOW_SIZE      = 20      # rolling Z-score window
CONSECUTIVE_NEED = 2       # Z-spikes in a row before node.motion=True
COOLDOWN_SECS    = 4       # seconds before re-arming after event
PHONE_TIMEOUT    = 5       # seconds before phone marked offline
EXTENDER_TIMEOUT = 5
EVENT_LOG_FILE   = "events.jsonl"

# ── Tuned constants ────────────────────────────────
POLL_INTERVAL     = 0.5    # halves data latency
EMA_ALPHA         = 0.25   # slightly more smoothing
DISTANCE_EMA      = 0.15   # separate, slower EMA for distance only
HAMPEL_K          = 3      # window half-size for outlier filter
HAMPEL_T          = 2.8    # threshold multiplier

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

# ── Per-node distance EMA state ────────────────────
dist_ema = {"laptop": None, "phone": None, "extender": 0.0}

# ── Hampel outlier filter ──────────────────────────
def hampel_filter(value, window):
    """Replaces EMA for spike rejection — far better for RSSI"""
    if len(window) < 2 * HAMPEL_K + 1:
        return value
    w = list(window)[-(2 * HAMPEL_K + 1):]
    med = statistics.median(w)
    mad = statistics.median([abs(x - med) for x in w]) or 0.001
    return med if abs(value - med) > HAMPEL_T * 1.4826 * mad else value

# ── Smoothed distance with dedicated EMA ──────────
def smooth_distance(key, raw_signal):
    """
    Separate slower EMA just for distance — prevents dot thrashing.
    Signal EMA stays fast for Z-score detection.
    Distance EMA is slower for visual stability.
    """
    dbm = raw_signal / 2 - 100
    raw_dist = min(12.0, max(0.4, 10 ** ((-40 - dbm) / 30)))
    
    if dist_ema[key] is None:
        dist_ema[key] = raw_dist
    
    # Only update if change is significant (>0.2m) — kills micro-jitter
    if abs(raw_dist - dist_ema[key]) < 0.2:
        return round(dist_ema[key], 2)
    
    dist_ema[key] = DISTANCE_EMA * raw_dist + (1 - DISTANCE_EMA) * dist_ema[key]
    return round(dist_ema[key], 2)

# ── Position estimator with angular spread ─────────
_angle_offsets = {"laptop": 0.52, "phone": -0.85}  # radians — spread them apart

def estimate_position(key, signal):
    dist = smooth_distance(key, signal)
    angle = _angle_offsets.get(key, 0)
    # Positions on the 1st floor plane (y stays near 0)
    return {
        "x": round(dist * math.cos(angle), 3),
        "y": 0.15,
        "z": round(dist * math.sin(angle), 3),
        "fixed": False,
        "floor": 1,
        "distance_from_extender": dist
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

    all_calibrated = all(nodes[k]["calibrated"] for k in NODE_WEIGHTS)
    if not all_calibrated:
        fused.update({"state": "CALIBRATING", "motion": False,
                      "confidence": 0.0, "source": "none",
                      "active_nodes": 0, "cooldown": False, "timestamp": now})
        return

    active, motion_nodes, conf = [], [], 0.0
    for k, w in NODE_WEIGHTS.items():
        fresh = (now - nodes[k]["last_seen"]) < (PHONE_TIMEOUT if k != "laptop" else 9999)
        if fresh:
            active.append(k)
            if nodes[k]["motion"]:
                motion_nodes.append(k)
                conf += w

    conf = round(min(conf, 1.0), 2)

    if in_cooldown:
        state = fused["state"]
    elif conf == 0:
        watching = any(consec[k] == 1 for k in active)
        state = "WATCHING" if watching else "IDLE"
    elif conf <= 0.25:
        state = "ACTIVITY"
    elif conf <= 0.65:
        state = "MOTION"
    else:
        state = "CONFIRMED"

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

# ── Faster netsh with result caching ───────────────
_netsh_cache = {"signal": 0, "router": "Unknown", "ts": 0}
_CACHE_TTL = 0.4  # reuse result if fresher than 400ms

def get_laptop_rssi_fast():
    now = time.time()
    if now - _netsh_cache["ts"] < _CACHE_TTL:
        return _netsh_cache["signal"], _netsh_cache["router"]
    try:
        out = subprocess.check_output(
            "netsh wlan show interfaces",
            shell=True, timeout=1.5
        ).decode(errors="ignore")
        sig  = re.search(r"Signal\s*:\s*(\d+)%", out)
        ssid = re.search(r"SSID\s*:\s*(.+)", out)
        signal = int(sig.group(1)) if sig else _netsh_cache["signal"]
        router = ssid.group(1).strip() if ssid else _netsh_cache["router"]
        _netsh_cache.update({"signal": signal, "router": router, "ts": now})
        return signal, router
    except Exception:
        return _netsh_cache["signal"], _netsh_cache["router"]

def laptop_loop():
    fail_count = 0
    while True:
        signal, router = get_laptop_rssi_fast()
        if signal > 0:
            filtered = hampel_filter(signal, windows["laptop"])
            update_node("laptop", filtered)
            nodes["laptop"]["router"] = router
            fail_count = 0
        else:
            fail_count += 1
            if fail_count >= 5:
                nodes["laptop"]["motion"] = False
        time.sleep(POLL_INTERVAL)

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

# ── Upgrade /fused to include velocity hint ─────────
_prev_positions = {}
_prev_pos_time  = {}

@app.route("/fused")
def get_fused():
    now = time.time()
    node_data = {}
    for k in NODE_WEIGHTS:
        n = nodes[k].copy()
        n["online"] = (now - n["last_seen"]) < (PHONE_TIMEOUT if k != "laptop" else 9999)
        node_data[k] = n

    positions = {
        "extender": {"x":0,"y":0,"z":0,"fixed":True,"floor":1},
        "router":   {"x":0,"y":-4,"z":0,"fixed":True,"floor":0},
    }

    for k in ["laptop", "phone"]:
        sig = nodes[k]["smoothed"] or nodes[k]["signal"]
        pos = estimate_position(k, sig)

        # Compute velocity for client-side prediction
        prev = _prev_positions.get(k)
        prev_t = _prev_pos_time.get(k, now)
        dt = now - prev_t
        if prev and dt > 0:
            pos["vx"] = round((pos["x"] - prev["x"]) / dt, 4)
            pos["vz"] = round((pos["z"] - prev["z"]) / dt, 4)
        else:
            pos["vx"] = 0.0
            pos["vz"] = 0.0

        _prev_positions[k] = pos.copy()
        _prev_pos_time[k]  = now
        positions[k] = pos

    return jsonify({
        **fused,
        "nodes": node_data,
        "positions": positions,
        "server_ts": now
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
    app.run(host="0.0.0.0", port=5000, debug=False)
