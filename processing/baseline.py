"""
Sens8 — Ambient Baseline Calibration
Captures 30 seconds of ambient RSSI data on startup to establish
per-AP mean/std baseline. Updates periodically via EMA.
"""

import time
import logging
import threading
from dataclasses import dataclass, field
from typing import Dict, Optional

import numpy as np

from capture.devices import DeviceTracker
import config

logger = logging.getLogger("sens8.processing.baseline")


@dataclass
class APBaseline:
    """Baseline statistics for a single AP."""
    mac: str
    ssid: str = ""
    mean_rssi: float = 0.0
    std_rssi: float = 0.0
    sample_count: int = 0
    last_updated: float = 0.0


class BaselineCalibrator:
    """
    Performs initial ambient baseline calibration and maintains
    a rolling reference via exponential moving average updates.
    """

    def __init__(self, tracker: DeviceTracker):
        self.tracker = tracker
        self._baselines: Dict[str, APBaseline] = {}
        self._calibrated = False
        self._calibration_progress = 0.0  # 0.0 to 1.0
        self._confidence = 0.0
        self._lock = threading.Lock()
        self._empty_room_signature: Optional[Dict[str, float]] = None

    @property
    def is_calibrated(self) -> bool:
        return self._calibrated

    @property
    def progress(self) -> float:
        return self._calibration_progress

    @property
    def confidence(self) -> float:
        return self._confidence

    @property
    def baselines(self) -> Dict[str, APBaseline]:
        with self._lock:
            return dict(self._baselines)

    def calibrate(self, duration: float = None) -> bool:
        """
        Run initial calibration for `duration` seconds.
        Blocks until complete. Returns True if sufficient data collected.
        """
        if duration is None:
            duration = config.BASELINE_DURATION

        logger.info(f"Starting baseline calibration ({duration}s)...")
        start = time.time()
        end = start + duration

        while time.time() < end:
            elapsed = time.time() - start
            self._calibration_progress = min(elapsed / duration, 1.0)
            time.sleep(0.5)

        # Compute baselines from captured data
        self._compute_baselines()

        if len(self._baselines) == 0:
            logger.warning("No APs detected during calibration!")
            self._confidence = 0.0
            self._calibrated = True  # Still mark as done
            return False

        # Store empty-room signature
        self._empty_room_signature = {
            mac: bl.mean_rssi for mac, bl in self._baselines.items()
        }

        # Confidence based on number of APs and samples
        ap_count = len(self._baselines)
        avg_samples = np.mean([bl.sample_count for bl in self._baselines.values()])
        self._confidence = min(
            (ap_count / 5.0) * 0.5 + (avg_samples / 100.0) * 0.5,
            1.0
        )

        self._calibration_progress = 1.0
        self._calibrated = True

        logger.info(
            f"✓ Calibration complete: {ap_count} APs, "
            f"confidence={self._confidence:.2f}"
        )
        return True

    def _compute_baselines(self):
        """Compute per-AP baseline statistics from current tracker data."""
        with self._lock:
            for ap in self.tracker.get_aps(min_samples=3):
                rssi_arr = ap.get_rssi_array()
                if len(rssi_arr) < 3:
                    continue

                bl = APBaseline(
                    mac=ap.mac,
                    ssid=ap.ssid,
                    mean_rssi=float(np.mean(rssi_arr)),
                    std_rssi=float(np.std(rssi_arr)),
                    sample_count=len(rssi_arr),
                    last_updated=time.time(),
                )
                self._baselines[ap.mac] = bl

    def update_ema(self):
        """
        Update baselines using exponential moving average.
        Called periodically (every BASELINE_UPDATE_INTERVAL seconds).
        """
        alpha = config.BASELINE_EMA_ALPHA

        with self._lock:
            for ap in self.tracker.get_aps(min_samples=5):
                recent = ap.get_rssi_array(seconds=config.BASELINE_UPDATE_INTERVAL)
                if len(recent) < 5:
                    continue

                new_mean = float(np.mean(recent))
                new_std = float(np.std(recent))

                if ap.mac in self._baselines:
                    bl = self._baselines[ap.mac]
                    bl.mean_rssi = bl.mean_rssi * (1 - alpha) + new_mean * alpha
                    bl.std_rssi = bl.std_rssi * (1 - alpha) + new_std * alpha
                    bl.sample_count += len(recent)
                    bl.last_updated = time.time()
                    if ap.ssid:
                        bl.ssid = ap.ssid
                else:
                    self._baselines[ap.mac] = APBaseline(
                        mac=ap.mac,
                        ssid=ap.ssid,
                        mean_rssi=new_mean,
                        std_rssi=new_std,
                        sample_count=len(recent),
                        last_updated=time.time(),
                    )

    def get_deviation(self, mac: str, current_rssi: float) -> Optional[float]:
        """
        Get deviation of current RSSI from baseline in std units.
        Returns None if no baseline exists.
        """
        with self._lock:
            bl = self._baselines.get(mac)
            if bl is None or bl.std_rssi < 0.1:
                return None
            return abs(current_rssi - bl.mean_rssi) / bl.std_rssi

    def is_room_occupied(self) -> Optional[bool]:
        """
        Compare current RSSI pattern against empty-room signature.
        Returns None if insufficient data.
        """
        if self._empty_room_signature is None:
            return None

        deviations = []
        for mac, baseline_rssi in self._empty_room_signature.items():
            device = self.tracker.get_device(mac)
            if device and device.latest_rssi is not None:
                dev = abs(device.latest_rssi - baseline_rssi)
                deviations.append(dev)

        if len(deviations) < 2:
            return None

        avg_deviation = np.mean(deviations)
        # If average deviation from empty-room baseline > 3 dBm, room is likely occupied
        return avg_deviation > 3.0
