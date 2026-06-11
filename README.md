# π HumanSense — WiFi Sensing Observatory

> Turn ordinary WiFi into a spatial intelligence system. Detect people, measure breathing rate, track movement, and monitor rooms — with no cameras or wearables. Just physics.

![Python](https://img.shields.io/badge/Python-3.10+-blue)
![Flask](https://img.shields.io/badge/Flask-3.0-green)
![Three.js](https://img.shields.io/badge/Three.js-r128-orange)
![License](https://img.shields.io/badge/License-MIT-yellow)

## What It Does

Your WiFi router fills every room with radio waves. When people move, breathe, or sit still, they disturb those waves in measurable ways. **HumanSense** captures these disturbances using RSSI (and optionally CSI from ESP32 hardware) and turns them into:

- **Presence detection** — detect people through walls
- **Motion detection** — Z-score anomaly detection with debounce
- **Vital signs** — breathing rate via CSI phase analysis (simulated or real)
- **3D visualization** — full Three.js observatory with skeleton, wireframe spheres, and glass panels

## Quick Start

### 1. Clone & Install

```cmd
git clone https://github.com/NAVEENKCG/Human_Wifi_Detector
cd Human_Wifi_Detector
py -m pip install -r requirements.txt
```

### 2. Run the Server

**RSSI only (default — no extra hardware):**
```cmd
py app.py
```

**With simulated CSI data (for demos/development):**
```cmd
py app.py --simulate
```

Expected terminal output:
```
[+] RSSI monitoring started
[+] CSI simulation mode ACTIVE — generating synthetic subcarrier data
[+] Starting Flask server on port 5000...
```

### 3. Open the Dashboard

Navigate to **http://localhost:5000** in your browser.

## How to Test

### Verify the API

Open a new terminal:
```cmd
curl http://localhost:5000/rssi
```

Expected:
```json
{
  "motion": false,
  "router": "YourSSID",
  "signal": 84,
  "timestamp": 1718012345.6,
  "zscore": 0.21
}
```

If `signal` is `0` and `router` is `"Error"`, verify your WiFi connection:
```cmd
netsh wlan show interfaces
```

### Trigger Real Motion Detection

Requirements:
- Laptop connected via **WiFi** (not ethernet)
- Router **3–6 metres away**
- Walk **between** the router and laptop

What to watch:
- `signal` fluctuates (e.g., 84 → 79 → 72 → 81)
- `zscore` spikes above `2.0`
- `motion` flips to `true`
- Dashboard shows pulsing scan ring + event log entry

### Tune Sensitivity

```cmd
curl http://localhost:5000/set_threshold/1.5
```

Lower threshold = more sensitive = more false positives. Start at `2.0`, lower to `1.5` for quiet environments.

### Stress Test Script

```python
import requests, time

for i in range(60):
    r = requests.get("http://localhost:5000/rssi")
    d = r.json()
    print(f"[{i:02d}s] Signal: {d['signal']}%  Z: {d['zscore']:+.2f}  Motion: {d['motion']}")
    time.sleep(1)
```

Run this while walking around for a clean timestamped log.

### Simulation Mode

Press **Space** in the dashboard or toggle in Settings (⚙) to activate client-side CSI simulation. This renders:
- Breathing BPM (~15 RPM)
- Heart rate data
- Skeleton figure fully lit
- "PRESENT" badge active

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `S` | Toggle settings drawer |
| `L` | Toggle event log |
| `M` | Mute/unmute beep |
| `Space` | Toggle simulation mode |

## API Endpoints

| Endpoint | Method | Response |
|----------|--------|----------|
| `/rssi` | GET | `{ signal, zscore, motion, timestamp, router }` |
| `/csi` | GET | `{ subcarriers, amplitude[], phase[], breathing_bpm, heart_rate_bpm, motion_power }` |
| `/set_threshold/<float>` | GET | Sets Z-score detection threshold |
| `/status` | GET | Server mode, uptime, CSI availability |

## Architecture

```
┌─────────────────┐     ┌──────────────┐     ┌────────────────────┐
│  WiFi Router    │────▶│  netsh/ESP32  │────▶│  Flask Backend     │
│  (Radio Waves)  │     │  (RSSI/CSI)  │     │  (Z-score + DSP)   │
└─────────────────┘     └──────────────┘     └────────┬───────────┘
                                                      │ JSON API
                                              ┌───────▼───────────┐
                                              │  Three.js + HTML  │
                                              │  (3D Observatory) │
                                              └───────────────────┘
```

## Upgrade Path

| Stage | What | Cost |
|-------|------|------|
| **Now** | RSSI motion + simulated CSI | ₹0 |
| **ESP32-S3** | Real CSI subcarrier data | ~₹750 |
| **ML model** | Isolation Forest anomaly detection | ₹0 |
| **MQTT** | Real-time push instead of polling | ₹0 |

## License

MIT — NAVEENKCG
