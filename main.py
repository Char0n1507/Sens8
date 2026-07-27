#!/usr/bin/env python3
"""
Sens8 — WiFi Sensing Daemon
Pure software WiFi sensing using RSSI-based detection.

DEFAULT MODE: Managed-mode scanning — WiFi stays connected.
Only uses monitor mode with --force-monitor flag.

Usage:
    sudo python3 main.py              # safe — WiFi stays connected
    sudo python3 main.py -i wlan1     # specify interface
    sudo python3 main.py --no-dashboard  # headless
    sudo python3 main.py --force-monitor # WILL disconnect WiFi
"""

import os
import sys
import time
import signal
import logging
import argparse
import threading

from rich.console import Console

# Ensure project root is in path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from capture.monitor import (
    detect_interface, setup_capture, get_chipset_info,
    get_current_mode, get_connected_ssid, start_channel_hopping,
    cleanup as monitor_cleanup, CaptureMode,
)
from capture.devices import DeviceTracker
from capture.scanner import ManagedScanner, MonitorScanner
from processing.baseline import BaselineCalibrator
from processing.variance import VarianceAnalyzer
from processing.presence import PresenceDetector
from processing.counter import PersonCounter
from processing.vitals import VitalsEstimator
from synthetic.csi_frame import CSIFrameBuilder
from synthetic.udp_sender import UDPSender
from output.dashboard import Dashboard, show_startup_banner
from output.websocket import WebSocketOutput


