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

    # ADR-018 Frame constants
    MAGIC = 0xC5110001
    NODE_ID = 255
    NUM_ANTENNAS = 1
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
        Returns raw bytes ready for UDP transmission (ADR-018 compliant).
        """
        self._seq = (self._seq + 1) % 65536

        # Get current state
        motion_score = self.variance.motion_score

        # ─── Header (20 bytes) ─────────────────────────────────
        # Offset  Size  Field
        # 0       4     Magic: 0xC5110001
        # 4       1     Node ID
        # 5       1     Number of antennas
        # 6       2     Number of subcarriers
        # 8       4     Frequency MHz
        # 12      4     Sequence number
        # 16      1     RSSI (i8)
        # 17      1     Noise floor (i8)
        # 18      1     PPDU type (0)
        # 19      1     Flags (0)
        header = struct.pack(
            "<IBBHIIbbBB",
            self.MAGIC,
            self.NODE_ID,
            self.NUM_ANTENNAS,
            self.NUM_SUBCARRIERS,
            2437,            # Freq MHz
            self._seq,       # Sequence
            -50,             # Simulated RSSI
            -95,             # Simulated Noise
            0,               # PPDU (HT Legacy)
            0,               # Flags
        )

        # ─── Synthetic CSI Data ────────────────────────────────
        amplitudes, phases = self._synthesize_csi(motion_score)

        csi_data = bytearray()
        for i in range(self.NUM_SUBCARRIERS):
            # ADR-018 expects I/Q as i8 pairs (-128 to 127)
            amp = amplitudes[i] * 120.0
            ph = phases[i]
            
            i_val = int(np.clip(amp * np.cos(ph), -128, 127))
            q_val = int(np.clip(amp * np.sin(ph), -128, 127))
            
            csi_data.extend(struct.pack("<bb", i_val, q_val))

        # ADR-018 does not use the metadata suffix, so we omit it
        # to ensure strict compliance with RuView decoders.
        return header + csi_data

    def _synthesize_csi(self, motion_score: float):
        """
        Map RSSI variance data to synthetic subcarrier amplitudes and phases.
        """
        n = self.NUM_SUBCARRIERS
        per_ap = self.variance.per_ap_results
        ap_count = len(per_ap)

        base_amp = 0.3 + motion_score * 0.5
        amplitudes = np.ones(n, dtype=np.float64) * base_amp

        if ap_count > 0:
            group_size = max(1, n // max(ap_count, 1))
            for idx, (mac, vr) in enumerate(per_ap.items()):
                start = (idx * group_size) % n
                end = min(start + group_size, n)
                amp_mod = vr.raw_variance / 10.0
                amplitudes[start:end] *= (1.0 + amp_mod)

        noise = np.random.normal(0, motion_score * 0.1, n)
        amplitudes += noise
        amplitudes = np.clip(amplitudes, 0.0, 1.0)

        phases = np.zeros(n, dtype=np.float64)
        if motion_score > 0.1:
            phase_shift = motion_score * np.pi
            phases = np.linspace(-phase_shift, phase_shift, n)
            count = self.counter.count
            if count > 1:
                for k in range(1, min(count, 4)):
                    phases += 0.3 * np.sin(2 * np.pi * k * np.arange(n) / n)

        return amplitudes, phases

    def build_empty_frame(self) -> bytes:
        """Build a minimal valid ADR-018 frame (keepalive)."""
        self._seq = (self._seq + 1) % 65536
        header = struct.pack(
            "<IBBHIIbbBB",
            self.MAGIC, self.NODE_ID, self.NUM_ANTENNAS, self.NUM_SUBCARRIERS,
            2437, self._seq, -50, -95, 0, 0
        )
        csi_data = struct.pack("<bb", 100, 0) * self.NUM_SUBCARRIERS
        return header + csi_data
