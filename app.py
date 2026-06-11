import subprocess
import re
import time
import json
import statistics
import threading
from collections import deque
from flask import Flask, jsonify, send_from_directory
from flask_cors import CORS

app = Flask(__name__, static_folder='static')
CORS(app)

# Global state
rssi_state = {
    "signal": 0,
    "zscore": 0.0,
    "motion": False,
    "timestamp": time.time(),
    "router": "Unknown"
}

# Configuration
WINDOW_SIZE = 10
Z_SCORE_THRESHOLD = 2.0  # Default, can be changed via UI
REQUIRED_CONSECUTIVE = 2

# To store the last threshold requested by UI
current_threshold = Z_SCORE_THRESHOLD

def get_wifi_stats():
    try:
        # Note: 'netsh' is Windows specific
        result = subprocess.check_output("netsh wlan show interfaces", shell=True, text=True)
        signal_match = re.search(r"Signal\s*:\s*(\d+)%", result)
        ssid_match = re.search(r"SSID\s*:\s*(.*)", result)
        
        signal = int(signal_match.group(1)) if signal_match else 0
        ssid = ssid_match.group(1).strip() if ssid_match else "Unknown"
        
        return signal, ssid
    except Exception as e:
        print(f"Error reading Wi-Fi stats: {e}")
        return 0, "Error"

def monitor_wifi():
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
            stdev = (sum((x - mean)**2 for x in window) / WINDOW_SIZE) ** 0.5
            
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
        
        # Poll roughly every 1 second (minus netsh overhead)
        time.sleep(1)

@app.route("/")
def index():
    return send_from_directory('static', 'index.html')

@app.route("/rssi")
def rssi():
    return jsonify(rssi_state)

@app.route("/set_threshold/<float:val>")
def set_threshold(val):
    global current_threshold
    current_threshold = val
    return jsonify({"status": "success", "threshold": current_threshold})

@app.route('/<path:path>')
def serve_static(path):
    return send_from_directory('static', path)

if __name__ == "__main__":
    monitor_thread = threading.Thread(target=monitor_wifi, daemon=True)
    monitor_thread.start()
    print("Starting Flask server on port 5000...")
    app.run(host="0.0.0.0", port=5000, debug=False)
