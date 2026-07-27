"""
Sens8 — Device & AP Tracking
Maintains per-MAC RSSI time series with sliding windows,
classifies MACs as AP vs client, and provides aggregate views.
"""

import time
import threading
import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

import config

logger = logging.getLogger("sens8.capture.devices")


@dataclass
class DeviceRecord:
    """Tracked device or AP."""
    mac: str
    ssid: str = ""
    is_ap: bool = False
    first_seen: float = 0.0
    last_seen: float = 0.0
    channel: int = 0
    frame_types: set = field(default_factory=set)

    # RSSI time series: deque of (timestamp, rssi) tuples
    rssi_history: deque = field(
        default_factory=lambda: deque(maxlen=config.RSSI_WINDOW_SIZE)
    )

    def add_rssi(self, rssi: int, timestamp: float, min_interval: float = 0.6):
        """Append an RSSI sample, filtering out static cache duplicates."""
        self.last_seen = timestamp
        if self.first_seen == 0:
            self.first_seen = timestamp

        if self.rssi_history:
            last_t, last_r = self.rssi_history[-1]
            # Ignore static identical duplicates added faster than min_interval
            if last_r == rssi and (timestamp - last_t) < min_interval:
                return

        self.rssi_history.append((timestamp, rssi))

    @property
    def latest_rssi(self) -> Optional[int]:
        if self.rssi_history:
            return self.rssi_history[-1][1]
        return None

    @property
    def sample_count(self) -> int:
        return len(self.rssi_history)

    def get_rssi_array(self, seconds: Optional[float] = None) -> np.ndarray:
        """Get RSSI values as numpy array, optionally only last N seconds."""
        if not self.rssi_history:
            return np.array([], dtype=np.float64)

        if seconds is None:
            return np.array([r for _, r in self.rssi_history], dtype=np.float64)

        cutoff = time.time() - seconds
        return np.array(
            [r for t, r in self.rssi_history if t >= cutoff],
            dtype=np.float64
        )

    def get_timestamps(self, seconds: Optional[float] = None) -> np.ndarray:
        """Get timestamp array."""
        if not self.rssi_history:
            return np.array([], dtype=np.float64)

        if seconds is None:
            return np.array([t for t, _ in self.rssi_history], dtype=np.float64)

        cutoff = time.time() - seconds
        return np.array(
            [t for t, _ in self.rssi_history if t >= cutoff],
            dtype=np.float64
        )

    @property
    def signal_quality(self) -> str:
        """Human-friendly signal quality string."""
        rssi = self.latest_rssi
        if rssi is None:
            return "unknown"
        if rssi >= -30:
            return "excellent"
        elif rssi >= -50:
            return "good"
        elif rssi >= -70:
            return "fair"
        elif rssi >= -85:
            return "weak"
        return "very weak"


class DeviceTracker:
    """
    Thread-safe tracker for all observed WiFi devices and APs.
    Maintains per-MAC RSSI time series and classifies devices.
    """

    def __init__(self):
        self._devices: Dict[str, DeviceRecord] = {}
        self._lock = threading.RLock()
        self._ap_macs: set = set()
        self._client_macs: set = set()
        self._total_updates = 0

    def update(self, datum: dict):
        """
        Update tracker with a captured packet datum.
        datum = {mac, ssid, rssi, timestamp, channel, frame_type}
        """
        mac = datum["mac"]
        rssi = datum["rssi"]
        ts = datum["timestamp"]

        with self._lock:
            self._total_updates += 1

            if mac not in self._devices:
                self._devices[mac] = DeviceRecord(mac=mac)

            dev = self._devices[mac]
            dev.add_rssi(rssi, ts)

            if datum.get("ssid"):
                dev.ssid = datum["ssid"]
            if datum.get("channel"):
                dev.channel = datum["channel"]
            if datum.get("frame_type"):
                dev.frame_types.add(datum["frame_type"])

            # Classify: if it sends beacons or probe responses, it's an AP
            if datum.get("frame_type") in ("beacon", "probe_resp"):
                dev.is_ap = True
                self._ap_macs.add(mac)
            elif datum.get("frame_type") == "probe_req":
                # Probe requests come from clients
                if not dev.is_ap:
                    self._client_macs.add(mac)

    def get_aps(self, min_samples: int = 3) -> List[DeviceRecord]:
        """Get all tracked APs with at least min_samples readings."""
        with self._lock:
            return [
                d for d in self._devices.values()
                if d.is_ap and d.sample_count >= min_samples
            ]

    def get_clients(self) -> List[DeviceRecord]:
        """Get all tracked client devices."""
        with self._lock:
            return [
                d for d in self._devices.values()
                if not d.is_ap
            ]

    def get_all_devices(self) -> List[DeviceRecord]:
        """Get all tracked devices."""
        with self._lock:
            return list(self._devices.values())

    def get_device(self, mac: str) -> Optional[DeviceRecord]:
        """Get a specific device by MAC."""
        with self._lock:
            return self._devices.get(mac)

    @property
    def ap_count(self) -> int:
        with self._lock:
            return len(self._ap_macs)

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._client_macs)

    @property
    def total_count(self) -> int:
        with self._lock:
            return len(self._devices)

    @property
    def total_updates(self) -> int:
        return self._total_updates

    def get_strongest_aps(self, n: int = 12, max_age: float = 15.0) -> List[DeviceRecord]:
        """Get top N APs by latest RSSI active within max_age seconds."""
        cutoff = time.time() - max_age
        aps = [
            d for d in self.get_aps(min_samples=1)
            if d.last_seen >= cutoff
        ]
        aps.sort(key=lambda d: d.latest_rssi or -999, reverse=True)
        return aps[:n]

    def prune_stale(self, max_age: float = 120.0):
        """Remove devices not seen for max_age seconds."""
        cutoff = time.time() - max_age
        with self._lock:
            stale = [
                mac for mac, dev in self._devices.items()
                if dev.last_seen < cutoff
            ]
            for mac in stale:
                del self._devices[mac]
                self._ap_macs.discard(mac)
                self._client_macs.discard(mac)
            if stale:
                logger.debug(f"Pruned {len(stale)} stale devices")
