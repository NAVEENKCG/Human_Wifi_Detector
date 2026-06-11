import subprocess
import re
import time
import json
import math
import argparse
import threading
from collections import deque
from flask import Flask, jsonify, send_from_directory
from flask_cors import CORS

# ---------------------------------------------------------------------------
# CLI Arguments
# ---------------------------------------------------------------------------
parser = argparse.ArgumentParser(description="Wi-Fi RSSI / CSI Motion Detector")
parser.add_argument("--simulate", action="store_true",
                    help="Enable simulated CSI data (no ESP32 needed)")
parser.add_argument("--port", type=int, default=5000,
                    help="Flask server port (default: 5000)")
args = parser.parse_args()

SIMULATE_CSI = args.simulate

app = Flask(__name__, static_folder='static')
CORS(app)

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
rssi_state = {
    "signal": 0,
    "zscore": 0.0,
    "motion": False,
    "timestamp": time.time(),
    "router": "Unknown"
}

csi_state = {
    "subcarriers": 56,
    "amplitude": [0.0] * 56,
    "phase": [0.0] * 56,
    "breathing_bpm": 0.0,
    "heart_rate_bpm": 0.0,
    "motion_power": 0.0,
    "timestamp": time.time()
}

# Configuration
WINDOW_SIZE = 10
Z_SCORE_THRESHOLD = 2.0
REQUIRED_CONSECUTIVE = 2
current_threshold = Z_SCORE_THRESHOLD

# Breathing extraction buffers
phase_buffer = deque(maxlen=300)       # ~5 min at 1 Hz
phase_hr_buffer = deque(maxlen=600)    # higher-rate for heart rate

# ---------------------------------------------------------------------------
# Pure-Python signal helpers (no numpy/scipy dependency)
# ---------------------------------------------------------------------------

def _mean(arr):
    return sum(arr) / len(arr) if arr else 0.0


def _std(arr):
    if len(arr) < 2:
        return 0.0
    m = _mean(arr)
    return math.sqrt(sum((x - m) ** 2 for x in arr) / len(arr))


def _bandpass_simple(signal_data, low_hz, high_hz, fs):
    """Very simple frequency-domain bandpass filter (no scipy needed).
    Works by FFT → zero-out bins outside passband → inverse FFT.
    Uses Python's built-in complex math."""
    n = len(signal_data)
    if n < 4:
        return signal_data

    # Remove DC
    m = _mean(signal_data)
    centered = [x - m for x in signal_data]

    # Manual DFT (acceptable for n ≤ 600)
    freqs = []
    for k in range(n):
        re_sum = 0.0
        im_sum = 0.0
        for t_idx, val in enumerate(centered):
            angle = -2 * math.pi * k * t_idx / n
            re_sum += val * math.cos(angle)
            im_sum += val * math.sin(angle)
        freqs.append(complex(re_sum, im_sum))

    # Zero bins outside passband
    for k in range(n):
        freq_hz = k * fs / n if k <= n // 2 else (n - k) * fs / n
        if freq_hz < low_hz or freq_hz > high_hz:
            freqs[k] = 0 + 0j

    # Inverse DFT
    result = []
    for t_idx in range(n):
        re_sum = 0.0
        for k in range(n):
            angle = 2 * math.pi * k * t_idx / n
            re_sum += freqs[k].real * math.cos(angle) - freqs[k].imag * math.sin(angle)
        result.append(re_sum / n)

    return result


def _count_zero_crossings(signal_data):
    crossings = 0
    for i in range(1, len(signal_data)):
        if (signal_data[i - 1] >= 0 and signal_data[i] < 0) or \
           (signal_data[i - 1] < 0 and signal_data[i] >= 0):
            crossings += 1
    return crossings


def extract_breathing_rate(phase_history, fs=1.0):
    """Bandpass 0.1–0.5 Hz → breathing rate in BPM."""
    if len(phase_history) < 30:
        return 0.0
    data = list(phase_history)[-60:]  # Last 60 samples max for speed
    filtered = _bandpass_simple(data, 0.1, 0.5, fs)
    crossings = _count_zero_crossings(filtered)
    duration_sec = len(data) / fs
    bpm = (crossings / 2) / (duration_sec / 60)
    return round(max(0, min(bpm, 30)), 1)


def extract_heart_rate(phase_history, fs=10.0):
    """Bandpass 0.8–2.0 Hz → heart rate in BPM. Needs ≥10 Hz sampling."""
    if len(phase_history) < 50:
        return 0.0
    data = list(phase_history)[-100:]  # Last 100 samples
    filtered = _bandpass_simple(data, 0.8, 2.0, fs)
    crossings = _count_zero_crossings(filtered)
    duration_sec = len(data) / fs
    if duration_sec == 0:
        return 0.0
    bpm = (crossings / 2) / (duration_sec / 60)
    return round(max(0, min(bpm, 200)), 1)


# ---------------------------------------------------------------------------
# Wi-Fi RSSI reader (Windows netsh)
# ---------------------------------------------------------------------------

