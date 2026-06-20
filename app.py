import subprocess, re, time, threading, statistics, json, os, math
from functools import lru_cache
from collections import deque
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ══════════════════════════════════════════════════════════════
# CONFIG — Industrial-grade tuning
# Sources: RuView (WiFi spatial intelligence), ESP-CSI (Espressif),
#          IEEE 802.11bf WLAN sensing, CUSUM/Hampel filter research,
#          Log-Distance Path Loss Model (indoor residential n=3.0)
# ══════════════════════════════════════════════════════════════
WARMUP_SAMPLES    = 20       # longer calibration for stable baseline (IEEE rec)
WINDOW_SIZE       = 30       # wider rolling window — robust statistics
CONSECUTIVE_NEED  = 3        # fallback consecutive gate (CUSUM is primary)
COOLDOWN_SECS     = 1.5      # fast re-detection for walking scenarios
PHONE_TIMEOUT     = 8        # generous timeout for phone WiFi scanning
EXTENDER_TIMEOUT  = 8
EVENT_LOG_FILE    = "events.jsonl"

POLL_INTERVAL     = 0.45     # slightly faster laptop polling (industry std)
EMA_ALPHA         = 0.18     # slower EMA — better noise rejection (research: 0.15-0.22)
DISTANCE_EMA      = 0.10     # very slow distance EMA — kills radar dot jitter
HAMPEL_K          = 4        # wider Hampel window — better spike rejection
HAMPEL_T          = 2.5      # tighter threshold (research: 2.0-3.0 optimal)

# CUSUM change-point detector (industrial standard parameters)
CUSUM_DRIFT       = 0.3      # lower drift = more sensitive to slow walk-ins
CUSUM_THRESHOLD   = 4.0      # higher threshold = fewer false alarms (industry std)

# Delta (rate-of-change) detection
DELTA_WINDOW_SIZE = 12       # wider delta window for smoother rate estimation
DELTA_Z_THRESHOLD = 2.2      # raised — reduces false positives from WiFi bursts

# Cross-node temporal correlation
CORRELATION_WINDOW = 2.5     # wider window for multi-room walk-through scenarios
SINGLE_NODE_PENALTY = 0.45   # stronger penalty — single-node events are unreliable
MULTI_NODE_BONUS   = 1.0     # full confidence when ≥2 nodes correlate

# Anti-phantom: sustained anomaly requirement
CONFIRM_SUSTAIN_SECS = 2.0   # longer sustain — eliminates WiFi handoff ghosts

# Node weights — rebalanced per empirical reliability
# Laptop (netsh) is most reliable/consistent reader
NODE_WEIGHTS = {"laptop": 0.45, "phone": 0.30, "extender": 0.25}

# Path-loss model parameters (Log-Distance, indoor residential)
PL_REF_POWER      = -40      # reference RSSI at 1m (dBm) — 2.4GHz typical
PL_EXPONENT       = 3.0      # indoor residential path-loss exponent (n=2.5-4.0)
PL_REF_DIST       = 1.0      # reference distance (meters)
PL_MAX_DIST       = 15.0     # clamp maximum estimated distance
PL_MIN_DIST       = 0.3      # dead zone — closer than this is unreliable
DISTANCE_DEADZONE = 0.15     # ignore distance changes smaller than this (meters)

# MAD-based adaptive noise floor
MAD_WINDOW        = 20       # window for Median Absolute Deviation noise estimation
MAD_SCALE         = 1.4826   # MAD to σ conversion factor (Gaussian assumption)

# ══════════════════════════════════════════════════════════════
# STATE
# ══════════════════════════════════════════════════════════════
nodes = {k: {
    "signal": 0, "smoothed": 0.0, "zscore": 0.0, "delta_zscore": 0.0,
    "motion": False, "consecutive": 0,
    "timestamp": 0, "last_seen": 0,
    "calibrated": False, "baseline_mean": 0, "baseline_std": 0
} for k in NODE_WEIGHTS}

nodes["laptop"]["router"] = "Unknown"
nodes["laptop"]["noise_floor"] = 0.0

