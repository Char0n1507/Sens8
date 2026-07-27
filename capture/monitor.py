"""
Sens8 — Monitor Mode Management
Handles WiFi interface detection, monitor mode setup/teardown,
and channel hopping. Requires root or CAP_NET_RAW.
"""

import subprocess
import logging
import re
import signal
import atexit
import time
import threading
from typing import Optional, Tuple, List

import config

logger = logging.getLogger("sens8.capture.monitor")

# ─── Module State ─────────────────────────────────────────────────────
_original_mode: Optional[str] = None
_interface: Optional[str] = None
_hopper_thread: Optional[threading.Thread] = None
_hopper_stop = threading.Event()


def _run(cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a shell command, log it, return result."""
    logger.debug(f"exec: {cmd}")
    return subprocess.run(
        cmd, shell=True, capture_output=True, text=True,
        check=check, timeout=15
    )


# ─── Interface Detection ─────────────────────────────────────────────

def detect_interface() -> str:
    """Auto-detect a WiFi interface from `iw dev` output."""
    try:
        result = _run("iw dev", check=False)
        interfaces = re.findall(r"Interface\s+(\S+)", result.stdout)
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

    # Fallback: try lshw
    try:
        result = _run("lshw -class network -short 2>/dev/null", check=False)
        for line in result.stdout.splitlines():
            if iface in line:
                return line.strip()
    except Exception:
        pass

    return "unknown"


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
            # Match lines like: "* 2412 MHz [1] (20.0 dBm)"
            m = re.search(r"\*\s+(\d+)\s+MHz\s+\[(\d+)\]", line)
            if m and "disabled" not in line.lower() and "no IR" not in line:
                freq = int(m.group(1))
                chan = int(m.group(2))
                if 2400 <= freq <= 2500:
                    channels_24.append(chan)
                elif 5100 <= freq <= 5900:
                    channels_5.append(chan)
    except Exception as e:
        logger.warning(f"Channel detection failed: {e}")

    # Defaults if detection fails
    if not channels_24:
        channels_24 = config.CHANNELS_24GHZ[:]
    if not channels_5:
        channels_5 = []  # Don't assume 5GHz support

    return channels_24, channels_5


def supports_monitor_mode(iface: str) -> bool:
    """Check if the interface supports monitor mode."""
    try:
        phy = get_phy_for_interface(iface)
        result = _run(f"iw phy {phy} info", check=False)
        return "monitor" in result.stdout.lower()
    except Exception:
        return False


# ─── Monitor Mode Management ─────────────────────────────────────────

def get_current_mode(iface: str) -> str:
    """Get current interface mode (managed, monitor, etc.)."""
    result = _run(f"iw dev {iface} info", check=False)
    match = re.search(r"type\s+(\S+)", result.stdout)
    return match.group(1) if match else "unknown"


def enable_monitor_mode(iface: str) -> bool:
    """Put interface into monitor mode. Returns True on success."""
    global _original_mode, _interface
    _interface = iface
    _original_mode = get_current_mode(iface)

    if _original_mode == "monitor":
        logger.info(f"{iface} already in monitor mode")
        return True

    if not supports_monitor_mode(iface):
        logger.warning(f"{iface} does not support monitor mode")
        return False

    logger.info(f"Enabling monitor mode on {iface} (was: {_original_mode})")

    try:
        # Kill processes that might interfere
        _run("airmon-ng check kill 2>/dev/null", check=False)

        # Standard method: down → set monitor → up
        _run(f"ip link set {iface} down")
        _run(f"iw dev {iface} set type monitor")
        _run(f"ip link set {iface} up")

        # Verify
        current = get_current_mode(iface)
        if current == "monitor":
            logger.info(f"✓ {iface} now in monitor mode")
            _register_cleanup()
            return True
        else:
            logger.error(f"Failed: mode is '{current}' after set")
            _restore_managed(iface)
            return False

    except subprocess.CalledProcessError as e:
        logger.error(f"Monitor mode failed: {e.stderr}")
        _restore_managed(iface)
        return False


def _restore_managed(iface: str):
    """Restore interface to managed mode."""
    try:
        _run(f"ip link set {iface} down", check=False)
        _run(f"iw dev {iface} set type managed", check=False)
        _run(f"ip link set {iface} up", check=False)
        logger.info(f"✓ {iface} restored to managed mode")
    except Exception as e:
        logger.error(f"Failed to restore managed mode: {e}")


def cleanup():
    """Restore interface on exit — called via atexit and signal handlers."""
    global _interface
    stop_channel_hopping()
    if _interface:
        logger.info(f"Cleaning up: restoring {_interface}")
        _restore_managed(_interface)
        _interface = None


def _register_cleanup():
    """Register cleanup handlers for graceful shutdown."""
    atexit.register(cleanup)

    def _signal_handler(signum, frame):
        logger.info(f"Caught signal {signum}, cleaning up...")
        cleanup()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)


# ─── Channel Hopping ─────────────────────────────────────────────────

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
    """Start channel hopping in a background thread."""
    global _hopper_thread

    if not config.CHANNEL_HOP:
        return

    channels_24, channels_5 = get_supported_channels(iface)
    # Use 2.4GHz primarily (more devices visible)
    channels = [c for c in config.CHANNELS_24GHZ if c in channels_24]
    if not channels:
        channels = channels_24[:3] if channels_24 else [1, 6, 11]

    # Add 5GHz if supported
    supported_5 = [c for c in config.CHANNELS_5GHZ if c in channels_5]
    if supported_5:
        channels.extend(supported_5[:2])  # Don't add too many

    logger.info(f"Channel hopping: {channels} (interval={config.CHANNEL_HOP_INTERVAL}s)")

    _hopper_stop.clear()
    _hopper_thread = threading.Thread(
        target=_channel_hop_loop,
        args=(iface, channels, config.CHANNEL_HOP_INTERVAL),
        daemon=True,
        name="channel-hopper"
    )
    _hopper_thread.start()


def stop_channel_hopping():
    """Stop the channel hopping thread."""
    global _hopper_thread
    _hopper_stop.set()
    if _hopper_thread and _hopper_thread.is_alive():
        _hopper_thread.join(timeout=2)
    _hopper_thread = None