def get_wifi_stats():
    try:
        result = subprocess.check_output(
            "netsh wlan show interfaces", shell=True, text=True
        )
        signal_match = re.search(r"Signal\s*:\s*(\d+)%", result)
        ssid_match = re.search(r"SSID\s*:\s*(.*)", result)

        signal = int(signal_match.group(1)) if signal_match else 0
        ssid = ssid_match.group(1).strip() if ssid_match else "Unknown"

        return signal, ssid
    except Exception as e:
        print(f"Error reading Wi-Fi stats: {e}")
        return 0, "Error"


# ---------------------------------------------------------------------------
# Simulated CSI generator (no hardware needed)
# ---------------------------------------------------------------------------

import random

def get_simulated_csi():
    """Simulate 56 subcarrier amplitudes + phases like ESP32-S3 output."""
    t = time.time()
    amplitude = []
    phase = []
    for i in range(56):
        # Base amplitude with per-subcarrier variation
        base = 50 + 5 * math.sin(2 * math.pi * i / 56)
        # Inject breathing signal at ~0.25 Hz (15 BPM)
        breathing = 3 * math.sin(2 * math.pi * 0.25 * t + i * 0.1)
        # Small heart rate component at ~1.2 Hz (72 BPM)
        heartbeat = 0.8 * math.sin(2 * math.pi * 1.2 * t + i * 0.05)
        # Noise
        noise = random.gauss(0, 1.5)
        amplitude.append(round(abs(base + breathing + heartbeat + noise), 2))
        phase.append(round(random.uniform(-math.pi, math.pi), 4))
    return amplitude, phase


# ---------------------------------------------------------------------------
# Background monitoring threads
# ---------------------------------------------------------------------------

def monitor_wifi():
    """RSSI monitoring loop — runs continuously."""
    global rssi_state
    window = deque(maxlen=WINDOW_SIZE)
    consecutive_anomalies = 0

    while True:
        signal, ssid = get_wifi_stats()
        timestamp = time.time()

        window.append(signal)
        z_score = 0.0
        motion_detected = False

        if len(window) == WINDOW_SIZE:
            mean = sum(window) / WINDOW_SIZE
            stdev = (sum((x - mean) ** 2 for x in window) / WINDOW_SIZE) ** 0.5

            if stdev > 0:
                z_score = (signal - mean) / stdev
            else:
                z_score = 0.0

            if abs(z_score) > current_threshold:
                consecutive_anomalies += 1
                if consecutive_anomalies >= REQUIRED_CONSECUTIVE:
                    motion_detected = True
                    consecutive_anomalies = 0
            else:
                consecutive_anomalies = 0

        rssi_state = {
            "signal": signal,
            "zscore": round(z_score, 2),
            "motion": motion_detected,
            "timestamp": timestamp,
            "router": ssid
        }

        time.sleep(1)


def monitor_csi():
    """CSI monitoring loop — runs when --simulate is enabled."""
    global csi_state

    while True:
        amplitude, phase = get_simulated_csi()

        # Use mean phase of middle subcarriers (20–36) for vital sign extraction
        mean_phase = _mean(phase[20:36])
        phase_buffer.append(mean_phase)

        breathing_bpm = extract_breathing_rate(phase_buffer, fs=1.0)

        # Motion power: std dev of amplitude across subcarriers
        motion_power = round(_std(amplitude), 2)

        csi_state = {
            "subcarriers": 56,
            "amplitude": amplitude,
            "phase": phase,
            "breathing_bpm": breathing_bpm,
            "heart_rate_bpm": 0.0,  # Needs >1 Hz sampling for accuracy
            "motion_power": motion_power,
            "timestamp": time.time()
        }

        time.sleep(1)


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory('static', 'index.html')


@app.route("/rssi")
def rssi():
    return jsonify(rssi_state)


@app.route("/csi")
def csi():
    return jsonify(csi_state)


@app.route("/set_threshold/<float:val>")
def set_threshold(val):
    global current_threshold
    current_threshold = val
    return jsonify({"status": "success", "threshold": current_threshold})


@app.route("/status")
def status():
    return jsonify({
        "mode": "simulate" if SIMULATE_CSI else "rssi",
        "csi_available": SIMULATE_CSI,
        "threshold": current_threshold,
        "uptime": round(time.time() - boot_time, 1)
    })


@app.route('/<path:path>')
def serve_static(path):
    return send_from_directory('static', path)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    boot_time = time.time()

    # Always start RSSI monitoring
    rssi_thread = threading.Thread(target=monitor_wifi, daemon=True)
    rssi_thread.start()
    print("[+] RSSI monitoring started")

    # Start CSI simulation if flag is set
    if SIMULATE_CSI:
        csi_thread = threading.Thread(target=monitor_csi, daemon=True)
        csi_thread.start()
        print("[+] CSI simulation mode ACTIVE — generating synthetic subcarrier data")
    else:
        print("[i] CSI simulation OFF — run with --simulate to enable")

    print(f"[+] Starting Flask server on port {args.port}...")
    app.run(host="0.0.0.0", port=args.port, debug=False)
