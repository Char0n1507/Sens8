"""
Sens8 — Packet Capture & RSSI Extraction
Supports both managed-mode scanning (safe) and monitor-mode sniffing.

Managed-mode uses `iw dev scan` which preserves WiFi connectivity.
"""

import time
import logging
import subprocess
import re
import threading
from typing import Optional, Callable

from capture.devices import DeviceTracker
from capture.monitor import CaptureMode
import config

logger = logging.getLogger("sens8.capture.scanner")


class ManagedScanner:
    """
    Primary scanner — uses `iw dev <iface> scan` in managed mode.
    SAFE: does not disrupt WiFi connection.

    Uses `iw dev scan dump` for fast reads of cached scan results,
    with periodic full `iw dev scan` triggers.
    """

    def __init__(self, interface: str, device_tracker: DeviceTracker):
        self.interface = interface
        self.tracker = device_tracker
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._packet_count = 0
        self._start_time = 0.0
        self._scan_count = 0
        self._callbacks: list[Callable] = []

    @property
    def packet_count(self) -> int:
        return self._packet_count

    @property
    def packets_per_second(self) -> float:
        elapsed = time.time() - self._start_time
        return self._packet_count / elapsed if elapsed > 0 else 0.0

    @property
    def scan_count(self) -> int:
        return self._scan_count

    def add_callback(self, cb: Callable):
        self._callbacks.append(cb)

    def start(self):
        """Start scanning in background thread."""
        self._running = True
        self._start_time = time.time()
        self._thread = threading.Thread(
            target=self._scan_loop, daemon=True, name="managed-scanner"
        )
        self._thread.start()

        # Start a background ping to the gateway to ensure continuous radio traffic
        # This is REQUIRED in managed mode because without active traffic, there are
        # no radio waves to bounce off moving bodies for station dump to measure.
        self._ping_thread = threading.Thread(
            target=self._active_ping_loop, daemon=True, name="active-ping"
        )
        self._ping_thread.start()
        logger.info("✓ Managed-mode scanner running (WiFi preserved)")

    def _active_ping_loop(self):
        """Ping the gateway continuously to force Wi-Fi packet exchange."""
        try:
            # Find default gateway
            result = subprocess.run(
                "ip route show default | awk '{print $3}'",
                shell=True, capture_output=True, text=True
            )
            gw = result.stdout.strip()
            if not gw:
                return
            
            logger.info(f"Starting active ping to gateway {gw} for continuous sensing...")
            while self._running:
                subprocess.run(
                    f"ping -c 1 -W 1 {gw} >/dev/null 2>&1",
                    shell=True
                )
                time.sleep(0.1)  # 10 packets per second
        except Exception:
            pass

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=8)
        logger.info(f"Scanner stopped. {self._packet_count} readings, {self._scan_count} scans.")

    def _scan_loop(self):
        """Main scan loop: trigger scans and parse results."""
        scan_interval = config.MANAGED_SCAN_INTERVAL
        dump_interval = config.MANAGED_DUMP_INTERVAL

        last_full_scan = 0
        iteration = 0

        while self._running:
            now = time.time()
            iteration += 1

            try:
                # Trigger a full scan periodically
                if now - last_full_scan >= scan_interval:
                    self._trigger_scan()
                    last_full_scan = now
                    self._scan_count += 1

                # Read cached scan results (fast, non-blocking)
                self._read_scan_dump()

            except Exception as e:
                logger.debug(f"Scan iteration error: {e}")

            # Always collect station info for connected AP (real-time high-freq RSSI)
            self._read_station_dump()

            time.sleep(dump_interval)

    def _trigger_scan(self):
        """Trigger a new scan asynchronously. Non-blocking — results read from dump."""
        def _do_scan():
            try:
                result = subprocess.run(
                    f"iw dev {self.interface} scan trigger 2>/dev/null",
                    shell=True, capture_output=True, text=True, timeout=3
                )
                if result.returncode != 0:
                    subprocess.run(
                        f"iw dev {self.interface} scan 2>/dev/null",
                        shell=True, capture_output=True, text=True, timeout=6
                    )
            except Exception as e:
                logger.debug(f"Scan trigger error: {e}")

        threading.Thread(target=_do_scan, daemon=True, name="iw-scan-trigger").start()


    def _read_scan_dump(self):
        """Read cached scan results from last scan."""
        try:
            result = subprocess.run(
                f"iw dev {self.interface} scan dump 2>/dev/null",
                shell=True, capture_output=True, text=True, timeout=5
            )
            if result.returncode != 0:
                return

            self._parse_iw_scan(result.stdout)
        except Exception as e:
            logger.debug(f"Scan dump error: {e}")

    def _read_station_dump(self):
        """Read station dump for connected AP — very accurate RSSI."""
        try:
            result = subprocess.run(
                f"iw dev {self.interface} station dump 2>/dev/null",
                shell=True, capture_output=True, text=True, timeout=3
            )
            if result.returncode != 0 or not result.stdout.strip():
                return

            now = time.time()
            mac = None
            rssi = None

            for line in result.stdout.splitlines():
                line = line.strip()

                m = re.match(r"Station\s+([\da-fA-F:]+)", line)
                if m:
                    mac = m.group(1).lower()
                    continue

                # Signal with averaging
                m = re.search(r"signal avg:\s*(-?\d+)", line)
                if m and mac:
                    rssi = int(m.group(1))
                    continue

                m = re.search(r"signal:\s*(-?\d+)", line)
                if m and mac and rssi is None:
                    rssi = int(m.group(1))

            if mac and rssi is not None:
                self._emit({
                    "mac": mac,
                    "ssid": "",
                    "rssi": rssi,
                    "timestamp": now,
                    "channel": 0,
                    "frame_type": "station",
                })

        except Exception:
            pass

    def _parse_iw_scan(self, output: str):
        """Parse `iw scan` output into device records."""
        now = time.time()
        current = {}

        for line in output.splitlines():
            line_stripped = line.strip()

            # New BSS entry
            m = re.match(r"BSS\s+([\da-fA-F:]+)", line_stripped)
            if m:
                if current.get("mac") and current.get("rssi") is not None:
                    self._emit(current)
                current = {
                    "mac": m.group(1).lower(),
                    "ssid": "",
                    "rssi": None,
                    "timestamp": now,
                    "channel": 0,
                    "frame_type": "beacon",
                }
                continue

            if not current:
                continue

            # Signal strength
            m = re.search(r"signal:\s*(-?[\d.]+)\s*dBm", line_stripped)
            if m:
                current["rssi"] = int(float(m.group(1)))
                continue

            # SSID
            m = re.search(r"SSID:\s*(.+)", line_stripped)
            if m:
                ssid = m.group(1).strip()
                if ssid and ssid != "\\x00" and not ssid.startswith("\\x"):
                    current["ssid"] = ssid
                continue

            # Channel from DS Parameter Set
            m = re.search(r"DS Parameter set: channel\s+(\d+)", line_stripped)
            if m:
                current["channel"] = int(m.group(1))
                continue

            # Primary channel
            m = re.search(r"primary channel:\s+(\d+)", line_stripped)
            if m and current.get("channel", 0) == 0:
                current["channel"] = int(m.group(1))
                continue

            # Frequency to channel
            m = re.search(r"freq:\s+(\d+)", line_stripped)
            if m and current.get("channel", 0) == 0:
                current["channel"] = self._freq_to_channel(int(m.group(1)))

        # Last entry
        if current.get("mac") and current.get("rssi") is not None:
            self._emit(current)

    @staticmethod
    def _freq_to_channel(freq: int) -> int:
        if 2412 <= freq <= 2484:
            return (freq - 2407) // 5 if freq != 2484 else 14
        elif 5170 <= freq <= 5825:
            return (freq - 5000) // 5
        return 0

    def _emit(self, datum: dict):
        """Emit a scan result to tracker and callbacks."""
        self.tracker.update(datum)
        self._packet_count += 1
        for cb in self._callbacks:
            try:
                cb(datum)
            except Exception:
                pass


