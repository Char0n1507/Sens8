"""
Sens8 — RSSI Variance Analysis & Motion Scoring
Improved accuracy via:
  - Adaptive normalization based on AP signal quality
  - Delta variance computed on centered differences
  - Multi-AP weighted fusion with outlier rejection
  - Zone grouping by signal proximity
"""

import time
import logging
import threading
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

import numpy as np

from capture.devices import DeviceTracker, DeviceRecord
from processing.baseline import BaselineCalibrator
import config

logger = logging.getLogger("sens8.processing.variance")


@dataclass
class VarianceResult:
    """Variance analysis result for a single AP."""
    mac: str
    ssid: str
    mean_delta: float       # mean of RSSI deltas
    std_delta: float        # std of RSSI deltas
    peak_delta: float       # max absolute delta
    zero_crossings: int     # sign-change count in deltas
    motion_score: float     # 0.0 to 1.0 normalized
    raw_variance: float     # raw variance value
    weight: float           # fusion weight
    recent_rssi: float      # latest RSSI value


class VarianceAnalyzer:
    """
    Analyzes RSSI variance to detect motion.

    Key improvements over naive variance:
    - Uses delta (first differences) to remove DC offset
    - Normalizes per-AP by baseline std deviation
    - Applies weighted fusion favoring stronger, more stable APs
    - Adaptive threshold that accounts for managed-mode scan jitter
    """

    def __init__(self, tracker: DeviceTracker, baseline: BaselineCalibrator):
        self.tracker = tracker
        self.baseline = baseline
        self._lock = threading.Lock()
        self._motion_score = 0.0
        self._per_ap_results: Dict[str, VarianceResult] = {}
        self._zones: Dict[str, List[str]] = {}
        self._history: list = []  # motion score history for smoothing

    @property
    def motion_score(self) -> float:
        return self._motion_score

    @property
    def per_ap_results(self) -> Dict[str, VarianceResult]:
        with self._lock:
            return dict(self._per_ap_results)

    @property
    def zones(self) -> Dict[str, List[str]]:
        return dict(self._zones)

    def analyze(self) -> float:
        """
        Run variance analysis on all visible APs.
        Returns fused motion score (0.0 to 1.0).
        """
        aps = self.tracker.get_aps(min_samples=3)
        if not aps:
            self._motion_score *= 0.9  # Decay
            return self._motion_score

        results = []
        for ap in aps:
            vr = self._analyze_ap(ap)
            if vr is not None:
                results.append(vr)

        with self._lock:
            self._per_ap_results = {r.mac: r for r in results}

        if not results:
            self._motion_score *= 0.9
            return self._motion_score

        # ─── Weighted Fusion ──────────────────────────────────
        # Use only APs with weight > 0.15 to reject noisy far-away APs
        good_results = [r for r in results if r.weight > 0.15]
        if not good_results:
            good_results = results[:3]  # Use top 3 anyway

        total_weight = sum(r.weight for r in good_results)
        if total_weight <= 0:
            self._motion_score *= 0.9
            return self._motion_score

        fused = sum(r.motion_score * r.weight for r in good_results) / total_weight

        # ─── Temporal smoothing ───────────────────────────────
        alpha = config.MOTION_SCORE_SMOOTHING
        self._motion_score = self._motion_score * (1 - alpha) + fused * alpha

        # Keep history for trend detection
        self._history.append(self._motion_score)
        if len(self._history) > 60:
            self._history = self._history[-60:]

        # Update zones
        self._update_zones(results)

        return self._motion_score

    def _analyze_ap(self, ap: DeviceRecord) -> Optional[VarianceResult]:
        """Analyze RSSI variance for a single AP with improved accuracy."""
        rssi = ap.get_rssi_array(seconds=config.MOTION_WINDOW)
        if len(rssi) < 3:
            return None

        # ─── Compute deltas (first differences) ──────────────
        deltas = np.diff(rssi)
        if len(deltas) < 2:
            return None

        mean_delta = float(np.mean(deltas))
        std_delta = float(np.std(deltas))
        peak_delta = float(np.max(np.abs(deltas)))
        variance = float(np.var(deltas))

        # Zero crossings — movement creates oscillations
        signs = np.sign(deltas)
        sign_changes = np.diff(signs)
        zero_crossings = int(np.count_nonzero(sign_changes))

        # ─── Adaptive normalization ───────────────────────────
        # Baseline-aware: compare current variance to baseline noise floor
        bl = self.baseline.baselines.get(ap.mac)
        noise_floor = 1.0  # default noise floor

        if bl and bl.std_rssi > 0:
            noise_floor = max(bl.std_rssi, 0.5)

        # Motion score: how much variance exceeds the noise floor
        # Higher ratio = more likely real motion vs ambient noise
        excess_ratio = variance / (noise_floor ** 2 + 0.1)

        # Normalize to 0-1 with soft clamp
        # For managed mode: scans have inherent ~1-2 dBm jitter
        # Real motion typically causes 3-10+ dBm swings
        motion_score = min(1.0, excess_ratio / 5.0)

        # Boost if there are many zero crossings (oscillation = movement)
        if len(deltas) > 5:
            zc_rate = zero_crossings / len(deltas)
            if zc_rate > 0.5:  # High oscillation
                motion_score = min(1.0, motion_score * 1.3)

        # ─── Weight by signal quality ─────────────────────────
        latest = ap.latest_rssi or -90
        # Strong signal = more reliable readings, higher weight
        # -30 dBm → 1.0, -60 → 0.5, -90 → 0.1
        weight = max(0.1, min(1.0, (latest + 95) / 65.0))

        # Penalize APs with very few samples (unreliable)
        if len(rssi) < 10:
            weight *= 0.5

        return VarianceResult(
            mac=ap.mac,
            ssid=ap.ssid or ap.mac[:8],
            mean_delta=mean_delta,
            std_delta=std_delta,
            peak_delta=peak_delta,
            zero_crossings=zero_crossings,
            motion_score=motion_score,
            raw_variance=variance,
            weight=weight,
            recent_rssi=float(latest),
        )

    def _update_zones(self, results: List[VarianceResult]):
        """Group APs into proximity zones by signal strength."""
        zones = {"near": [], "medium": [], "far": []}

        for r in results:
            if r.recent_rssi >= -50:
                zones["near"].append(r.mac)
            elif r.recent_rssi >= -70:
                zones["medium"].append(r.mac)
            else:
                zones["far"].append(r.mac)

        self._zones = {k: v for k, v in zones.items() if v}

    def get_variance_vector(self, mac: str) -> Optional[np.ndarray]:
        """Build a feature vector for an AP (used by DBSCAN counter)."""
        with self._lock:
            vr = self._per_ap_results.get(mac)
            if vr is None:
                return None

        return np.array([
            vr.mean_delta,
            vr.std_delta,
            vr.peak_delta,
            vr.zero_crossings / max(1.0, 20.0),
        ], dtype=np.float64)

    def get_all_variance_vectors(self) -> Tuple[List[str], np.ndarray]:
        """Get variance vectors for all APs with results."""
        with self._lock:
            macs = []
            vectors = []
            for mac, vr in self._per_ap_results.items():
                vec = np.array([
                    vr.mean_delta,
                    vr.std_delta,
                    vr.peak_delta,
                    vr.zero_crossings / max(1.0, 20.0),
                ], dtype=np.float64)
                macs.append(mac)
                vectors.append(vec)

        if not vectors:
            return [], np.array([])

        return macs, np.vstack(vectors)

    def get_motion_trend(self) -> str:
        """Get motion trend: rising, falling, stable."""
        if len(self._history) < 10:
            return "collecting"

        recent = self._history[-5:]
        older = self._history[-10:-5]
        avg_recent = np.mean(recent)
        avg_older = np.mean(older)

        diff = avg_recent - avg_older
        if diff > 0.05:
            return "rising"
        elif diff < -0.05:
            return "falling"
        return "stable"
