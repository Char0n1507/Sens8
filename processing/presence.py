"""
Sens8 — Presence Inference
Determines presence from sustained motion variance with hysteresis
to prevent detection flicker.
"""

import time
import logging
import threading
from dataclasses import dataclass

import config

logger = logging.getLogger("sens8.processing.presence")


@dataclass
class PresenceState:
    """Current presence detection state."""
    present: bool = False
    confidence: float = 0.0
    consecutive_frames: int = 0
    last_change: float = 0.0
    duration: float = 0.0  # how long current state has been active


class PresenceDetector:
    """
    Infers presence from sustained motion score above threshold.
    Uses hysteresis to prevent rapid state flicker.

    RSSI-based estimate — confidence capped at PRESENCE_MAX_CONFIDENCE.
    """

    def __init__(self):
        self._state = PresenceState()
        self._pending_state: bool = False
        self._pending_count: int = 0
        self._lock = threading.Lock()

    @property
    def state(self) -> PresenceState:
        with self._lock:
            return PresenceState(
                present=self._state.present,
                confidence=self._state.confidence,
                consecutive_frames=self._state.consecutive_frames,
                last_change=self._state.last_change,
                duration=self._state.duration,
            )

    @property
    def is_present(self) -> bool:
        return self._state.present

    @property
    def confidence(self) -> float:
        return self._state.confidence

    def update(self, motion_score: float) -> PresenceState:
        """
        Update presence state based on current motion score.
        Called at the analysis rate (~2Hz).

        Returns current state after update.
        """
        now = time.time()
        threshold = config.PRESENCE_THRESHOLD
        hysteresis = config.PRESENCE_HYSTERESIS

        # Determine raw detection
        raw_present = motion_score > threshold

        with self._lock:
            # Hysteresis: require N consecutive frames before changing state
            if raw_present != self._state.present:
                if raw_present == self._pending_state:
                    self._pending_count += 1
                else:
                    self._pending_state = raw_present
                    self._pending_count = 1

                # Check if hysteresis threshold met
                if self._pending_count >= hysteresis:
                    self._state.present = raw_present
                    self._state.last_change = now
                    self._pending_count = 0
                    logger.info(
                        f"Presence state changed: "
                        f"{'PRESENT' if raw_present else 'ABSENT'} "
                        f"(motion_score={motion_score:.3f})"
                    )
            else:
                self._pending_count = 0

            # Update consecutive frames
            if raw_present == self._state.present:
                self._state.consecutive_frames += 1
            else:
                self._state.consecutive_frames = 0

            # Compute confidence
            self._state.confidence = self._compute_confidence(motion_score)

            # Update duration
            if self._state.last_change > 0:
                self._state.duration = now - self._state.last_change

            return self.state

    def _compute_confidence(self, motion_score: float) -> float:
        """
        Compute detection confidence.
        Cap at PRESENCE_MAX_CONFIDENCE — honest about RSSI limitations.
        """
        if not self._state.present:
            # Confidence in "absent" state
            if motion_score < config.PRESENCE_THRESHOLD * 0.5:
                conf = 0.8  # Very low motion → confident absent
            elif motion_score < config.PRESENCE_THRESHOLD:
                conf = 0.5  # Near threshold → less confident
            else:
                conf = 0.3  # Above threshold but hysteresis hasn't triggered
        else:
            # Confidence in "present" state
            excess = motion_score - config.PRESENCE_THRESHOLD
            conf = min(
                0.4 + (excess / config.PRESENCE_THRESHOLD) * 0.35,
                config.PRESENCE_MAX_CONFIDENCE
            )

        # Boost confidence with sustained consecutive frames
        frame_boost = min(self._state.consecutive_frames / 20.0, 0.1)
        conf = min(conf + frame_boost, config.PRESENCE_MAX_CONFIDENCE)

        return round(conf, 3)

    def reset(self):
        """Reset presence state."""
        with self._lock:
            self._state = PresenceState()
            self._pending_state = False
            self._pending_count = 0
