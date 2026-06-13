import subprocess
import re
import time
import threading
import statistics
from collections import deque
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

app = Flask(__name__, static_folder='static')
CORS(app)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
WINDOW = 20
THRESHOLD = 2.0
CONSECUTIVE_NEEDED = 2

# ---------------------------------------------------------------------------
# Node registry — floor tagged
# Router is ground floor (reference only — vertical through-slab signal
# rarely changes when someone walks on the first floor).
# The real sensing mesh is the triangle: Extender — Laptop — Phone
# on the first floor.
# ---------------------------------------------------------------------------
NODE_META = {
    "laptop":   {"floor": 1, "role": "primary"},
    "phone":    {"floor": 1, "role": "primary"},
    "extender": {"floor": 1, "role": "primary"},
    "router":   {"floor": 0, "role": "reference"},
}

nodes = {k: {"signal": 0, "zscore": 0.0, "motion": False,
             "timestamp": 0, "last_seen": 0} for k in NODE_META}
windows = {k: deque(maxlen=WINDOW) for k in NODE_META}
consec  = {k: 0 for k in NODE_META}

fused = {"motion": False, "confidence": 0.0, "source": "none",
         "floor1_active": 0, "timestamp": 0}


# ---------------------------------------------------------------------------
# Signal analysis
# ---------------------------------------------------------------------------
def zscore(val, win):
    if len(win) < 5:
        return 0.0
    m = statistics.mean(win)
    s = statistics.stdev(win) or 0.001
    return (val - m) / s


def update_node(key, signal):
    """Update a node's signal and recompute detection."""
    windows[key].append(signal)
    z = zscore(signal, windows[key])
    motion = False

    if len(windows[key]) >= 5 and abs(z) > THRESHOLD:
        consec[key] += 1
        if consec[key] >= CONSECUTIVE_NEEDED:
            motion = True
            consec[key] = 0
    else:
        consec[key] = 0

    nodes[key].update({
        "signal": signal,
        "zscore": round(z, 3),
        "motion": motion,
        "timestamp": time.time(),
        "last_seen": time.time()
    })
    update_fused()


def update_fused():
    """Recompute fused detection — only floor-1 nodes contribute."""
    global fused
    now = time.time()

    floor1_nodes = [k for k, m in NODE_META.items() if m["floor"] == 1]
    active = []
    conf = 0.0

    for k in floor1_nodes:
        fresh = (now - nodes[k]["last_seen"]) < 5
        if k == "laptop":
            fresh = True  # laptop is always local
        if fresh:
            active.append(k)
            if nodes[k]["motion"]:
                conf += 1.0 / len(floor1_nodes)

    sources = [k for k in active if nodes[k]["motion"]]

    fused = {
        "motion": conf >= (1.0 / len(floor1_nodes)),
        "confidence": round(conf, 2),
        "source": "+".join(sources) if sources else "none",
        "floor1_active": len(active),
        "timestamp": now
    }


# ---------------------------------------------------------------------------
# Laptop RSSI loop (Windows netsh — reads router signal)
# ---------------------------------------------------------------------------
def laptop_loop():
    while True:
        try:
            out = subprocess.check_output(
                "netsh wlan show interfaces",
                shell=True).decode(errors="ignore")
            sig = re.search(r"Signal\s*:\s*(\d+)%", out)
            ssid = re.search(r"SSID\s*:\s*(.+)", out)
            if sig:
                update_node("laptop", int(sig.group(1)))
            nodes["laptop"]["router"] = ssid.group(1).strip() if ssid else "Unknown"
        except Exception:
            pass
        time.sleep(1)


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory('static', 'index.html')


@app.route("/rssi")
def rssi():
    return jsonify(nodes["laptop"])


@app.route("/phone", methods=["POST"])
def phone_post():
    d = request.json or {}
    update_node("phone", d.get("signal", 0))
    return jsonify({"status": "ok"})


@app.route("/extender", methods=["POST"])
def extender_post():
    d = request.json or {}
    update_node("extender", d.get("signal", 0))
    return jsonify({"status": "ok"})


@app.route("/fused")
def get_fused():
    return jsonify({**fused, "nodes": nodes, "meta": NODE_META})


@app.route("/set_threshold/<float:val>")
def set_threshold(val):
    global THRESHOLD
    THRESHOLD = val
    return jsonify({"threshold": THRESHOLD})


@app.route("/phone.html")
def phone_page():
    return send_from_directory('static', 'phone.html')


@app.route("/extender.html")
def ext_page():
    return send_from_directory('static', 'extender.html')


@app.route('/<path:path>')
def serve_static(path):
    return send_from_directory('static', path)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    boot_time = time.time()

    threading.Thread(target=laptop_loop, daemon=True).start()
    print("[+] Laptop RSSI loop started (floor 1, primary)")
    print("[+] Phone endpoint ready  -> POST /phone")
    print("[+] Extender endpoint     -> POST /extender")
    print("[+] Fused detection       -> GET  /fused")
    print("[+] Dashboard             -> http://0.0.0.0:5000")
    print("[i] Router (floor 0) logged as reference only - excluded from motion scoring")
    app.run(host="0.0.0.0", port=5000, debug=False)
