"""
Sens8 — Synthetic CSI Frame Generation
Maps RSSI variance data into synthetic CSI-like frames that
mimic the ESP32 CSI format expected by RuView on UDP 5005.

All frames tagged with source="software-rssi" to distinguish from real CSI.
"""

import struct
import time
import logging
from typing import Optional

import numpy as np

from processing.variance import VarianceAnalyzer
from processing.presence import PresenceDetector
from processing.counter import PersonCounter
import config

logger = logging.getLogger("sens8.synthetic.csi_frame")


class CSIFrameBuilder:
    """
    Builds synthetic CSI-like UDP frames from RSSI sensing data.

    Frame structure (binary):
    ┌─────────────────────────────────────────────┐
    │ Header (16 bytes)                           │
    │   magic: 4 bytes  "CSI\x00"                │
    │   version: 1 byte  (0x02)                  │
    │   source: 1 byte   (0xFF = software-rssi)  │
    │   seq: 2 bytes     (frame sequence number) │
    │   timestamp: 4 bytes (unix seconds)        │
    │   flags: 2 bytes   (presence/motion flags) │
    │   person_count: 1 byte                     │
    │   reserved: 1 byte                         │
    ├─────────────────────────────────────────────┤
    │ CSI Data (224 bytes = 56 subcarriers × 4)  │
    │   Per subcarrier: amplitude (i16) + phase (i16) │
    ├─────────────────────────────────────────────┤
    │ Metadata (variable, JSON-like)             │
    │   motion_score, confidence, source tag     │
    └─────────────────────────────────────────────┘
    """

    # Frame constants
    MAGIC = b"CSI\x00"
    VERSION = 0x02
    SOURCE_SOFTWARE = 0xFF
    NUM_SUBCARRIERS = config.CSI_SUBCARRIERS

    def __init__(self, variance: VarianceAnalyzer,
                 presence: PresenceDetector,
                 counter: PersonCounter):
        self.variance = variance
        self.presence = presence
        self.counter = counter
        self._seq = 0

    def build_frame(self) -> bytes:
        """
        Build a single synthetic CSI frame from current sensing state.
        Returns raw bytes ready for UDP transmission.
        """
        self._seq = (self._seq + 1) % 65536
        now = int(time.time())

        # Get current state
        motion_score = self.variance.motion_score
        pres = self.presence.state
        count = self.counter.result

        # Build flags
        flags = 0
        if pres.present:
            flags |= 0x01  # Presence detected
        if motion_score > config.PRESENCE_THRESHOLD:
            flags |= 0x02  # Motion detected
        if count.count > 0:
            flags |= 0x04  # Occupancy detected
        flags |= 0x80  # Software source flag

        # ─── Header ────────────────────────────────────────────
        header = struct.pack(
            "<4sBBHIHBB",
            self.MAGIC,           # magic
            self.VERSION,         # version
            self.SOURCE_SOFTWARE, # source
            self._seq,            # sequence
            now,                  # timestamp
            flags,                # flags
            min(count.count, 255),  # person_count
            0,                    # reserved
        )

        # ─── Synthetic CSI Data ────────────────────────────────
        amplitudes, phases = self._synthesize_csi(motion_score)

        csi_data = b""
        for i in range(self.NUM_SUBCARRIERS):
            amp = int(np.clip(amplitudes[i] * 32767, -32768, 32767))
            ph = int(np.clip(phases[i] * 32767, -32768, 32767))
            csi_data += struct.pack("<hh", amp, ph)

        # ─── Metadata ──────────────────────────────────────────
        meta = self._build_metadata(motion_score, pres, count)

        return header + csi_data + meta

    def _synthesize_csi(self, motion_score: float):
        """
        Map RSSI variance data to synthetic subcarrier amplitudes and phases.

        - Baseline (no motion): low-amplitude, near-zero phase
        - Motion: increased amplitude variation, phase shifts
        - Person count: more clusters → more complex patterns
        """
        n = self.NUM_SUBCARRIERS

        # Get per-AP variance results for texture
        per_ap = self.variance.per_ap_results
        ap_count = len(per_ap)

        # Base amplitude from overall signal levels
        base_amp = 0.3 + motion_score * 0.5

        # Generate amplitude profile
        amplitudes = np.ones(n, dtype=np.float64) * base_amp

        if ap_count > 0:
            # Map per-AP variance to subcarrier groups
            group_size = max(1, n // max(ap_count, 1))
            for idx, (mac, vr) in enumerate(per_ap.items()):
                start = (idx * group_size) % n
                end = min(start + group_size, n)
                # Variance → amplitude modulation
                amp_mod = vr.raw_variance / 10.0
                amplitudes[start:end] *= (1.0 + amp_mod)

        # Add noise proportional to motion
        noise = np.random.normal(0, motion_score * 0.1, n)
        amplitudes += noise
        amplitudes = np.clip(amplitudes, 0.0, 1.0)

        # Generate phase profile
        phases = np.zeros(n, dtype=np.float64)

        if motion_score > 0.1:
            # Motion causes phase shifts
            phase_shift = motion_score * np.pi
            # Create phase gradient across subcarriers
            phases = np.linspace(-phase_shift, phase_shift, n)
            # Add person-count dependent complexity
            count = self.counter.count
            if count > 1:
                for k in range(1, min(count, 4)):
                    phases += 0.3 * np.sin(2 * np.pi * k * np.arange(n) / n)

            # Normalize to [-1, 1]
            if np.max(np.abs(phases)) > 0:
                phases /= np.max(np.abs(phases))

        return amplitudes, phases

    def _build_metadata(self, motion_score: float,
                        pres, count) -> bytes:
        """Build metadata suffix for the frame."""
        meta_str = (
            f"motion={motion_score:.3f},"
            f"presence={int(pres.present)},"
            f"confidence={pres.confidence:.3f},"
            f"count={count.count},"
            f"count_conf={count.confidence:.3f},"
            f"source={config.CSI_SOURCE_TAG}"
        )
        meta_bytes = meta_str.encode("ascii")
        # Prefix with length byte
        return struct.pack("<H", len(meta_bytes)) + meta_bytes

    def build_empty_frame(self) -> bytes:
        """Build a minimal frame indicating no sensing data (keepalive)."""
        self._seq = (self._seq + 1) % 65536
        now = int(time.time())

        header = struct.pack(
            "<4sBBHIHBB",
            self.MAGIC, self.VERSION, self.SOURCE_SOFTWARE,
            self._seq, now, 0x80, 0, 0
        )

        # Flat CSI — no motion
        csi_data = struct.pack("<hh", 100, 0) * self.NUM_SUBCARRIERS

        meta = b"source=software-rssi,keepalive=1"
        meta_frame = struct.pack("<H", len(meta)) + meta

        return header + csi_data + meta_frame
