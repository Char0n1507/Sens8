"""
Sens8 — Scapy Packet Capture & RSSI Extraction
Captures 802.11 frames in monitor mode, extracts RSSI from RadioTap headers,
and feeds data into the device tracker.
"""

import time
import logging
import threading
from typing import Optional, Callable
from collections import defaultdict

from scapy.all import (
    AsyncSniffer, RadioTap, Dot11, Dot11Beacon, Dot11Elt,
    Dot11ProbeReq, Dot11ProbeResp, conf
)

from capture.devices import DeviceTracker
import config

logger = logging.getLogger("sens8.capture.scanner")

# Suppress Scapy warnings
conf.verb = 0


class PacketScanner:
    """
    Async packet sniffer for 802.11 frames.
    Extracts RSSI, MAC, SSID, channel, and frame type from captured packets.
    """

    def __init__(self, interface: str, device_tracker: DeviceTracker):
        self.interface = interface
        self.tracker = device_tracker
        self._sniffer: Optional[AsyncSniffer] = None
        self._running = False
        self._packet_count = 0
        self._start_time = 0.0
        self._callbacks: list[Callable] = []
        self._lock = threading.Lock()

    @property
    def packet_count(self) -> int:
        return self._packet_count

    @property
    def packets_per_second(self) -> float:
        elapsed = time.time() - self._start_time
        if elapsed <= 0:
            return 0.0
        return self._packet_count / elapsed

    def add_callback(self, cb: Callable):
        """Register a callback for each captured packet datum."""
        self._callbacks.append(cb)

    def start(self):
        """Start async packet capture."""
        if self._running:
            return

        logger.info(f"Starting packet capture on {self.interface}")
        self._start_time = time.time()
        self._packet_count = 0
        self._running = True

        self._sniffer = AsyncSniffer(
            iface=self.interface,
            prn=self._process_packet,
            store=False,
            filter="",  # Capture all — we filter in _process_packet
            monitor=True,
        )
        self._sniffer.start()
        logger.info("✓ Packet capture running")

    def stop(self):
        """Stop packet capture."""
        self._running = False
        if self._sniffer:
            try:
                self._sniffer.stop()
            except Exception:
                pass
            self._sniffer = None
        logger.info(f"Capture stopped. {self._packet_count} packets processed.")

    def _extract_rssi(self, packet) -> Optional[int]:
        """Extract RSSI (dBm) from RadioTap header."""
        if not packet.haslayer(RadioTap):
            return None

        try:
            # Primary: dBm_AntSignal field
            rssi = packet[RadioTap].dBm_AntSignal
            if rssi is not None:
                # Sanity check: valid RSSI range
                if -120 <= rssi <= 0:
                    return int(rssi)
        except (AttributeError, TypeError):
            pass

        try:
            # Fallback: some drivers put it in different fields
            if hasattr(packet[RadioTap], 'notdecoded'):
                ndata = packet[RadioTap].notdecoded
                if len(ndata) >= 14:
                    import struct
                    rssi = struct.unpack("b", bytes([ndata[14]]))[0]
                    if -120 <= rssi <= 0:
                        return rssi
        except (AttributeError, IndexError, TypeError):
            pass

        return None

    def _extract_ssid(self, packet) -> str:
        """Extract SSID from beacon/probe response."""
        try:
            if packet.haslayer(Dot11Elt):
                elt = packet[Dot11Elt]
                while elt:
                    if elt.ID == 0:  # SSID element
                        ssid = elt.info.decode("utf-8", errors="ignore").strip()
                        if ssid and len(ssid) > 0:
                            return ssid
                    elt = elt.payload.getlayer(Dot11Elt)
        except Exception:
            pass
        return ""

    def _extract_channel(self, packet) -> int:
        """Extract channel from RadioTap or Dot11 IE."""
        try:
            if hasattr(packet[RadioTap], 'ChannelFrequency'):
                freq = packet[RadioTap].ChannelFrequency
                if freq:
                    return self._freq_to_channel(freq)
        except (AttributeError, TypeError):
            pass

        # Try DS Parameter Set IE
        try:
            if packet.haslayer(Dot11Elt):
                elt = packet[Dot11Elt]
                while elt:
                    if elt.ID == 3 and len(elt.info) >= 1:  # DS Parameter Set
                        return elt.info[0]
                    elt = elt.payload.getlayer(Dot11Elt)
        except Exception:
            pass

        return 0

    @staticmethod
    def _freq_to_channel(freq: int) -> int:
        """Convert frequency (MHz) to channel number."""
        if 2412 <= freq <= 2484:
            if freq == 2484:
                return 14
            return (freq - 2407) // 5
        elif 5170 <= freq <= 5825:
            return (freq - 5000) // 5
        return 0

    def _classify_frame(self, packet) -> str:
        """Classify the 802.11 frame type."""
        if packet.haslayer(Dot11Beacon):
            return "beacon"
        elif packet.haslayer(Dot11ProbeResp):
            return "probe_resp"
        elif packet.haslayer(Dot11ProbeReq):
            return "probe_req"
        elif packet.haslayer(Dot11):
            frame_type = packet[Dot11].type
            if frame_type == 0:
                return "management"
            elif frame_type == 1:
                return "control"
            elif frame_type == 2:
                return "data"
        return "other"

    def _process_packet(self, packet):
        """Process a single captured packet."""
        if not self._running:
            return

        if not packet.haslayer(Dot11):
            return

        rssi = self._extract_rssi(packet)
        if rssi is None:
            return

        # Get MAC addresses
        dot11 = packet[Dot11]
        src_mac = dot11.addr2  # Transmitter address
        if not src_mac or src_mac == "ff:ff:ff:ff:ff:ff":
            return

        src_mac = src_mac.lower()
        ssid = self._extract_ssid(packet)
        channel = self._extract_channel(packet)
        frame_type = self._classify_frame(packet)

        now = time.time()
        self._packet_count += 1

        # Build datum
        datum = {
            "mac": src_mac,
            "ssid": ssid,
            "rssi": rssi,
            "timestamp": now,
            "channel": channel,
            "frame_type": frame_type,
        }

        # Feed to device tracker
        self.tracker.update(datum)

        # Fire callbacks
        for cb in self._callbacks:
            try:
                cb(datum)
            except Exception as e:
                logger.debug(f"Callback error: {e}")