def setup_logging(level: str = "INFO", log_file: str = None):
    fmt = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
    handlers = [logging.StreamHandler(sys.stderr)]
    if log_file:
        handlers.append(logging.FileHandler(log_file))
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=fmt, handlers=handlers,
    )
    logging.getLogger("scapy").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Sens8 — WiFi Sensing Daemon (RSSI-based)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  sudo python3 main.py                    # auto-detect, WiFi stays connected
  sudo python3 main.py -i wlan1           # specify interface
  sudo python3 main.py --no-dashboard     # headless mode
  sudo python3 main.py --no-ruview        # no RuView integration
  sudo python3 main.py --force-monitor    # use monitor mode (disconnects WiFi!)
  sudo python3 main.py --log-level DEBUG  # verbose
        """,
    )

    parser.add_argument("-i", "--interface", default=None,
                        help="WiFi interface (default: auto-detect)")
    parser.add_argument("--force-monitor", action="store_true",
                        help="Force monitor mode (WARNING: disconnects WiFi!)")
    parser.add_argument("--no-dashboard", action="store_true",
                        help="Disable terminal dashboard")
    parser.add_argument("--no-ruview", action="store_true",
                        help="Disable RuView integration")
    parser.add_argument("--no-channel-hop", action="store_true",
                        help="Disable channel hopping")
    parser.add_argument("--baseline-duration", type=int,
                        default=config.BASELINE_DURATION,
                        help=f"Calibration seconds (default: {config.BASELINE_DURATION})")
    parser.add_argument("--log-level", default=config.LOG_LEVEL,
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--log-file", default=None)
    parser.add_argument("--ruview-host", default=config.RUVIEW_HOST)
    parser.add_argument("--ruview-port", type=int, default=config.RUVIEW_UDP_PORT)

    return parser.parse_args()


def check_root():
    if os.geteuid() != 0:
        print("\n[!] Sens8 requires root for WiFi scanning.")
        print("    Run with: sudo python3 main.py\n")
        sys.exit(1)


def main():
    args = parse_args()

    # Apply CLI overrides
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
    ssid = get_connected_ssid(interface)

    if ssid:
        console.print(f"  [green]Connected to:[/green] [bold]{ssid}[/bold]")

    # ─── Setup Capture Mode (SAFE by default) ────────────────
    capture_mode, capture_iface = setup_capture(
        interface, force_monitor=args.force_monitor
    )

    mode_labels = {
        CaptureMode.MANAGED_SCAN: "managed-scan (WiFi preserved ✓)",
        CaptureMode.VIRTUAL_MONITOR: "virtual-monitor (mon0, WiFi preserved ✓)",
        CaptureMode.DIRECT_MONITOR: "direct-monitor (WiFi disconnected ⚠)",
    }
    mode_label = mode_labels[capture_mode]

    show_startup_banner(console, interface, chipset, mode_label)

    # Warn if WiFi was disconnected
    if capture_mode == CaptureMode.DIRECT_MONITOR:
        console.print(
            "[bold yellow]⚠ WiFi connection disconnected for monitor mode.[/bold yellow]"
        )
        console.print(
            "[dim]  WiFi will be restored when Sens8 exits (Ctrl+C).[/dim]"
        )

    # ─── Initialize Components ────────────────────────────────
    tracker = DeviceTracker()
    baseline = BaselineCalibrator(tracker)
    variance = VarianceAnalyzer(tracker, baseline)
    presence = PresenceDetector()
    counter = PersonCounter(variance)
    vitals = VitalsEstimator(tracker)

    # Create appropriate scanner
    if capture_mode in (CaptureMode.VIRTUAL_MONITOR, CaptureMode.DIRECT_MONITOR):
        scanner = MonitorScanner(capture_iface, tracker)
    else:
        scanner = ManagedScanner(capture_iface, tracker)

    # Synthetic CSI
    frame_builder = CSIFrameBuilder(variance, presence, counter)
    udp_sender = UDPSender(frame_builder)

    # WebSocket
    ws_output = WebSocketOutput()

    # Dashboard
    dashboard = None
    if not args.no_dashboard:
        dashboard = Dashboard(
            interface=interface,
            chipset=chipset,
            mode=mode_label,
            tracker=tracker,
            variance=variance,
            presence=presence,
            counter=counter,
            vitals=vitals,
            baseline=baseline,
            udp_sender=udp_sender,
        )

    # ─── Start Capture ────────────────────────────────────────
    console.print("[bold cyan]Starting capture...[/bold cyan]")
    scanner.start()

    if capture_mode != CaptureMode.MANAGED_SCAN:
        start_channel_hopping(capture_iface)

    # ─── Calibration ──────────────────────────────────────────
    cal_dur = args.baseline_duration
    console.print(f"[bold yellow]Calibrating baseline ({cal_dur}s)...[/bold yellow]")
    console.print("[dim]  Walk away from the device during calibration for best results.[/dim]")

    baseline.calibrate(duration=cal_dur)

    bl_count = len(baseline.baselines)
    console.print(
        f"[bold green]✓ Calibrated: {bl_count} APs, "
        f"confidence: {baseline.confidence:.0%}[/bold green]"
    )

    if bl_count == 0:
        console.print(
            "[yellow]⚠ No APs detected. Make sure WiFi is active nearby.[/yellow]"
        )

    # ─── Start Output ─────────────────────────────────────────
    if not args.no_ruview:
        udp_sender.start()
        ws_output.start()
        console.print("[green]✓ RuView integration active[/green]")

    if dashboard:
        console.print("[cyan]Launching dashboard (Ctrl+C to exit)...[/cyan]")
        time.sleep(1.5)
        dashboard.start()

    # ─── Main Loop ────────────────────────────────────────────
    logger.info("Entering main analysis loop")
    stop_event = threading.Event()

    def _shutdown(signum=None, frame=None):
        logger.info("Shutdown signal received")
        stop_event.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    analysis_interval = 1.0 / config.DASHBOARD_REFRESH_RATE
    baseline_tick = 0
    baseline_update_ticks = int(
        config.BASELINE_UPDATE_INTERVAL / analysis_interval
    )

    try:
        while not stop_event.is_set():
            t0 = time.monotonic()

            # Run analysis pipeline
            motion = variance.analyze()
            presence.update(motion)
            counter.update()
            vitals.update()

            # Periodic baseline update
            baseline_tick += 1
            if baseline_tick >= baseline_update_ticks:
                baseline.update_ema()
                baseline_tick = 0
                tracker.prune_stale()

            # WebSocket data
            if not args.no_ruview:
                try:
                    data = ws_output.build_sensing_data(
                        variance, presence, counter, vitals, tracker
                    )
                    ws_output.update_data(data)
                except Exception:
                    pass

            # Maintain loop rate
            elapsed = time.monotonic() - t0
            remaining = analysis_interval - elapsed
            if remaining > 0:
                stop_event.wait(remaining)

    except KeyboardInterrupt:
        pass
    finally:
        # ─── Cleanup ─────────────────────────────────────────
        logger.info("Shutting down...")

        if dashboard:
            dashboard.stop()
        scanner.stop()
        udp_sender.stop()
        ws_output.stop()
        monitor_cleanup()

        # Verify WiFi is restored
        ssid = get_connected_ssid(interface)
        if ssid:
            console.print(f"\n[bold green]✓ WiFi connected: {ssid}[/bold green]")
        else:
            console.print(
                f"\n[yellow]WiFi reconnecting... "
                f"If it doesn't reconnect, run:[/yellow]"
            )
            console.print(
                f"  [bold]sudo systemctl restart NetworkManager[/bold]"
            )

        console.print("[bold green]✓ Sens8 shutdown complete[/bold green]")


if __name__ == "__main__":
    main()
