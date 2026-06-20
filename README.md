# Human WiFi Detector — Industrial Tuned 2-Floor Mesh

WiFi-based human presence detection using a **3-node sensing mesh** on the first floor, with a ground-floor router as a reference baseline.

## 🔬 Industrial Tuning Sources

Parameters calibrated from industrial WiFi sensing research and open-source projects:

| Parameter | Value | Source |
|---|---|---|
| CUSUM Drift | 0.3 | IEEE CUSUM control chart standard |
| CUSUM Threshold | 4.0 | arXiv WiFi sensing papers (change-point detection) |
| EMA Alpha | 0.18 | Research consensus (0.15-0.22 optimal for RSSI) |
| Hampel K | 4 | Hampel filter research (wider window = better spike rejection) |
| Hampel T | 2.5 | MAD-based outlier detection (2.0-3.0 range) |
| Path Loss n | 3.0 | Log-Distance indoor residential (IEEE, NXP App Notes) |
| Reference Power | -40 dBm | 2.4GHz indoor at 1m reference distance |
| MAD Scale | 1.4826 | Gaussian assumption (MAD → σ conversion) |
| Warmup | 20 samples | IEEE recommended calibration length |
| Rolling Window | 30 | Robust statistics standard |

### Reference Projects
- **[RuView](https://github.com/ruvnet/RuView)** — WiFi spatial intelligence, vital sign monitoring, presence detection
- **[ESP-CSI](https://github.com/espressif/esp-csi)** — Espressif official CSI sensing framework
- **[IEEE 802.11bf](https://www.ieee802.org/)** — WLAN Sensing standard (in development)
- **Hampel Filter Research** — [hampel Python library](https://github.com/MichaelisTrofficus/hampel_filter)
- **Wireless-Sensing-Tutorial** — [GitHub](https://github.com/Guoxuan-Chi/Wireless-Sensing-Tutorial)

## Architecture

```
Ground Floor:  Router (192.168.1.1) — reference only
                     │ (vertical through slab — low motion sensitivity)
─────────────────────┼──────────────────────────────────
First Floor:    Extender ──── Laptop ──── Phone
                   (teal)     (blue)      (amber)
                     └─────────────────────┘
                     Horizontal links = HIGH sensitivity
```

### Why this topology works

The router-to-first-floor signal travels **vertically through a concrete slab** — a person walking on the first floor barely crosses this beam. It's almost useless for motion detection.

The **lateral links on the first floor** (extender ↔ laptop ↔ phone) are horizontal. A person walking between rooms crosses these paths repeatedly. This triangle geometry gives excellent coverage.

| Link | Path | Motion Sensitivity |
|---|---|---|
| Router → Laptop | Vertical through slab | Low |
| Extender ↔ Laptop | Horizontal, 1st floor | **High** |
| Laptop ↔ Phone | Horizontal, 1st floor | **High** |
| Extender ↔ Phone | Horizontal, 1st floor | **High** |

## Quick Start

```bash
pip install flask flask-cors
python app.py
```

Then open:
- **Dashboard**: `http://localhost:5000` (on laptop)
- **Phone node**: `http://<laptop-ip>:5000/phone.html` (on phone browser)
- **Extender node**: `http://<laptop-ip>:5000/extender.html` (on extender-connected device)

## Signal Processing Pipeline

The detection pipeline follows industry best practices:

```
Raw RSSI → Hampel Filter (K=4, T=2.5) → EMA Smoothing (α=0.18)
         → Z-Score (window=30)
         → Delta Z-Score (rate-of-change, window=12)
         → CUSUM Detector (drift=0.3, threshold=4.0)
         → Cross-Node Correlation (2.5s window)
         → Anti-Phantom Filter (2.0s sustain)
         → Fused State Machine
```

### Distance Estimation
Uses **Log-Distance Path Loss Model** (indoor residential):
```
d = d₀ × 10^((P₀ - Pᵣ) / (10 × n))
```
Where: P₀ = -40 dBm (reference at 1m), n = 3.0 (indoor residential)

### Adaptive Noise Floor
Live **Median Absolute Deviation (MAD)** based noise tracking:
- Adapts to changing environments (doors opening, appliances turning on)
- More robust than static baseline from calibration

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| GET | `/fused` | Fused detection with all node data + tuning params |
| GET | `/rssi` | Laptop RSSI reading + channel/band info |
| POST | `/phone` | Phone sends `{ signal: N }` |
| POST | `/extender` | Extender sends `{ signal: N }` |
| GET | `/set_threshold/<float>` | Adjust Z-score threshold |

### `/fused` response format

```json
{
  "motion": true,
  "confidence": 0.67,
  "source": "laptop+phone",
  "active_nodes": 3,
  "timestamp": 1718000000.0,
  "nodes": {
    "laptop": { "signal": 78, "zscore": -2.4, "delta_zscore": -1.2, "noise_floor": 0.8, "motion": true },
    "phone": { "signal": 65, "zscore": -1.8, "delta_zscore": -0.5, "motion": true },
    "extender": { "signal": 71, "zscore": 0.3, "delta_zscore": 0.1, "motion": false }
  },
  "tuning": {
    "cusum_drift": 0.3,
    "cusum_threshold": 4.0,
    "ema_alpha": 0.18,
    "pl_exponent": 3.0,
    "pl_ref_power": -40,
    "hampel_k": 4,
    "hampel_t": 2.5,
    "warmup_samples": 20,
    "channel": 6,
    "band": "802.11n"
  },
  "positions": { ... }
}
```

## Confidence Scoring

Only **first-floor nodes** contribute to motion confidence:

| Active Detectors | Confidence | Label |
|---|---|---|
| 1 of 3 | 0.25-0.30 | Single node (penalized) |
| 2 of 3 | 0.55-0.75 | Two nodes (correlated) |
| 3 of 3 | 1.00 | All nodes |

Router is excluded from scoring. Single-node events receive a 0.45x penalty.

## Dashboard Features

- **SVG radar** — laptop POV with sweep beam and blip detection
- **Tuning parameters panel** — live display of industrial CUSUM/Hampel/EMA/PL values
- **Three node cards** — Z-score + Delta Z-score per node
- **Fused confidence bar** — color-coded with ripple effect on motion
- **Chart.js signal history** — 60-reading three-line chart
- **Channel/band info** — shows WiFi channel and radio type in HUD
- **Simulation mode** — client-side signal generation with periodic motion bursts
- **Web Audio beeps** — 330Hz (single), 440Hz (two), triple beep (all nodes)

## Files

```
├── app.py                  # Flask server, industrial-tuned detection engine
├── requirements.txt        # flask, flask-cors
├── static/
│   ├── index.html          # Main radar dashboard with tuning panel
│   ├── phone.html          # Phone node (amber)
│   └── extender.html       # Extender node (teal)
└── README.md
```