class ManagedModeScanner:
    """
    Fallback scanner for cards that don't support monitor mode.
    Uses `iwlist scan` periodically to get AP list with RSSI.
    """

    def __init__(self, interface: str, device_tracker: DeviceTracker):
        self.interface = interface
        self.tracker = device_tracker
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._packet_count = 0
        self._start_time = 0.0

    @property
    def packet_count(self) -> int:
        return self._packet_count

    @property
    def packets_per_second(self) -> float:
        elapsed = time.time() - self._start_time
        if elapsed <= 0:
            return 0.0
        return self._packet_count / elapsed

    def start(self):
        """Start periodic managed-mode scanning."""
        self._running = True
        self._start_time = time.time()
        self._thread = threading.Thread(
            target=self._scan_loop, daemon=True, name="managed-scanner"
        )
        self._thread.start()
        logger.info("✓ Managed-mode scanner running (fallback)")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    def _scan_loop(self):
        """Periodically run iwlist scan."""
        import subprocess
        import re

        while self._running:
            try:
                result = subprocess.run(
                    f"iwlist {self.interface} scan 2>/dev/null",
                    shell=True, capture_output=True, text=True, timeout=10
                )

                now = time.time()
                current_mac = None
                current_ssid = ""
                current_rssi = None
                current_channel = 0

                for line in result.stdout.splitlines():
                    line = line.strip()

                    # Cell with MAC
                    m = re.search(r"Cell \d+ - Address: ([\da-fA-F:]+)", line)
                    if m:
                        # Save previous
                        if current_mac and current_rssi is not None:
                            self._emit(current_mac, current_ssid, current_rssi,
                                       current_channel, now)
                        current_mac = m.group(1).lower()
                        current_ssid = ""
                        current_rssi = None
                        current_channel = 0
                        continue

                    # RSSI / signal level
                    m = re.search(r"Signal level[=:](-?\d+)", line)
                    if m:
                        current_rssi = int(m.group(1))
                        continue

                    # SSID
                    m = re.search(r'ESSID:"(.+?)"', line)
                    if m:
                        current_ssid = m.group(1)
                        continue

                    # Channel
                    m = re.search(r"Channel:(\d+)", line)
                    if m:
                        current_channel = int(m.group(1))

                # Last cell
                if current_mac and current_rssi is not None:
                    self._emit(current_mac, current_ssid, current_rssi,
                               current_channel, now)

            except Exception as e:
                logger.debug(f"Managed scan error: {e}")

            time.sleep(2)  # iwlist scan is slow

    def _emit(self, mac, ssid, rssi, channel, ts):
        """Emit a scan result as a datum."""
        datum = {
            "mac": mac,
            "ssid": ssid,
            "rssi": rssi,
            "timestamp": ts,
            "channel": channel,
            "frame_type": "beacon",
        }
        self.tracker.update(datum)
        self._packet_count += 1
