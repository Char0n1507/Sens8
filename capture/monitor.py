"""
Sens8 — WiFi Interface Management
Safe interface handling that NEVER disrupts your existing WiFi connection.

Strategy:
  1. Default: managed-mode scanning (iw dev scan) — always safe
  2. Virtual monitor (mon0): only if card supports managed+monitor combo
  3. Direct monitor: only with --force-monitor flag (will disconnect WiFi)
  4. Always restores interface on exit
"""

import subprocess
import logging
import re
import signal
import atexit
import time
import threading
from typing import Optional, Tuple, List
from enum import Enum

import config

logger = logging.getLogger("sens8.capture.monitor")


class CaptureMode(Enum):
    MANAGED_SCAN = "managed-scan"       # Safe: uses iw scan, no disconnection
    VIRTUAL_MONITOR = "virtual-monitor"  # mon0 alongside wlan0 (if supported)
    DIRECT_MONITOR = "direct-monitor"    # Converts interface (WILL disconnect)


# ─── Module State ─────────────────────────────────────────────────────
_original_mode: Optional[str] = None
_primary_interface: Optional[str] = None
_monitor_interface: Optional[str] = None  # mon0, etc.
_capture_mode: CaptureMode = CaptureMode.MANAGED_SCAN
_hopper_thread: Optional[threading.Thread] = None
_hopper_stop = threading.Event()
_cleanup_registered = False


def _run(cmd: str, check: bool = True, timeout: int = 15) -> subprocess.CompletedProcess:
    """Run a shell command, log it, return result."""
    logger.debug(f"exec: {cmd}")
    return subprocess.run(
        cmd, shell=True, capture_output=True, text=True,
        check=check, timeout=timeout
    )


# ─── Interface Detection ─────────────────────────────────────────────

def detect_interface() -> str:
    """Auto-detect a WiFi interface from `iw dev` output."""
    try:
        result = _run("iw dev", check=False)
        interfaces = re.findall(r"Interface\s+(\S+)", result.stdout)

        # Filter out monitor interfaces we may have created
        interfaces = [i for i in interfaces if not i.startswith("mon")]

        if not interfaces:
            raise RuntimeError("No WiFi interfaces found via `iw dev`")

        # Prefer wlan0, wlan1, then first found
        for preferred in ["wlan0", "wlan1"]:
            if preferred in interfaces:
                logger.info(f"Auto-detected interface: {preferred}")
                return preferred

        chosen = interfaces[0]
        logger.info(f"Auto-detected interface: {chosen}")
        return chosen
    except FileNotFoundError:
        raise RuntimeError("`iw` not found — install with: apt install iw")


def get_phy_for_interface(iface: str) -> str:
    """Get the phy name for a given interface."""
    result = _run(f"iw dev {iface} info", check=False)
    match = re.search(r"wiphy\s+(\d+)", result.stdout)
    if match:
        return f"phy{match.group(1)}"
    raise RuntimeError(f"Cannot determine phy for {iface}")


def get_chipset_info(iface: str) -> str:
    """Get chipset/driver info for the interface."""
    try:
        result = _run(f"ethtool -i {iface}", check=False)
        driver_match = re.search(r"driver:\s+(\S+)", result.stdout)
        if driver_match:
            return driver_match.group(1)
    except Exception:
        pass
    return "unknown"


def get_connected_ssid(iface: str) -> str:
    """Get currently connected SSID."""
    try:
        result = _run(f"iw dev {iface} info", check=False)
        match = re.search(r"ssid\s+(.+)", result.stdout)
        if match:
            return match.group(1).strip()
    except Exception:
        pass
    return ""


def get_current_mode(iface: str) -> str:
    """Get current interface mode (managed, monitor, etc.)."""
    result = _run(f"iw dev {iface} info", check=False)
    match = re.search(r"type\s+(\S+)", result.stdout)
    return match.group(1) if match else "unknown"


def get_current_channel(iface: str) -> int:
    """Get current channel of the interface."""
    result = _run(f"iw dev {iface} info", check=False)
    match = re.search(r"channel\s+(\d+)", result.stdout)
    return int(match.group(1)) if match else 0


def supports_monitor_mode(iface: str) -> bool:
    """Check if the interface supports monitor mode."""
    try:
        phy = get_phy_for_interface(iface)
        result = _run(f"iw phy {phy} info", check=False)
        return "monitor" in result.stdout.lower()
    except Exception:
        return False


