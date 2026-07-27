# Sens8 — WiFi Sensing Daemon

<p align="center">
  <img src="https://img.shields.io/badge/Platform-Kali_Linux-557C94?style=for-the-badge&logo=kalilinux&logoColor=white" />
  <img src="https://img.shields.io/badge/Python-3.10+-3776AB?style=for-the-badge&logo=python&logoColor=white" />
  <img src="https://img.shields.io/badge/Mode-Monitor_Mode-00C853?style=for-the-badge&logo=wifi&logoColor=white" />
  <img src="https://img.shields.io/badge/Hardware-None_Required-FF6D00?style=for-the-badge" />
</p>

<p align="center">
  <b>Pure software WiFi sensing — detect presence, motion, occupancy, and person count using only ambient WiFi signals.</b>
  <br/>
  <em>No ESP32, no CSI hardware, no extra equipment. Just your existing WiFi card.</em>
</p>

---

## What It Does

Sens8 is a Python daemon that turns your WiFi card into a passive sensing system by analyzing **RSSI (Received Signal Strength Indicator)** variations from nearby access points. When people move through a space, they disturb WiFi signals — Sens8 detects these disturbances.

### Capabilities

| Feature | Method | Confidence |
|---------|--------|------------|
| **Presence Detection** | RSSI variance above baseline threshold | Medium (capped at 75%) |
| **Motion Detection** | RSSI delta variance over 5s sliding window | Medium |
| **Person Count** | DBSCAN clustering of per-AP variance vectors | High (1 person) → Low (3+) |
| **Zone Detection** | Signal-strength-based AP grouping | Low-Medium |
| **Breathing Rate** | Bandpass filter (0.1–0.5 Hz) on stable RSSI | Very Low (best-effort) |

### How It Works

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  WiFi Card      │────▸│  Packet Capture  │────▸│  RSSI Tracking  │
│  (Monitor Mode) │     │  (Scapy)         │     │  (Per-MAC)      │
└─────────────────┘     └──────────────────┘     └────────┬────────┘
                                                          │
                    ┌─────────────────────────────────────┘
                    ▼
┌──────────────────────────────────────────────────────────────────┐
│                    Signal Processing Pipeline                     │
│                                                                    │
│  ┌────────────┐  ┌────────────┐  ┌──────────┐  ┌──────────────┐ │
│  │ Baseline   │  │ Variance   │  │ Presence │  │ Person Count │ │
│  │ Calibration│─▸│ Analysis   │─▸│ Detector │  │ (DBSCAN)     │ │
│  │ (30s EMA)  │  │ (5s window)│  │ (w/hyst) │  │              │ │
│  └────────────┘  └────────────┘  └──────────┘  └──────────────┘ │
└──────────────────────────────────────────────────────────────────┘
                    │                      │
          ┌────────┘                      └────────┐
          ▼                                        ▼
┌──────────────────┐                    ┌──────────────────┐
│ Synthetic CSI    │                    │ Rich Terminal    │
│ Frame Generator  │                    │ Dashboard        │
│ → UDP 5005       │                    │ (Live UI)        │
└──────────────────┘                    └──────────────────┘
          │
          ▼
┌──────────────────┐
│ RuView Dashboard │
│ localhost:3000   │
└──────────────────┘
```

---

## ⚠️ Honest Limitations vs Real CSI

> **This is NOT a replacement for ESP32 CSI hardware.**

| Aspect | Sens8 (RSSI) | ESP32 CSI |
|--------|-------------|-----------|
| **Signal Data** | Single RSSI value per packet | 56+ subcarrier amplitudes & phases |
| **Spatial Resolution** | Low (room-level) | High (sub-meter) |
| **Person Count** | Estimate, degrades above 2 | Accurate up to 5+ |
| **Breathing Detection** | Best-effort, very low confidence | Research-grade accuracy |
| **Presence Detection** | Variance-based, 75% max confidence | Phase-based, >95% accuracy |
| **Update Rate** | ~10 Hz (packet dependent) | Consistent 100+ Hz |
| **Extra Hardware** | None — uses existing WiFi | Requires ESP32 + companion AP |

All detections are labeled as **"RSSI-based estimate"** and confidence is honestly capped. Person count above 2 people is labeled as low confidence. Breathing rate is labeled as **"estimated — low confidence without CSI hardware"**.

---

## Quick Start

### Prerequisites

- **Kali Linux** (or any Linux with `iw` and monitor-mode-capable WiFi card)
- **Python 3.10+**
- **Root privileges** (for monitor mode + raw packet capture)
- **WiFi card** that supports monitor mode (most Atheros/Realtek/Intel cards)

### Install & Run

```bash
# Clone the repository
git clone https://github.com/Char0n1507/Sens8.git
cd Sens8

# One-command install + run
sudo bash install.sh --run

# Or install separately and run manually
sudo bash install.sh
sudo python3 main.py
```

### Manual Setup

```bash
# Install Python dependencies
pip3 install -r requirements.txt

# Run with auto-detected interface
sudo python3 main.py

# Specify interface
sudo python3 main.py -i wlan0

# Headless mode (no dashboard)
sudo python3 main.py --no-dashboard

