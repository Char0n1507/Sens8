"""
Sens8 — RSSI Variance Analysis & Motion Scoring
Computes motion score from RSSI delta variance across a sliding window.
Multi-AP fusion for robust detection.
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
    zero_crossings: int     # zero-crossing count of deltas
    motion_score: float     # 0.0 to 1.0 normalized motion score
    raw_variance: float     # raw variance value
    weight: float           # signal strength weight (stronger = higher)


class VarianceAnalyzer:
    """
    Analyzes RSSI variance to detect motion.
    Motion score = variance of RSSI delta over sliding window.
    Multi-AP fusion: weight stronger signals higher.
    """

    def __init__(self, tracker: DeviceTracker, baseline: BaselineCalibrator):
        self.tracker = tracker
        self.baseline = baseline
        self._lock = threading.Lock()

        # Smoothed global motion score
        self._motion_score = 0.0
        self._per_ap_results: Dict[str, VarianceResult] = {}

        # Zone groupings
        self._zones: Dict[str, List[str]] = {}  # zone_name → [mac, ...]

    @property
    def motion_score(self) -> float:
        """Global fused motion score (0.0 to 1.0)."""
        return self._motion_score

    @property
    def per_ap_results(self) -> Dict[str, VarianceResult]:
        """Per-AP variance analysis results."""
        with self._lock:
            return dict(self._per_ap_results)

    @property
    def zones(self) -> Dict[str, List[str]]:
        """Zone groupings."""
        return dict(self._zones)

    def analyze(self) -> float:
        """
        Run variance analysis on all visible APs.
        Returns global fused motion score (0.0 to 1.0).
        """
        aps = self.tracker.get_aps(min_samples=5)
        if not aps:
            self._motion_score = 0.0
            return 0.0

        results = []
        for ap in aps:
            vr = self._analyze_ap(ap)
            if vr is not None:
                results.append(vr)

        with self._lock:
            self._per_ap_results = {r.mac: r for r in results}

        if not results:
            self._motion_score = 0.0
            return 0.0

        # Weighted fusion across APs
        total_weight = sum(r.weight for r in results)
        if total_weight <= 0:
            self._motion_score = 0.0
            return 0.0

        fused_score = sum(r.motion_score * r.weight for r in results) / total_weight

        # EMA smoothing
        alpha = config.MOTION_SCORE_SMOOTHING
        self._motion_score = self._motion_score * (1 - alpha) + fused_score * alpha

        # Update zones
        self._update_zones(results)

        return self._motion_score

    def _analyze_ap(self, ap: DeviceRecord) -> Optional[VarianceResult]:
        """Analyze RSSI variance for a single AP."""
        rssi = ap.get_rssi_array(seconds=config.MOTION_WINDOW)
        if len(rssi) < 5:
            return None

        # Compute deltas (first differences)
        deltas = np.diff(rssi)
        if len(deltas) < 3:
            return None

        mean_delta = float(np.mean(deltas))
        std_delta = float(np.std(deltas))
        peak_delta = float(np.max(np.abs(deltas)))
        variance = float(np.var(deltas))

        # Zero crossings — how often the delta changes sign
        signs = np.sign(deltas)
        sign_changes = np.diff(signs)
        zero_crossings = int(np.count_nonzero(sign_changes))

        # Normalize motion score (0 to 1)
        # Typical quiet room variance < 1, active person > 5
        motion_score = min(variance / 10.0, 1.0)

        # Weight by signal strength (stronger = more reliable)
        latest = ap.latest_rssi or -90
        # Map -30 → weight=1.0, -90 → weight=0.1
        weight = max(0.1, min(1.0, (latest + 90) / 60.0))

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
        )

    def _update_zones(self, results: List[VarianceResult]):
        """
        Group APs into zones by signal strength.
        Strong signal APs are likely in the same room,
        weaker ones in adjacent rooms.
        """
        zones = {"near": [], "medium": [], "far": []}

        for r in results:
            ap = self.tracker.get_device(r.mac)
            if ap is None:
                continue
            rssi = ap.latest_rssi or -90

            if rssi >= -50:
                zones["near"].append(r.mac)
            elif rssi >= -70:
                zones["medium"].append(r.mac)
            else:
                zones["far"].append(r.mac)

        # Only keep non-empty zones
        self._zones = {k: v for k, v in zones.items() if v}

    def get_variance_vector(self, mac: str) -> Optional[np.ndarray]:
        """
        Build a variance feature vector for an AP.
        Used by DBSCAN person counter.
        Returns [mean_delta, std_delta, peak_delta, zero_crossings_normalized]
        """
        with self._lock:
            vr = self._per_ap_results.get(mac)
            if vr is None:
                return None

        return np.array([
            vr.mean_delta,
            vr.std_delta,
            vr.peak_delta,
            vr.zero_crossings / 20.0,  # normalize
        ], dtype=np.float64)

    def get_all_variance_vectors(self) -> Tuple[List[str], np.ndarray]:
        """
        Get variance vectors for all APs with results.
        Returns (mac_list, feature_matrix).
        """
        with self._lock:
            macs = []
            vectors = []
            for mac, vr in self._per_ap_results.items():
                vec = np.array([
                    vr.mean_delta,
                    vr.std_delta,
                    vr.peak_delta,
                    vr.zero_crossings / 20.0,
                ], dtype=np.float64)
                macs.append(mac)
                vectors.append(vec)

        if not vectors:
            return [], np.array([])

        return macs, np.vstack(vectors)
