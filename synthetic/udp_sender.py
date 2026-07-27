"""
Sens8 — UDP Sender for RuView Integration
Sends synthetic CSI frames to RuView's UDP port 5005 at the configured rate.
"""

import socket
import time
import logging
import threading
from typing import Optional

from synthetic.csi_frame import CSIFrameBuilder
import config

logger = logging.getLogger("sens8.synthetic.udp_sender")


class UDPSender:
    """
    Sends synthetic CSI frames via UDP to the RuView server.
    Runs in a background thread at CSI_FRAME_RATE Hz.
    """

    def __init__(self, frame_builder: CSIFrameBuilder):
        self.frame_builder = frame_builder
        self.host = config.RUVIEW_HOST
        self.port = config.RUVIEW_UDP_PORT
        self._socket: Optional[socket.socket] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._frames_sent = 0
        self._errors = 0
        self._connected = False
        self._lock = threading.Lock()

    @property
    def frames_sent(self) -> int:
        return self._frames_sent

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def error_count(self) -> int:
        return self._errors

    def start(self):
        """Start the UDP sender thread."""
        if self._running:
            return

        try:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._socket.settimeout(1.0)
            self._connected = True
            logger.info(f"✓ UDP socket ready → {self.host}:{self.port}")
        except Exception as e:
            logger.error(f"Failed to create UDP socket: {e}")
            self._connected = False
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._send_loop, daemon=True, name="udp-sender"
        )
        self._thread.start()

    def stop(self):
        """Stop the UDP sender."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)
        if self._socket:
            self._socket.close()
            self._socket = None
        self._connected = False
        logger.info(f"UDP sender stopped. {self._frames_sent} frames sent.")

    def _send_loop(self):
        """Main send loop — runs at CSI_FRAME_RATE Hz."""
        interval = 1.0 / config.CSI_FRAME_RATE
        consecutive_errors = 0

        while self._running:
            start = time.monotonic()

            try:
                frame = self.frame_builder.build_frame()
                self._socket.sendto(frame, (self.host, self.port))
                self._frames_sent += 1
                consecutive_errors = 0
                self._connected = True
            except OSError as e:
                self._errors += 1
                consecutive_errors += 1
                if consecutive_errors == 1:
                    logger.warning(f"UDP send error: {e}")
                elif consecutive_errors % 50 == 0:
                    logger.warning(
                        f"UDP send failing ({consecutive_errors} consecutive errors)"
                    )
                self._connected = False
            except Exception as e:
                self._errors += 1
                logger.debug(f"Frame build/send error: {e}")

            # Sleep to maintain target rate
            elapsed = time.monotonic() - start
            sleep_time = interval - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

    def send_single(self, data: bytes) -> bool:
        """Send a single raw frame. Returns True on success."""
        if not self._socket:
            return False
        try:
            self._socket.sendto(data, (self.host, self.port))
            self._frames_sent += 1
            return True
        except Exception as e:
            self._errors += 1
            logger.debug(f"Single send error: {e}")
            return False

    def check_connection(self) -> bool:
        """
        Quick connectivity check — try to send a keepalive frame.
        Note: UDP is connectionless, so this just checks if send doesn't error.
        """
        try:
            if self._socket is None:
                self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self._socket.settimeout(1.0)

            frame = self.frame_builder.build_empty_frame()
            self._socket.sendto(frame, (self.host, self.port))
            self._connected = True
            return True
        except Exception:
            self._connected = False
            return False
