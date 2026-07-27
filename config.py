"""
Sens8 — WiFi Sensing Configuration
All tunables in one place. Override via CLI args in main.py.
"""

# ─── Network Interface ────────────────────────────────────────────────
INTERFACE = "wlan0"                 # auto-detected at startup if not set
INTERFACE_AUTO_DETECT = True        # attempt auto-detection via `iw dev`

# ─── RuView Integration ──────────────────────────────────────────────
RUVIEW_HOST = "127.0.0.1"
RUVIEW_UDP_PORT = 5005
RUVIEW_WS_URL = "ws://localhost:3001/ws/sensing"
RUVIEW_HTTP_URL = "http://localhost:3000"

# ─── Capture Settings ────────────────────────────────────────────────
SAMPLE_RATE = 10                    # target Hz for CSI frame output
RSSI_WINDOW_SIZE = 500              # sliding window samples per MAC
CHANNEL_HOP = True                  # hop channels for broader visibility
CHANNEL_HOP_INTERVAL = 0.5         # seconds per channel
CHANNELS_24GHZ = [1, 6, 11]        # 2.4 GHz non-overlapping channels
CHANNELS_5GHZ = [36, 40, 44, 48]   # 5 GHz channels (if card supports)

# ─── Baseline Calibration ────────────────────────────────────────────
BASELINE_DURATION = 30              # seconds of initial calibration
BASELINE_UPDATE_INTERVAL = 60      # seconds between EMA baseline updates
BASELINE_EMA_ALPHA = 0.1           # exponential moving average smoothing

# ─── Motion Detection ────────────────────────────────────────────────
MOTION_WINDOW = 5                   # seconds for variance window
MOTION_SCORE_SMOOTHING = 0.3       # EMA alpha for motion score

# ─── Presence Detection ──────────────────────────────────────────────
PRESENCE_THRESHOLD = 0.35          # variance threshold for presence
PRESENCE_HYSTERESIS = 3            # consecutive frames before state change
PRESENCE_MAX_CONFIDENCE = 0.75     # cap — honest about RSSI limitations

# ─── Person Counting (DBSCAN) ────────────────────────────────────────
DBSCAN_EPS = 0.3                   # clustering sensitivity
DBSCAN_MIN_SAMPLES = 2            # min APs to form a cluster
COUNT_HYSTERESIS = 3               # frames before count changes
COUNT_UPDATE_INTERVAL = 2          # seconds between count updates
MIN_APS_FOR_COUNTING = 3          # need at least 3 APs for meaningful clusters

# ─── Vitals Estimation ───────────────────────────────────────────────
BREATHING_CONFIDENCE_MIN = 0.4     # don't report below this
BREATHING_BAND_LOW = 0.1           # Hz  (6 BPM)
BREATHING_BAND_HIGH = 0.5          # Hz  (30 BPM)
VITALS_WINDOW_SECONDS = 30        # seconds of data for vitals analysis

# ─── Synthetic CSI ────────────────────────────────────────────────────
CSI_SUBCARRIERS = 56               # number of synthetic subcarriers
CSI_FRAME_RATE = 10                # Hz — match RuView expectation
CSI_SOURCE_TAG = "software-rssi"   # tag to distinguish from real CSI

# ─── Dashboard ────────────────────────────────────────────────────────
DASHBOARD_REFRESH_RATE = 2         # Hz
DASHBOARD_RSSI_BARS = True         # show signal strength bars

# ─── Logging ──────────────────────────────────────────────────────────
LOG_LEVEL = "INFO"
LOG_FILE = None                    # set to path to enable file logging