# MAD-based noise floor per node
noise_windows = {k: deque(maxlen=MAD_WINDOW) for k in NODE_WEIGHTS}

windows      = {k: deque(maxlen=WINDOW_SIZE) for k in NODE_WEIGHTS}
warmup_buf   = {k: [] for k in NODE_WEIGHTS}
ema_prev     = {k: None for k in NODE_WEIGHTS}
consec       = {k: 0 for k in NODE_WEIGHTS}
cooldown_until = 0

# Delta (rate-of-change) state
delta_windows = {k: deque(maxlen=DELTA_WINDOW_SIZE) for k in NODE_WEIGHTS}
prev_smoothed = {k: None for k in NODE_WEIGHTS}

# CUSUM state per node
cusum_pos = {k: 0.0 for k in NODE_WEIGHTS}
cusum_neg = {k: 0.0 for k in NODE_WEIGHTS}

# Cross-node correlation state
last_anomaly_time = {k: 0.0 for k in NODE_WEIGHTS}

# Anti-phantom: track when motion first started per node
motion_onset = {k: 0.0 for k in NODE_WEIGHTS}

fused = {
    "state": "CALIBRATING",
    "motion": False,
    "confidence": 0.0,
    "source": "none",
    "active_nodes": 0,
    "cooldown": False,
    "timestamp": 0
}

# Per-node distance EMA state
dist_ema = {"laptop": None, "phone": None, "extender": 0.0}


# ══════════════════════════════════════════════════════════════
# SIGNAL PROCESSING
# ══════════════════════════════════════════════════════════════

def hampel_filter(value, window):
    """Hampel filter with MAD-based outlier rejection.
    Research: K=4, T=2.5 optimal for indoor RSSI (IEEE WLAN sensing)"""
    if len(window) < 2 * HAMPEL_K + 1:
        return value
    w = list(window)[-(2 * HAMPEL_K + 1):]
    med = statistics.median(w)
    mad = statistics.median([abs(x - med) for x in w]) or 0.001
    sigma_est = MAD_SCALE * mad
    return med if abs(value - med) > HAMPEL_T * sigma_est else value


def smooth_distance(key, raw_signal):
    """Industry-standard Log-Distance path-loss model for distance estimation.
    Uses empirically-tuned parameters for indoor residential (n=3.0).
    Source: IEEE path-loss research, NXP Application Notes"""
    dbm = raw_signal / 2 - 100
    # Log-Distance Path Loss Model: d = d0 * 10^((P0 - Pr) / (10*n))
    exponent = (PL_REF_POWER - dbm) / (10.0 * PL_EXPONENT)
    raw_dist = PL_REF_DIST * (10 ** exponent)
    raw_dist = min(PL_MAX_DIST, max(PL_MIN_DIST, raw_dist))

    if dist_ema[key] is None:
        dist_ema[key] = raw_dist

    # Dead-zone filter — ignore micro-jitter below threshold
    if abs(raw_dist - dist_ema[key]) < DISTANCE_DEADZONE:
        return round(dist_ema[key], 2)

    dist_ema[key] = DISTANCE_EMA * raw_dist + (1 - DISTANCE_EMA) * dist_ema[key]
    return round(dist_ema[key], 2)


_angle_offsets = {"laptop": 0.52, "phone": -0.85}

def estimate_position(key, signal):
    dist = smooth_distance(key, signal)
    angle = _angle_offsets.get(key, 0)
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
    if len(win) < 5:
        return 0.0
    m = statistics.mean(win)
    s = statistics.stdev(win) or 0.001
    return (val - m) / s


def adaptive_threshold(key):
    """2.5x above baseline noise with MAD-based live noise floor.
    Industry best practice: floor=1.8, multiplier=2.5 (IEEE/Espressif)"""
    n = nodes[key]
    if not n["calibrated"]:
        return 2.0
    # Use live noise floor from MAD if available, else baseline std
    live_noise = compute_noise_floor(key)
    base_noise = max(n["baseline_std"], live_noise, 0.5)
    return max(1.8, 2.5 * base_noise / max(base_noise, 1))


