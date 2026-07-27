"""
Sens8 — WebSocket Output to RuView
Mirrors sensing data to ws://localhost:3001/ws/sensing for
the RuView dashboard to consume.
"""

import json
import time
import logging
import asyncio
import threading
from typing import Optional

import config

logger = logging.getLogger("sens8.output.websocket")

# Try to import websockets — optional dependency
try:
    import websockets
    import websockets.sync.client as ws_sync
    HAS_WEBSOCKETS = True
except ImportError:
    HAS_WEBSOCKETS = False
    logger.info("websockets not installed — WS output disabled")


class WebSocketOutput:
    """
    Sends sensing data to RuView via WebSocket.
    Runs in a background thread with automatic reconnection.
    """

    def __init__(self):
        self.url = config.RUVIEW_WS_URL
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._connected = False
        self._messages_sent = 0
        self._last_data: Optional[dict] = None
        self._lock = threading.Lock()

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def messages_sent(self) -> int:
        return self._messages_sent

    def start(self):
        """Start WebSocket output thread."""
        if not HAS_WEBSOCKETS:
            logger.warning("websockets package not available, skipping WS output")
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="ws-output"
        )
        self._thread.start()

    def stop(self):
        """Stop WebSocket output."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)

    def update_data(self, data: dict):
        """Update the data to send on next cycle."""
        with self._lock:
            self._last_data = data

    def _run_loop(self):
        """Main WebSocket loop with reconnection."""
        while self._running:
            try:
                self._connect_and_send()
            except Exception as e:
                logger.debug(f"WebSocket error: {e}")
                self._connected = False
                time.sleep(5)  # Wait before reconnecting

    def _connect_and_send(self):
        """Connect to WebSocket and send data periodically."""
        try:
            ws = ws_sync.connect(self.url, open_timeout=5)
            self._connected = True
            logger.info(f"✓ WebSocket connected to {self.url}")

            while self._running:
                with self._lock:
                    data = self._last_data

                if data:
                    try:
                        msg = json.dumps({
                            "type": "sensing_update",
                            "source": config.CSI_SOURCE_TAG,
                            "timestamp": time.time(),
                            "data": data,
                        })
                        ws.send(msg)
                        self._messages_sent += 1
                    except Exception as e:
                        logger.debug(f"WS send error: {e}")
                        break

                time.sleep(1.0 / config.DASHBOARD_REFRESH_RATE)

            ws.close()
        except Exception as e:
            self._connected = False
            raise

    def build_sensing_data(self, variance, presence, counter, vitals,
                           tracker) -> dict:
        """Build the data payload for WebSocket transmission."""
        pres = presence.state
        count = counter.result
        vit = vitals.result

        data = {
            "presence": {
                "detected": pres.present,
                "confidence": pres.confidence,
                "duration": pres.duration,
                "label": "RSSI-based estimate",
            },
            "motion": {
                "score": round(variance.motion_score, 4),
                "threshold": config.PRESENCE_THRESHOLD,
            },
            "occupancy": {
                "count": count.count,
                "display": count.display,
                "confidence": count.confidence,
                "label": count.label,
            },
            "vitals": {
                "breathing_bpm": vit.bpm if vit.is_reportable else None,
                "confidence": vit.confidence,
                "label": vit.label,
            },
            "environment": {
                "ap_count": tracker.ap_count,
                "client_count": tracker.client_count,
                "total_macs": tracker.total_count,
            },
            "zones": variance.zones,
        }

        return data
