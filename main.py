#!/usr/bin/env python3
"""
Sens8 — WiFi Sensing Daemon
Pure software WiFi sensing using RSSI-based detection.
Detects presence, motion, occupancy, and person count using ambient WiFi signals.

Usage:
    sudo python main.py [OPTIONS]

Requires root for monitor mode and raw packet capture.
"""

import os
import sys
import time
import signal
import logging
import argparse
import threading

from rich.console import Console

# ─── Ensure project root is in path ──────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from capture.monitor import (
    detect_interface, enable_monitor_mode, get_chipset_info,
    get_current_mode, supports_monitor_mode, start_channel_hopping,
    cleanup as monitor_cleanup,
)
from capture.devices import DeviceTracker
from capture.scanner import PacketScanner, ManagedModeScanner
from processing.baseline import BaselineCalibrator
from processing.variance import VarianceAnalyzer
from processing.presence import PresenceDetector
from processing.counter import PersonCounter
from processing.vitals import VitalsEstimator
from synthetic.csi_frame import CSIFrameBuilder
from synthetic.udp_sender import UDPSender
from output.dashboard import Dashboard, show_startup_banner
from output.websocket import WebSocketOutput


# ─── Logging Setup ────────────────────────────────────────────────────
def setup_logging(level: str = "INFO", log_file: str = None):
    """Configure logging."""
    fmt = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
    handlers = [logging.StreamHandler(sys.stderr)]

    if log_file:
        handlers.append(logging.FileHandler(log_file))

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=fmt,
        handlers=handlers,
    )
    # Quiet noisy loggers
    logging.getLogger("scapy").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