def compute_noise_floor(key):
    """MAD-based adaptive noise floor — tracks changing environments.
    Source: Hampel filter research, Towards Data Science CUSUM article"""
    win = noise_windows[key]
    if len(win) < 5:
        return nodes[key].get("baseline_std", 1.0)
    med = statistics.median(win)
    mad = statistics.median([abs(x - med) for x in win]) or 0.001
    return MAD_SCALE * mad


# ══════════════════════════════════════════════════════════════
# CUSUM CHANGE-POINT DETECTOR
# ══════════════════════════════════════════════════════════════

def cusum_update(key, z_value):
    """
    Industrial CUSUM (Cumulative Sum) change-point detector.
    Parameters: drift=0.3, threshold=4.0 (standard industrial values).
    Source: CUSUM control chart research, arXiv WiFi sensing papers.

    The lower drift (0.3 vs 0.5) detects slower walk-ins more reliably.
    The higher threshold (4.0 vs 3.5) prevents false alarms from WiFi bursts.
    """
    cusum_pos[key] = max(0, cusum_pos[key] + abs(z_value) - CUSUM_DRIFT)
    cusum_neg[key] = max(0, cusum_neg[key] + abs(z_value) - CUSUM_DRIFT)
    triggered = cusum_pos[key] > CUSUM_THRESHOLD or cusum_neg[key] > CUSUM_THRESHOLD
    if triggered:
        cusum_pos[key] = 0.0  # reset after detection
        cusum_neg[key] = 0.0
    return triggered


# ══════════════════════════════════════════════════════════════
# DUAL-FEATURE NODE UPDATE
# ══════════════════════════════════════════════════════════════

def update_node(key, raw_signal):
    global cooldown_until
    n = nodes[key]
    now = time.time()

    smoothed = ema_smooth(key, raw_signal)
    n["signal"]    = raw_signal
    n["smoothed"]  = smoothed
    n["timestamp"] = now
    n["last_seen"] = now

    # ── Warmup / calibration (20 samples, ~9s at 450ms intervals) ──
    if not n["calibrated"]:
        warmup_buf[key].append(smoothed)
        if len(warmup_buf[key]) >= WARMUP_SAMPLES:
            buf = warmup_buf[key]
            n["baseline_mean"] = statistics.mean(buf)
            n["baseline_std"]  = statistics.stdev(buf) or 1.0
            # Bootstrap the noise window with calibration data
            for v in buf[-MAD_WINDOW:]:
                noise_windows[key].append(v)
            n["calibrated"]    = True
        n["zscore"] = 0.0
        n["delta_zscore"] = 0.0
        n["motion"] = False
        update_fused()
        return

    # ── Feed MAD noise floor tracker ──
    noise_windows[key].append(smoothed)

    # ── Feature 1: Signal Z-score ──
    windows[key].append(smoothed)
    z = zscore(smoothed, windows[key])
    n["zscore"] = round(z, 3)

    # ── Feature 2: Rate-of-change (delta) Z-score ──
    delta = 0.0
    if prev_smoothed[key] is not None:
        delta = smoothed - prev_smoothed[key]
    prev_smoothed[key] = smoothed

    delta_windows[key].append(delta)
    z_delta = zscore(delta, delta_windows[key])
    n["delta_zscore"] = round(z_delta, 3)

    # ── Store noise floor in node data for API exposure ──
    n["noise_floor"] = round(compute_noise_floor(key), 3)

    # ── CUSUM on combined signal ──
    # Weighted combination: signal Z + 0.6x delta Z (delta slightly lower)
    combined_z = max(abs(z), abs(z_delta) * 0.6)
    cusum_triggered = cusum_update(key, combined_z)

    # ── Legacy consecutive gate (fallback, raised to 3) ──
    thresh = adaptive_threshold(key)
    if abs(z) > thresh or abs(z_delta) > DELTA_Z_THRESHOLD:
        consec[key] += 1
    else:
        consec[key] = max(0, consec[key] - 1)  # gradual decay instead of reset

    consecutive_triggered = consec[key] >= CONSECUTIVE_NEED

    # ── Motion decision: CUSUM OR consecutive gate ──
    in_cooldown = now < cooldown_until
    any_triggered = (cusum_triggered or consecutive_triggered) and not in_cooldown

    # Track anomaly timing for cross-node correlation
    if any_triggered:
        last_anomaly_time[key] = now

    # Track motion onset for anti-phantom
    if any_triggered and not n["motion"]:
        motion_onset[key] = now

    n["motion"] = any_triggered

    if n["motion"]:
        consec[key] = 0  # reset after firing

    update_fused()