def supports_virtual_monitor(iface: str) -> bool:
    """
    Check if the card supports running managed + monitor simultaneously.
    Most Intel iwlwifi cards do NOT support this combo.
    """
    try:
        phy = get_phy_for_interface(iface)
        result = _run(f"iw phy {phy} info", check=False)

        # Parse valid interface combinations
        combo_section = False
        for line in result.stdout.splitlines():
            if "valid interface combinations" in line:
                combo_section = True
                continue
            if combo_section:
                if line.strip().startswith("*"):
                    # Check if this combo includes both managed and monitor
                    if "managed" in line and "monitor" in line:
                        return True
                elif not line.strip().startswith("#") and line.strip():
                    combo_section = False
    except Exception:
        pass

    return False


def get_supported_channels(iface: str) -> Tuple[List[int], List[int]]:
    """Return (2.4GHz channels, 5GHz channels) the card supports."""
    channels_24 = []
    channels_5 = []

    try:
        phy = get_phy_for_interface(iface)
        result = _run(f"iw phy {phy} channels", check=False)
        if result.returncode != 0:
            result = _run(f"iw phy {phy} info", check=False)

        for line in result.stdout.splitlines():
            m = re.search(r"\*\s+(\d+)\s+MHz\s+\[(\d+)\]", line)
            if m and "disabled" not in line.lower():
                freq = int(m.group(1))
                chan = int(m.group(2))
                if 2400 <= freq <= 2500:
                    channels_24.append(chan)
                elif 5100 <= freq <= 5900:
                    channels_5.append(chan)
    except Exception as e:
        logger.warning(f"Channel detection failed: {e}")

    if not channels_24:
        channels_24 = config.CHANNELS_24GHZ[:]

    return channels_24, channels_5


# ─── Capture Mode Setup ──────────────────────────────────────────────

def setup_capture(iface: str, force_monitor: bool = False) -> Tuple[CaptureMode, str]:
    """
    Set up the best available capture mode WITHOUT disrupting WiFi.

    Returns (mode, capture_interface) where capture_interface is the
    interface to sniff on.

    Priority:
      1. If force_monitor: convert interface (WILL disconnect WiFi)
      2. If card supports managed+monitor: create virtual mon0
      3. Default: managed-mode scanning (always safe)
    """
    global _primary_interface, _monitor_interface, _capture_mode, _original_mode
    _primary_interface = iface
    _original_mode = get_current_mode(iface)

    _register_cleanup()

    if force_monitor:
        logger.warning("⚠ --force-monitor: WiFi connection WILL be disconnected")
        ok = _enable_direct_monitor(iface)
        if ok:
            _capture_mode = CaptureMode.DIRECT_MONITOR
            _monitor_interface = iface
            return CaptureMode.DIRECT_MONITOR, iface
        else:
            logger.error("Direct monitor mode failed, falling back to managed scan")

    if supports_virtual_monitor(iface):
        mon_iface = _create_virtual_monitor(iface)
        if mon_iface:
            _capture_mode = CaptureMode.VIRTUAL_MONITOR
            _monitor_interface = mon_iface
            return CaptureMode.VIRTUAL_MONITOR, mon_iface

    # Safe default: managed-mode scanning
    logger.info("Using managed-mode scanning (WiFi connection preserved)")
    _capture_mode = CaptureMode.MANAGED_SCAN
    return CaptureMode.MANAGED_SCAN, iface


def _create_virtual_monitor(iface: str) -> Optional[str]:
    """Create a virtual monitor interface (mon0) alongside the primary."""
    mon_name = "mon0"
    try:
        # Remove if exists from previous run
        _run(f"iw dev {mon_name} del", check=False)
        time.sleep(0.3)

        _run(f"iw dev {iface} interface add {mon_name} type monitor")
        _run(f"ip link set {mon_name} up")

        # Verify
        mode = get_current_mode(mon_name)
        if mode == "monitor":
            logger.info(f"✓ Virtual monitor interface {mon_name} created")
            return mon_name
        else:
            logger.warning(f"Virtual monitor setup failed (mode={mode})")
            _run(f"iw dev {mon_name} del", check=False)
    except Exception as e:
        logger.warning(f"Virtual monitor failed: {e}")
        _run(f"iw dev {mon_name} del", check=False)

    return None