class MonitorScanner:
    """
    Monitor-mode scanner using Scapy — captures raw 802.11 frames.
    Only used when monitor mode is explicitly enabled.
    """

    def __init__(self, interface: str, device_tracker: DeviceTracker):
        self.interface = interface
        self.tracker = device_tracker
        self._sniffer = None
        self._running = False
        self._packet_count = 0
        self._start_time = 0.0
        self._callbacks: list[Callable] = []

    @property
    def packet_count(self) -> int:
        return self._packet_count

    @property
    def packets_per_second(self) -> float:
        elapsed = time.time() - self._start_time
        return self._packet_count / elapsed if elapsed > 0 else 0.0

    @property
    def scan_count(self) -> int:
        return 0

    def add_callback(self, cb: Callable):
        self._callbacks.append(cb)

    def start(self):
        if self._running:
            return

        from scapy.all import AsyncSniffer, conf
        conf.verb = 0

        logger.info(f"Starting monitor capture on {self.interface}")
        self._start_time = time.time()
        self._running = True

        self._sniffer = AsyncSniffer(
            iface=self.interface,
            prn=self._process_packet,
            store=False,
            monitor=True,
        )
        self._sniffer.start()
        logger.info("✓ Monitor-mode capture running")

    def stop(self):
        self._running = False
        if self._sniffer:
            try:
                self._sniffer.stop()
            except Exception:
                pass
            self._sniffer = None

    def _process_packet(self, packet):
        if not self._running:
            return

        from scapy.all import RadioTap, Dot11, Dot11Beacon, Dot11Elt, Dot11ProbeReq, Dot11ProbeResp

        if not packet.haslayer(Dot11):
            return

        # Extract RSSI
        rssi = None
        if packet.haslayer(RadioTap):
            try:
                rssi = packet[RadioTap].dBm_AntSignal
                if rssi is not None and not (-120 <= rssi <= 0):
                    rssi = None
            except (AttributeError, TypeError):
                pass

        if rssi is None:
            return

        dot11 = packet[Dot11]
        src_mac = dot11.addr2
        if not src_mac or src_mac == "ff:ff:ff:ff:ff:ff":
            return

        # SSID extraction
        ssid = ""
        try:
            if packet.haslayer(Dot11Elt):
                elt = packet[Dot11Elt]
                while elt:
                    if elt.ID == 0:
                        s = elt.info.decode("utf-8", errors="ignore").strip()
                        if s:
                            ssid = s
                            break
                    elt = elt.payload.getlayer(Dot11Elt)
        except Exception:
            pass

        # Channel from RadioTap
        channel = 0
        try:
            freq = packet[RadioTap].ChannelFrequency
            if freq:
                channel = ManagedScanner._freq_to_channel(freq)
        except (AttributeError, TypeError):
            pass

        # Frame type
        frame_type = "other"
        if packet.haslayer(Dot11Beacon):
            frame_type = "beacon"
        elif packet.haslayer(Dot11ProbeResp):
            frame_type = "probe_resp"
        elif packet.haslayer(Dot11ProbeReq):
            frame_type = "probe_req"

        self._packet_count += 1
        datum = {
            "mac": src_mac.lower(),
            "ssid": ssid,
            "rssi": int(rssi),
            "timestamp": time.time(),
            "channel": channel,
            "frame_type": frame_type,
        }

        self.tracker.update(datum)
        for cb in self._callbacks:
            try:
                cb(datum)
            except Exception:
                pass