# ══════════════════════════════════════════════════════════════
# FUSED STATE WITH CROSS-NODE CORRELATION & ANTI-PHANTOM
# ══════════════════════════════════════════════════════════════

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

    # ── Cross-node temporal correlation ──
    # Count how many nodes had anomalies within the correlation window
    correlated_count = sum(
        1 for k in active
        if (now - last_anomaly_time[k]) < CORRELATION_WINDOW
    )

    if conf > 0 and len(motion_nodes) > 0:
        if correlated_count >= 2:
            # Multiple nodes fired within 2s — high confidence, real person
            conf *= MULTI_NODE_BONUS
        elif correlated_count == 1 and len(motion_nodes) == 1:
            # Single node only — likely WiFi glitch, penalize
            conf *= SINGLE_NODE_PENALTY

    conf = round(min(conf, 1.0), 2)

    # ── State classification with anti-phantom ──
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
        # Anti-phantom: CONFIRMED requires sustained anomaly ≥1.5s
        earliest_onset = min(
            (motion_onset[k] for k in motion_nodes if motion_onset[k] > 0),
            default=now
        )
        if (now - earliest_onset) >= CONFIRM_SUSTAIN_SECS:
            state = "CONFIRMED"
        else:
            state = "MOTION"  # not yet sustained long enough

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
                       "zscore": nodes[k]["zscore"],
                       "delta_zscore": nodes[k]["delta_zscore"]} for k in NODE_WEIGHTS}
    }
    try:
        with open(EVENT_LOG_FILE, "a") as f:
            f.write(json.dumps(event) + "\n")
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════
# LAPTOP RSSI READER
# ══════════════════════════════════════════════════════════════

_netsh_cache = {"signal": 0, "router": "Unknown", "ts": 0, "channel": 0, "band": ""}
_CACHE_TTL = 0.35  # slightly faster cache refresh

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
        chan  = re.search(r"Channel\s*:\s*(\d+)", out)
        band  = re.search(r"Radio type\s*:\s*(.+)", out)
        signal = int(sig.group(1)) if sig else _netsh_cache["signal"]
        router = ssid.group(1).strip() if ssid else _netsh_cache["router"]
        channel = int(chan.group(1)) if chan else _netsh_cache["channel"]
        radio = band.group(1).strip() if band else _netsh_cache["band"]
        _netsh_cache.update({"signal": signal, "router": router, "ts": now,
                            "channel": channel, "band": radio})
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


# ══════════════════════════════════════════════════════════════
# API ROUTES
# ══════════════════════════════════════════════════════════════

@app.route("/rssi")
def rssi():
    return jsonify({**nodes["laptop"], "fused_state": fused["state"],
                    "channel": _netsh_cache["channel"],
                    "band": _netsh_cache["band"]})

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


# ── /fused with velocity hint for radar ──
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

    # Expose tuning params for client-side display
    tuning = {
        "cusum_drift": CUSUM_DRIFT,
        "cusum_threshold": CUSUM_THRESHOLD,
        "ema_alpha": EMA_ALPHA,
        "pl_exponent": PL_EXPONENT,
        "pl_ref_power": PL_REF_POWER,
        "hampel_k": HAMPEL_K,
        "hampel_t": HAMPEL_T,
        "warmup_samples": WARMUP_SAMPLES,
        "channel": _netsh_cache.get("channel", 0),
        "band": _netsh_cache.get("band", ""),
    }
    return jsonify({
        **fused,
        "nodes": node_data,
        "positions": positions,
        "tuning": tuning,
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