def _enable_direct_monitor(iface: str) -> bool:
    """Convert interface to monitor mode. WARNING: disconnects WiFi."""
    try:
        # Tell NetworkManager to ignore this interface so it doesn't kill it
        _run(f"nmcli dev set {iface} managed no", check=False)
        time.sleep(0.5)

        _run(f"ip link set {iface} down")
        _run(f"iw dev {iface} set type monitor")
        _run(f"ip link set {iface} up")

        mode = get_current_mode(iface)
        if mode == "monitor":
            logger.info(f"✓ {iface} in direct monitor mode (NetworkManager bypassed)")
            return True
        else:
            logger.error(f"Monitor mode failed (mode={mode}), restoring...")
            _restore_interface(iface)
            return False
    except Exception as e:
        logger.error(f"Monitor mode error: {e}")
        _restore_interface(iface)
        return False


# ─── Cleanup & Restore ───────────────────────────────────────────────

def _restore_interface(iface: str):
    """Restore interface to managed mode and restart NetworkManager."""
    try:
        _run(f"ip link set {iface} down", check=False)
        _run(f"iw dev {iface} set type managed", check=False)
        _run(f"ip link set {iface} up", check=False)
        
        # Tell NetworkManager to manage it again
        _run(f"nmcli dev set {iface} managed yes", check=False)

        # Restart NetworkManager to reconnect
        _run("systemctl restart NetworkManager", check=False)

        logger.info(f"✓ {iface} restored to managed mode")

        # Wait for reconnection
        time.sleep(2)
        ssid = get_connected_ssid(iface)
        if ssid:
            logger.info(f"✓ Reconnected to: {ssid}")
    except Exception as e:
        logger.error(f"Restore failed: {e}")
        logger.error(f"  Run manually: sudo ip link set {iface} down && "
                      f"sudo iw dev {iface} set type managed && "
                      f"sudo ip link set {iface} up && "
                      f"sudo systemctl restart NetworkManager")


def cleanup():
    """Restore everything on exit."""
    global _monitor_interface, _primary_interface

    stop_channel_hopping()

    if _capture_mode == CaptureMode.VIRTUAL_MONITOR and _monitor_interface:
        try:
            _run(f"ip link set {_monitor_interface} down", check=False)
            _run(f"iw dev {_monitor_interface} del", check=False)
            logger.info(f"✓ Removed virtual monitor interface {_monitor_interface}")
        except Exception as e:
            logger.error(f"Virtual monitor cleanup error: {e}")
        _monitor_interface = None

    elif _capture_mode == CaptureMode.DIRECT_MONITOR and _primary_interface:
        logger.info(f"Restoring {_primary_interface} to managed mode...")
        _restore_interface(_primary_interface)
        _primary_interface = None


def _register_cleanup():
    """Register cleanup handlers."""
    global _cleanup_registered
    if _cleanup_registered:
        return
    _cleanup_registered = True

    atexit.register(cleanup)

    def _signal_handler(signum, frame):
        logger.info(f"Signal {signum} — cleaning up...")
        cleanup()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)


# ─── Channel Hopping (monitor mode only) ─────────────────────────────

def _channel_hop_loop(iface: str, channels: List[int], interval: float):
    """Background thread that hops between channels."""
    idx = 0
    while not _hopper_stop.is_set():
        chan = channels[idx % len(channels)]
        try:
            _run(f"iw dev {iface} set channel {chan}", check=False)
        except Exception:
            pass
        idx += 1
        _hopper_stop.wait(interval)


def start_channel_hopping(iface: str):
    """Start channel hopping (only for monitor mode interfaces)."""
    global _hopper_thread

    if not config.CHANNEL_HOP:
        return
    if _capture_mode == CaptureMode.MANAGED_SCAN:
        return  # Channel hopping not used in managed scan mode

    channels_24, channels_5 = get_supported_channels(
        _primary_interface or iface
    )

    channels = [c for c in config.CHANNELS_24GHZ if c in channels_24]
    if not channels:
        channels = channels_24[:3] if channels_24 else [1, 6, 11]

    supported_5 = [c for c in config.CHANNELS_5GHZ if c in channels_5]
    if supported_5:
        channels.extend(supported_5[:2])

    logger.info(f"Channel hopping: {channels}")

    _hopper_stop.clear()
    _hopper_thread = threading.Thread(
        target=_channel_hop_loop,
        args=(iface, channels, config.CHANNEL_HOP_INTERVAL),
        daemon=True, name="channel-hopper"
    )
    _hopper_thread.start()


def stop_channel_hopping():
    """Stop channel hopping."""
    global _hopper_thread
    _hopper_stop.set()
    if _hopper_thread and _hopper_thread.is_alive():
        _hopper_thread.join(timeout=2)
    _hopper_thread = None


def get_capture_mode() -> CaptureMode:
    """Get the current capture mode."""
    return _capture_mode
