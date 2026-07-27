"""
Sens8 — Breathing Rate Estimation (Best-Effort)
Applies bandpass filter to smoothed RSSI time series to detect
slow oscillations consistent with breathing.

⚠ RSSI does NOT provide true CSI-quality vitals.
All estimates labeled as "estimated — low confidence without CSI hardware".
"""

import logging
import threading
from dataclasses import dataclass
from typing import Optional

import numpy as np
from scipy.signal import butter, filtfilt

from capture.devices import DeviceTracker
import config

logger = logging.getLogger("sens8.processing.vitals")


@dataclass
class VitalsResult:
    """Breathing rate estimation result."""
    bpm: float = 0.0
    confidence: float = 0.0
    stable_ap_mac: str = ""
    stable_ap_ssid: str = ""
    sample_count: int = 0
    label: str = "estimated — low confidence without CSI hardware"

    @property
    def is_reportable(self) -> bool:
        """Only report if confidence meets minimum threshold."""
        return self.confidence >= config.BREATHING_CONFIDENCE_MIN


class VitalsEstimator:
    """
    Best-effort breathing rate estimation from RSSI.

    Method:
    1. Select strongest/most stable AP signal
    2. Apply bandpass filter (0.1–0.5 Hz → 6–30 BPM)
    3. Count zero crossings → BPM estimate
    4. Only report if confidence > BREATHING_CONFIDENCE_MIN

    This is a best-effort feature. Real breathing detection requires CSI.
    """

    def __init__(self, tracker: DeviceTracker):
        self.tracker = tracker
        self._result = VitalsResult()
        self._lock = threading.Lock()

    @property
    def result(self) -> VitalsResult:
        with self._lock:
            return VitalsResult(
                bpm=self._result.bpm,
                confidence=self._result.confidence,
                stable_ap_mac=self._result.stable_ap_mac,
                stable_ap_ssid=self._result.stable_ap_ssid,
                sample_count=self._result.sample_count,
                label=self._result.label,
            )

    def update(self) -> VitalsResult:
        """
        Run breathing rate estimation.
        Should be called every few seconds.
        """
        # Find the strongest/most stable AP
        best_ap = self._select_best_ap()
        if best_ap is None:
            with self._lock:
                self._result = VitalsResult()
            return self.result

        # Get RSSI time series
        rssi = best_ap.get_rssi_array(seconds=config.VITALS_WINDOW_SECONDS)
        timestamps = best_ap.get_timestamps(seconds=config.VITALS_WINDOW_SECONDS)

        if len(rssi) < 50:
            with self._lock:
                self._result = VitalsResult(
                    stable_ap_mac=best_ap.mac,
                    stable_ap_ssid=best_ap.ssid,
                    sample_count=len(rssi),
                )
            return self.result

        # Estimate sample rate from timestamps
        dt = np.mean(np.diff(timestamps))
        if dt <= 0:
            return self.result
        fs = 1.0 / dt

        # Need at least 2x the highest frequency of interest (Nyquist)
        if fs < 1.0:
            with self._lock:
                self._result = VitalsResult(
                    stable_ap_mac=best_ap.mac,
                    stable_ap_ssid=best_ap.ssid,
                    sample_count=len(rssi),
                )
            return self.result

        # Bandpass filter for breathing frequencies
        bpm, confidence = self._extract_breathing(rssi, fs)

        with self._lock:
            self._result = VitalsResult(
                bpm=bpm,
                confidence=confidence,
                stable_ap_mac=best_ap.mac,
                stable_ap_ssid=best_ap.ssid,
                sample_count=len(rssi),
            )

        return self.result

    def _select_best_ap(self):
        """Select the AP with strongest, most stable signal."""
        aps = self.tracker.get_aps(min_samples=30)
        if not aps:
            return None

        best = None
        best_score = -999

        for ap in aps:
            rssi = ap.get_rssi_array(seconds=config.VITALS_WINDOW_SECONDS)
            if len(rssi) < 30:
                continue

            # Score: prefer strong signal with low variance (stable link)
            mean_rssi = np.mean(rssi)
            std_rssi = np.std(rssi)
            # Strong signal → higher score, low variance → higher score
            score = mean_rssi - std_rssi * 2

            if score > best_score:
                best_score = score
                best = ap

        return best

    def _extract_breathing(self, rssi: np.ndarray, fs: float) -> tuple:
        """
        Apply bandpass filter and zero-crossing analysis
        to extract breathing rate estimate.

        Returns (bpm, confidence).
        """
        try:
            # Remove DC / detrend
            rssi_centered = rssi - np.mean(rssi)

            # Design bandpass filter: 0.1 – 0.5 Hz (6–30 BPM)
            low = config.BREATHING_BAND_LOW
            high = config.BREATHING_BAND_HIGH

            # Clamp filter frequencies to Nyquist
            nyquist = fs / 2.0
            if high >= nyquist:
                high = nyquist * 0.9
            if low >= high:
                return 0.0, 0.0

            b, a = butter(3, [low / nyquist, high / nyquist], btype='band')

            # Apply filter
            filtered = filtfilt(b, a, rssi_centered)

            # Zero-crossing count
            zero_crossings = np.sum(np.diff(np.sign(filtered)) != 0)

            # Duration in seconds
            duration = len(rssi) / fs

            # BPM = (zero_crossings / 2) / duration * 60
            # Each full cycle has 2 zero crossings
            if duration < 5:
                return 0.0, 0.0

            bpm = (zero_crossings / 2.0) / duration * 60.0

            # Sanity check: reasonable breathing range
            if not (6 <= bpm <= 30):
                return 0.0, 0.1

            # Confidence based on signal quality
            signal_power = np.var(filtered)
            noise_power = np.var(rssi_centered - filtered)
            if noise_power <= 0:
                snr = 0
            else:
                snr = signal_power / noise_power

            # Map SNR to confidence (0–1)
            confidence = min(snr * 2.0, 0.6)  # Cap at 0.6 — RSSI is noisy

            # Reduce confidence if too few samples
            if len(rssi) < 100:
                confidence *= 0.7

            return round(bpm, 1), round(confidence, 3)

        except Exception as e:
            logger.debug(f"Vitals extraction error: {e}")
            return 0.0, 0.0
