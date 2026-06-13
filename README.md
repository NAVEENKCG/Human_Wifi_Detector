# Human WiFi Detector — 2 Floor Mesh

WiFi-based human presence detection using a **3-node sensing mesh** on the first floor, with a ground-floor router as a reference baseline.

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

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| GET | `/fused` | Fused detection with all node data |
| GET | `/rssi` | Laptop RSSI reading |
| POST | `/phone` | Phone sends `{ signal: N }` |
| POST | `/extender` | Extender sends `{ signal: N }` |
| GET | `/set_threshold/<float>` | Adjust Z-score threshold |

### `/fused` response format

```json
{
  "motion": true,
  "confidence": 0.67,
  "source": "laptop+phone",
  "floor1_active": 3,
  "timestamp": 1718000000.0,
  "nodes": {
    "laptop": { "signal": 78, "zscore": -2.4, "motion": true, "last_seen": 1718000000.0 },
    "phone": { "signal": 65, "zscore": -1.8, "motion": true, "last_seen": 1718000000.0 },
    "extender": { "signal": 71, "zscore": 0.3, "motion": false, "last_seen": 1718000000.0 }
  },
  "meta": {
    "laptop": { "floor": 1, "role": "primary" },
    "phone": { "floor": 1, "role": "primary" },
    "extender": { "floor": 1, "role": "primary" },
    "router": { "floor": 0, "role": "reference" }
  }
}
```

## Confidence Scoring

Only **first-floor nodes** contribute to motion confidence:

| Active Detectors | Confidence | Label |
|---|---|---|
| 1 of 3 | 0.33 | Single node |
| 2 of 3 | 0.67 | Two nodes |
| 3 of 3 | 1.00 | All nodes |

Router is excluded from scoring.

## Dashboard Features

- **SVG floor map** — two-floor layout with node positions and link status
- **Three node cards** — circular gauges, Z-score, online/offline status
- **Fused confidence bar** — color-coded with ripple effect on motion
- **Chart.js signal history** — 60-reading three-line chart
- **Event log** — with CSV export
- **Simulation mode** — client-side signal generation with periodic motion bursts
- **Web Audio beeps** — 330Hz (single), 440Hz (two), triple beep (all nodes)

## Placement Tips

- Place phone and extender at **opposite ends** of the first floor
- The wider the triangle, the larger the coverage area
- A person walking anywhere crosses at least one link path
- Keep laptop centrally positioned for best overall coverage

## Files

```
├── app.py                  # Flask server, floor-aware 3-node detection
├── requirements.txt        # flask, flask-cors
├── static/
│   ├── index.html          # Main dashboard
│   ├── phone.html          # Phone node (amber)
│   └── extender.html       # Extender node (teal)
└── README.md
```
