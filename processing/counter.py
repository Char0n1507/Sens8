"""
Sens8 — Person Count via DBSCAN Clustering
Estimates number of people present by clustering RSSI variance
signatures across multiple APs.

Each person creates a distinct multipath disturbance pattern.
Clusters above motion threshold ≈ person count.

Honest label: "estimated occupancy — accuracy drops above 2 people without real CSI hardware"
"""

import time
import logging
import threading
from dataclasses import dataclass
from typing import Optional

import numpy as np
from sklearn.cluster import DBSCAN
from sklearn.preprocessing import StandardScaler

from processing.variance import VarianceAnalyzer
import config

logger = logging.getLogger("sens8.processing.counter")


@dataclass
class CountResult:
    """Person count estimation result."""
    count: int = 0
    confidence: float = 0.0
    cluster_count: int = 0
    ap_count_used: int = 0
    timestamp: float = 0.0
    label: str = "estimated occupancy"

    @property
    def display(self) -> str:
        if self.count == 0:
            return "0 people"
        elif self.count == 1:
            return "1 person"
        elif self.count <= 3:
            return f"{self.count} people"
        else:
            return "3+ people"

    @property
    def confidence_label(self) -> str:
        if self.confidence >= 0.6:
            return "high"
        elif self.confidence >= 0.4:
            return "medium"
        else:
            return "low"


class PersonCounter:
    """
    Estimates person count using DBSCAN clustering on
    per-AP variance feature vectors.

    Method:
    1. Build a variance vector per AP: [mean_delta, std_delta, peak_delta, zero_crossings]
    2. Standardize features
    3. Run DBSCAN clustering
    4. Clusters above motion threshold ≈ distinct persons
    5. Apply hysteresis before reporting changes
    """

    def __init__(self, variance_analyzer: VarianceAnalyzer):
        self.variance = variance_analyzer
        self._result = CountResult()
        self._pending_count: int = 0
        self._pending_frames: int = 0
        self._last_update: float = 0.0
        self._lock = threading.Lock()

    @property
    def result(self) -> CountResult:
        with self._lock:
            return CountResult(
                count=self._result.count,
                confidence=self._result.confidence,
                cluster_count=self._result.cluster_count,
                ap_count_used=self._result.ap_count_used,
                timestamp=self._result.timestamp,
                label=self._result.label,
            )

    @property
    def count(self) -> int:
        return self._result.count

    def update(self) -> CountResult:
        """
        Run person count estimation.
        Should be called every COUNT_UPDATE_INTERVAL seconds.
        """
        now = time.time()

        # Rate limit
        if now - self._last_update < config.COUNT_UPDATE_INTERVAL:
            return self.result
        self._last_update = now

        macs, features = self.variance.get_all_variance_vectors()

        if len(macs) < config.MIN_APS_FOR_COUNTING:
            # Not enough APs for meaningful clustering
            with self._lock:
                self._result.ap_count_used = len(macs)
                self._result.timestamp = now
            return self.result

        # Standardize features
        try:
            scaler = StandardScaler()
            X = scaler.fit_transform(features)
        except Exception as e:
            logger.debug(f"Scaling failed: {e}")
            return self.result

        # Run DBSCAN
        try:
            db = DBSCAN(
                eps=config.DBSCAN_EPS,
                min_samples=config.DBSCAN_MIN_SAMPLES,
            ).fit(X)
            labels = db.labels_
        except Exception as e:
            logger.debug(f"DBSCAN failed: {e}")
            return self.result

        # Count clusters (excluding noise label -1)
        unique_labels = set(labels)
        unique_labels.discard(-1)
        n_clusters = len(unique_labels)

        # Determine which clusters show significant motion
        active_clusters = 0
        motion_threshold = config.PRESENCE_THRESHOLD

        for cluster_id in unique_labels:
            cluster_mask = labels == cluster_id
            cluster_features = features[cluster_mask]
            # Use std_delta (column 1) as motion indicator
            avg_motion = np.mean(np.abs(cluster_features[:, 1]))
            if avg_motion > motion_threshold * 0.5:
                active_clusters += 1

        # Person count = active clusters (capped at reasonable range)
        raw_count = min(active_clusters, 5)

        # Apply hysteresis
        new_count = self._apply_hysteresis(raw_count)

        # Compute confidence
        confidence = self._compute_confidence(new_count, len(macs), n_clusters)

        with self._lock:
            self._result = CountResult(
                count=new_count,
                confidence=confidence,
                cluster_count=n_clusters,
                ap_count_used=len(macs),
                timestamp=now,
                label="estimated occupancy",
            )

        return self.result

    def _apply_hysteresis(self, raw_count: int) -> int:
        """
        Require COUNT_HYSTERESIS consecutive same-count frames
        before reporting a change.
        """
        if raw_count == self._result.count:
            self._pending_frames = 0
            return raw_count

        if raw_count == self._pending_count:
            self._pending_frames += 1
        else:
            self._pending_count = raw_count
            self._pending_frames = 1

        if self._pending_frames >= config.COUNT_HYSTERESIS:
            logger.info(
                f"Person count changed: {self._result.count} → {raw_count}"
            )
            self._pending_frames = 0
            return raw_count

        return self._result.count

    def _compute_confidence(self, count: int, ap_count: int,
                            cluster_count: int) -> float:
        """
        Compute confidence in person count estimate.
        Confidence degrades with higher counts — honest about limitations.
        """
        if count == 0:
            # High confidence in "empty" if motion is low
            base_conf = 0.7
        elif count == 1:
            base_conf = 0.6
        elif count == 2:
            base_conf = 0.4
        else:
            base_conf = 0.25  # Low confidence for 3+ without real CSI

        # More APs → better confidence
        ap_boost = min(ap_count / 10.0, 0.15)

        confidence = min(base_conf + ap_boost, config.PRESENCE_MAX_CONFIDENCE)
        return round(confidence, 3)

    def reset(self):
        """Reset counter state."""
        with self._lock:
            self._result = CountResult()
            self._pending_count = 0
            self._pending_frames = 0