# Skip RuView integration
sudo python3 main.py --no-ruview

# Longer calibration for better baseline
sudo python3 main.py --baseline-duration 60

# Debug logging
sudo python3 main.py --log-level DEBUG
```

---

## RuView Integration

Sens8 integrates with the [RuView/wifi-densepose](https://github.com/RuView/wifi-densepose) server:

### Setup

1. **RuView should already be running:**
   ```bash
   # Docker container on localhost:3000
   # UDP port 5005 for CSI frames
   # WebSocket ws://localhost:3001/ws/sensing
   ```

2. **Start Sens8:**
   ```bash
   sudo python3 main.py
   ```

3. **Open RuView dashboard:**
   ```
   http://localhost:3000
   ```

Sens8 sends synthetic CSI-like frames on UDP 5005 and sensing data on the WebSocket. All frames are tagged with `source=software-rssi` so RuView can distinguish them from real ESP32 CSI data.

### Custom RuView Host

```bash
sudo python3 main.py --ruview-host 192.168.1.100 --ruview-port 5005
```

---

## Project Structure

```
wifi-sense/
├── main.py                  # Entry point, CLI args, orchestration
├── capture/
│   ├── monitor.py           # Monitor mode management (iw, ip link)
│   ├── scanner.py           # Scapy packet capture, RSSI extraction
│   └── devices.py           # Device/AP tracking, MAC management
├── processing/
│   ├── baseline.py          # Ambient baseline calibration (30s EMA)
│   ├── variance.py          # RSSI variance analysis, motion scoring
│   ├── presence.py          # Presence inference with hysteresis
│   ├── counter.py           # Person count via DBSCAN clustering
│   └── vitals.py            # Breathing rate estimation (best-effort)
├── synthetic/
│   ├── csi_frame.py         # Synthesize CSI-like UDP frames from RSSI
│   └── udp_sender.py        # Send frames to RuView UDP 5005
├── output/
│   ├── dashboard.py         # Rich terminal dashboard
│   └── websocket.py         # Mirror data to RuView WebSocket
├── config.py                # All configuration and thresholds
├── requirements.txt         # Python dependencies
├── install.sh               # Auto-setup script for Kali Linux
└── README.md
```

---

## Configuration

All tunables are in `config.py`. Key settings:

| Setting | Default | Description |
|---------|---------|-------------|
| `INTERFACE` | `wlan0` | WiFi interface (auto-detected) |
| `BASELINE_DURATION` | `30` | Calibration duration (seconds) |
| `MOTION_WINDOW` | `5` | Motion analysis window (seconds) |
| `PRESENCE_THRESHOLD` | `0.35` | Variance threshold for presence |
| `PRESENCE_HYSTERESIS` | `3` | Frames before state change |
| `PRESENCE_MAX_CONFIDENCE` | `0.75` | Honest confidence cap |
| `DBSCAN_EPS` | `0.3` | Clustering sensitivity |
| `COUNT_HYSTERESIS` | `3` | Frames before count change |
| `CHANNEL_HOP` | `True` | Enable channel hopping |
| `CSI_FRAME_RATE` | `10` | Synthetic frame rate (Hz) |

---

## Dashboard

The terminal dashboard (powered by [Rich](https://github.com/Textualize/rich)) displays:

- **Header**: Interface, chipset, mode, uptime
- **Calibration**: Progress bar during startup
- **Person Count**: Large display with confidence (0/1/2/3+ people)
- **Presence**: YES/NO indicator with confidence %
- **Motion Score**: Live animated bar with threshold marker
- **RSSI Heatmap**: Table of all visible APs with signal bars
- **Zone Map**: APs grouped by signal proximity
- **Breathing Rate**: Estimated BPM (when confidence sufficient)
- **RuView Status**: UDP connection state and frame count

---

## How Person Counting Works

1. **Feature Extraction**: For each visible AP, compute a variance vector:
   `[mean_delta, std_delta, peak_delta, zero_crossings]`

2. **Standardization**: Normalize features across APs

3. **DBSCAN Clustering**: Group APs with similar disturbance patterns.
   Each person creates a distinct multipath disturbance pattern.

4. **Count = Active Clusters**: Clusters above motion threshold ≈ distinct persons

5. **Hysteresis**: Require 3 consecutive same-count frames before reporting

> **Note**: Requires 3+ visible APs for meaningful clustering. Accuracy degrades significantly above 2 people without real CSI hardware.

---

## Supported WiFi Cards

Any card that supports monitor mode. Common compatible chipsets:

| Chipset | Driver | Monitor Mode |
|---------|--------|:------------:|
| Atheros AR9271 | ath9k_htc | ✅ |
| Ralink RT3070 | rt2800usb | ✅ |
| Realtek RTL8812AU | 88XXau | ✅ |
| Intel AX200/AX210 | iwlwifi | ✅ |
| Broadcom BCM43xx | brcmfmac | ⚠️ Limited |

If your card doesn't support monitor mode, Sens8 falls back to managed-mode scanning using `iwlist scan` (reduced capability).

---

## License

MIT

---

<p align="center">
  <em>Built for the cybersecurity community. Use responsibly.</em>
  <br/>
  <strong>Sens8</strong> — See without being seen.
</p>