# ─── CLI Arguments ────────────────────────────────────────────────────
def parse_args():
    parser = argparse.ArgumentParser(
        description="Sens8 — WiFi Sensing Daemon (RSSI-based)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  sudo python main.py                         # auto-detect interface
  sudo python main.py -i wlan1                # specify interface
  sudo python main.py --no-dashboard          # headless mode
  sudo python main.py --no-ruview             # skip RuView integration
  sudo python main.py --log-level DEBUG       # verbose logging
  sudo python main.py --baseline-duration 60  # longer calibration
        """,
    )

    parser.add_argument(
        "-i", "--interface",
        default=None,
        help="WiFi interface (default: auto-detect)",
    )
    parser.add_argument(
        "--no-dashboard",
        action="store_true",
        help="Disable terminal dashboard (headless mode)",
    )
    parser.add_argument(
        "--no-ruview",
        action="store_true",
        help="Disable RuView UDP/WS integration",
    )
    parser.add_argument(
        "--no-channel-hop",
        action="store_true",
        help="Disable channel hopping",
    )
    parser.add_argument(
        "--baseline-duration",
        type=int,
        default=config.BASELINE_DURATION,
        help=f"Baseline calibration duration in seconds (default: {config.BASELINE_DURATION})",
    )
    parser.add_argument(
        "--log-level",
        default=config.LOG_LEVEL,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="Log to file",
    )
    parser.add_argument(
        "--ruview-host",
        default=config.RUVIEW_HOST,
        help=f"RuView host (default: {config.RUVIEW_HOST})",
    )
    parser.add_argument(
        "--ruview-port",
        type=int,
        default=config.RUVIEW_UDP_PORT,
        help=f"RuView UDP port (default: {config.RUVIEW_UDP_PORT})",
    )

    return parser.parse_args()


# ─── Root Check ───────────────────────────────────────────────────────
def check_root():
    """Verify running as root."""
    if os.geteuid() != 0:
        print("\n[!] Sens8 requires root privileges for monitor mode and packet capture.")
        print("    Run with: sudo python main.py\n")
        sys.exit(1)


# ─── Main Orchestration ──────────────────────────────────────────────
def main():
    args = parse_args()

    # Apply CLI overrides to config
    if args.no_channel_hop:
        config.CHANNEL_HOP = False
    config.RUVIEW_HOST = args.ruview_host
    config.RUVIEW_UDP_PORT = args.ruview_port

    setup_logging(args.log_level, args.log_file)
    logger = logging.getLogger("sens8.main")

    console = Console()
    check_root()

    # ─── Interface Detection ──────────────────────────────────
    interface = args.interface
    if interface is None:
        try:
            interface = detect_interface()
        except RuntimeError as e:
            console.print(f"[bold red]✗ {e}[/bold red]")
            sys.exit(1)

    chipset = get_chipset_info(interface)
    logger.info(f"Interface: {interface}, Chipset: {chipset}")

    # ─── Monitor Mode ────────────────────────────────────────
    monitor_ok = False
    if supports_monitor_mode(interface):
        monitor_ok = enable_monitor_mode(interface)

    mode = get_current_mode(interface)

    if not monitor_ok:
        console.print(
            f"[yellow]⚠ Monitor mode unavailable on {interface}. "
            f"Falling back to managed mode scanning.[/yellow]"
        )
        mode = "managed (fallback)"

    # Show banner
    show_startup_banner(console, interface, chipset, mode)

    # ─── Initialize Components ────────────────────────────────
    tracker = DeviceTracker()
    baseline = BaselineCalibrator(tracker)
    variance = VarianceAnalyzer(tracker, baseline)
    presence = PresenceDetector()
    counter = PersonCounter(variance)
    vitals = VitalsEstimator(tracker)

    # Scanner
    if monitor_ok:
        scanner = PacketScanner(interface, tracker)
    else:
        scanner = ManagedModeScanner(interface, tracker)

    # Synthetic CSI
    frame_builder = CSIFrameBuilder(variance, presence, counter)
    udp_sender = UDPSender(frame_builder)

    # WebSocket output
    ws_output = WebSocketOutput()

    # Dashboard
    dashboard = None
    if not args.no_dashboard:
        dashboard = Dashboard(
            interface=interface,
            chipset=chipset,
            mode=mode,
            tracker=tracker,
            variance=variance,
            presence=presence,
            counter=counter,
            vitals=vitals,
            baseline=baseline,
            udp_sender=udp_sender,
        )

    # ─── Start Capture ────────────────────────────────────────
    console.print("[bold cyan]Starting packet capture...[/bold cyan]")
    scanner.start()

    if monitor_ok:
        start_channel_hopping(interface)

    # ─── Calibration Phase ────────────────────────────────────
    console.print(
        f"[bold yellow]Calibrating baseline ({args.baseline_duration}s)...[/bold yellow]"
    )
    baseline.calibrate(duration=args.baseline_duration)

    baselines = baseline.baselines
    console.print(
        f"[bold green]✓ Calibration complete: "
        f"{len(baselines)} APs baselined, "
        f"confidence: {baseline.confidence:.0%}[/bold green]"
    )

    # ─── Start Output Systems ─────────────────────────────────
    if not args.no_ruview:
        udp_sender.start()
        ws_output.start()
        console.print("[bold green]✓ RuView integration active[/bold green]")

    if dashboard:
        console.print("[bold cyan]Launching dashboard...[/bold cyan]")
        time.sleep(1)
        dashboard.start()

    # ─── Main Analysis Loop ──────────────────────────────────
    logger.info("Entering main analysis loop")
    stop_event = threading.Event()

    def _shutdown(signum=None, frame=None):
        logger.info("Shutdown requested...")
        stop_event.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    analysis_interval = 1.0 / config.DASHBOARD_REFRESH_RATE
    baseline_update_counter = 0
    baseline_update_ticks = int(
        config.BASELINE_UPDATE_INTERVAL / analysis_interval
    )

    try:
        while not stop_event.is_set():
            loop_start = time.monotonic()

            # Run analyses
            motion_score = variance.analyze()
            presence.update(motion_score)
            counter.update()
            vitals.update()

            # Periodic baseline update
            baseline_update_counter += 1
            if baseline_update_counter >= baseline_update_ticks:
                baseline.update_ema()
                baseline_update_counter = 0
                tracker.prune_stale()

            # WebSocket data update
            if not args.no_ruview and ws_output.is_connected:
                data = ws_output.build_sensing_data(
                    variance, presence, counter, vitals, tracker
                )
                ws_output.update_data(data)

            # Sleep to maintain rate
            elapsed = time.monotonic() - loop_start
            sleep_time = analysis_interval - elapsed
            if sleep_time > 0:
                stop_event.wait(sleep_time)

    except KeyboardInterrupt:
        pass
    finally:
        # ─── Cleanup ─────────────────────────────────────────
        logger.info("Shutting down Sens8...")

        if dashboard:
            dashboard.stop()

        scanner.stop()
        udp_sender.stop()
        ws_output.stop()
        monitor_cleanup()

        console.print("\n[bold green]✓ Sens8 shutdown complete[/bold green]")


if __name__ == "__main__":
    main()
