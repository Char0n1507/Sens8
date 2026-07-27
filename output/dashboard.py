"""
Sens8 — Rich Terminal Dashboard
Live terminal UI showing all sensing data with rich library.
Refreshes at DASHBOARD_REFRESH_RATE Hz.
"""

import time
import logging
import threading
from typing import Optional

from rich.console import Console
from rich.live import Live
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.progress_bar import ProgressBar
from rich.columns import Columns
from rich import box

from capture.devices import DeviceTracker
from processing.variance import VarianceAnalyzer
from processing.presence import PresenceDetector
from processing.counter import PersonCounter
from processing.vitals import VitalsEstimator
from processing.baseline import BaselineCalibrator
from synthetic.udp_sender import UDPSender
import config

logger = logging.getLogger("sens8.output.dashboard")


class Dashboard:
    """
    Rich terminal dashboard for live sensing display.
    """

    def __init__(
        self,
        interface: str,
        chipset: str,
        mode: str,
        tracker: DeviceTracker,
        variance: VarianceAnalyzer,
        presence: PresenceDetector,
        counter: PersonCounter,
        vitals: VitalsEstimator,
        baseline: BaselineCalibrator,
        udp_sender: UDPSender,
    ):
        self.interface = interface
        self.chipset = chipset
        self.mode = mode
        self.tracker = tracker
        self.variance = variance
        self.presence = presence
        self.counter = counter
        self.vitals = vitals
        self.baseline = baseline
        self.udp_sender = udp_sender
        self.console = Console()
        self._start_time = time.time()
        self._running = False
        self._live: Optional[Live] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def uptime(self) -> str:
        """Format uptime string."""
        elapsed = int(time.time() - self._start_time)
        hours = elapsed // 3600
        minutes = (elapsed % 3600) // 60
        seconds = elapsed % 60
        if hours > 0:
            return f"{hours}h {minutes}m {seconds}s"
        elif minutes > 0:
            return f"{minutes}m {seconds}s"
        return f"{seconds}s"

    def start(self):
        """Start the dashboard in a background thread."""
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="dashboard"
        )
        self._thread.start()

    def stop(self):
        """Stop the dashboard."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)

    def _run(self):
        """Main dashboard render loop."""
        interval = 1.0 / config.DASHBOARD_REFRESH_RATE

        try:
            with Live(
                self._build_layout(),
                console=self.console,
                refresh_per_second=config.DASHBOARD_REFRESH_RATE,
                screen=True,
            ) as live:
                self._live = live
                while self._running:
                    try:
                        live.update(self._build_layout())
                    except Exception as e:
                        logger.debug(f"Dashboard render error: {e}")
                    time.sleep(interval)
        except Exception as e:
            logger.error(f"Dashboard error: {e}")

    def _build_layout(self) -> Layout:
        """Build the full dashboard layout."""
        layout = Layout()

        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="calibration", size=3),
            Layout(name="main"),
            Layout(name="footer", size=3),
        )

        layout["main"].split_row(
            Layout(name="left", ratio=2),
            Layout(name="right", ratio=3),
        )

        layout["left"].split_column(
            Layout(name="person_count", size=7),
            Layout(name="presence", size=5),
            Layout(name="motion", size=5),
            Layout(name="vitals", size=5),
            Layout(name="zones"),
        )

        layout["right"].split_column(
            Layout(name="rssi_heatmap"),
            Layout(name="devices", size=5),
        )

        # Render sections
        layout["header"].update(self._render_header())
        layout["calibration"].update(self._render_calibration())
        layout["person_count"].update(self._render_person_count())
        layout["presence"].update(self._render_presence())
        layout["motion"].update(self._render_motion())
        layout["vitals"].update(self._render_vitals())
        layout["zones"].update(self._render_zones())
        layout["rssi_heatmap"].update(self._render_rssi_heatmap())
        layout["devices"].update(self._render_device_summary())
        layout["footer"].update(self._render_footer())

        return layout

    def _render_header(self) -> Panel:
        """Render header with interface info."""
        header = Text()
        header.append("  ⦿ SENS8 ", style="bold white on blue")
        header.append("  WiFi Sensing Daemon  ", style="bold cyan")
        header.append(f"│ {self.interface} ", style="bold green")
        header.append(f"│ {self.chipset} ", style="dim")
        header.append(f"│ {self.mode} ", style="bold yellow")
        header.append(f"│ uptime: {self.uptime} ", style="dim cyan")

        return Panel(header, style="blue", box=box.HEAVY)

    def _render_calibration(self) -> Panel:
        """Render calibration progress bar."""
        progress = self.baseline.progress
        if progress >= 1.0:
            text = Text()
            text.append(" ✓ CALIBRATED ", style="bold green")
            conf = self.baseline.confidence
            text.append(f" confidence: {conf:.0%} ", style="dim")
            return Panel(text, title="[bold]Calibration[/bold]", border_style="green")
        else:
            pct = int(progress * 100)
            bar = "█" * (pct // 2) + "░" * (50 - pct // 2)
            text = Text()
            text.append(f" Calibrating... {pct}%  ", style="bold yellow")
            text.append(bar, style="yellow")
            return Panel(text, title="[bold]Calibration[/bold]", border_style="yellow")

    def _render_person_count(self) -> Panel:
        """Render large person count display."""
        result = self.counter.result
        count_text = Text(justify="center")

        if result.count == 0:
            count_text.append("\n 0 ", style="bold dim white")
            count_text.append("people", style="dim")
        elif result.count == 1:
            count_text.append("\n 1 ", style="bold green")
            count_text.append("person", style="green")
        elif result.count <= 3:
            count_text.append(f"\n {result.count} ", style="bold yellow")
            count_text.append("people", style="yellow")
        else:
            count_text.append("\n 3+ ", style="bold red")
            count_text.append("people", style="red")

        # Confidence
        conf = result.confidence
        conf_color = "green" if conf >= 0.6 else "yellow" if conf >= 0.4 else "red"
        count_text.append(f"\n  conf: {conf:.0%} ", style=f"dim {conf_color}")
        count_text.append(f"({result.confidence_label})", style="dim")

        title = "[bold]ESTIMATED OCCUPANCY[/bold] [dim](RSSI-based)[/dim]"
        return Panel(
            count_text, title=title,
            subtitle="[dim]accuracy drops above 2 people without real CSI[/dim]",
            border_style="cyan", box=box.DOUBLE,
        )

    def _render_presence(self) -> Panel:
        """Render presence indicator."""
        state = self.presence.state
        text = Text()

        if state.present:
            text.append(" ● PRESENT ", style="bold green on dark_green")
        else:
            text.append(" ○ ABSENT  ", style="bold dim")

        text.append(f"  confidence: {state.confidence:.0%}", style="dim")
        if state.duration > 0:
            text.append(f"  │ duration: {int(state.duration)}s", style="dim cyan")

        return Panel(
            text,
            title="[bold]Presence[/bold] [dim](RSSI-based estimate)[/dim]",
            border_style="green" if state.present else "dim",
        )

    def _render_motion(self) -> Panel:
        """Render motion score bar."""
        score = self.variance.motion_score
        pct = int(score * 100)
        bar_width = 40

        filled = int(score * bar_width)
        bar = ""

        for i in range(bar_width):
            if i < filled:
                if score > 0.7:
                    bar += "█"
                elif score > 0.4:
                    bar += "▓"
                else:
                    bar += "▒"
            else:
                bar += "░"

        text = Text()
        color = "red" if score > 0.7 else "yellow" if score > 0.35 else "green"
        text.append(f" {bar} ", style=color)
        text.append(f" {pct}%", style=f"bold {color}")

        threshold_pos = int(config.PRESENCE_THRESHOLD * bar_width) + 1
        text.append(f"  │ threshold: {config.PRESENCE_THRESHOLD:.0%}", style="dim")

        return Panel(text, title="[bold]Motion Score[/bold]", border_style=color)

    def _render_vitals(self) -> Panel:
        """Render breathing rate estimation."""
        result = self.vitals.result
        text = Text()

        if result.is_reportable:
            text.append(f" ~{result.bpm:.0f} BPM ", style="bold cyan")
            text.append(f" (confidence: {result.confidence:.0%})", style="dim")
        else:
            text.append(" — insufficient data ", style="dim")

        if result.stable_ap_ssid:
            text.append(f"\n  via: {result.stable_ap_ssid}", style="dim cyan")

        return Panel(
            text,
            title="[bold]Breathing Rate[/bold] [dim](estimated — low confidence without CSI)[/dim]",
            border_style="dim cyan",
        )

    def _render_zones(self) -> Panel:
        """Render zone map."""
        zones = self.variance.zones
        text = Text()

        zone_labels = {
            "near": ("🟢", "green", "Same Room"),
            "medium": ("🟡", "yellow", "Adjacent"),
            "far": ("🔴", "red", "Far"),
        }

        for zone_name, macs in zones.items():
            icon, color, label = zone_labels.get(zone_name, ("⚪", "dim", zone_name))
            text.append(f"\n {icon} {label}", style=f"bold {color}")
            text.append(f" ({len(macs)} APs)", style="dim")

            # Show top 3 APs in zone
            for mac in macs[:3]:
                dev = self.tracker.get_device(mac)
                if dev:
                    name = dev.ssid or dev.mac[:11]
                    rssi = dev.latest_rssi or 0
                    text.append(f"\n    {name} ", style=f"dim {color}")
                    text.append(f"[{rssi} dBm]", style="dim")

        if not zones:
            text.append("\n  No zone data yet...", style="dim")

        return Panel(text, title="[bold]Zone Map[/bold]", border_style="dim")

    def _render_rssi_heatmap(self) -> Panel:
        """Render live RSSI table for all visible APs."""
        table = Table(
            box=box.SIMPLE_HEAVY, expand=True,
            title="[bold]Live RSSI Heatmap[/bold]",
            title_style="bold cyan",
        )

        table.add_column("SSID / MAC", style="cyan", no_wrap=True, max_width=20)
        table.add_column("Ch", justify="center", style="dim", width=4)
        table.add_column("RSSI", justify="right", style="bold", width=6)
        table.add_column("Signal", min_width=20)
        table.add_column("Δ", justify="right", style="dim", width=6)
        table.add_column("Motion", justify="right", width=6)

        # Get APs sorted by signal strength
        aps = self.tracker.get_strongest_aps(n=15)

        for ap in aps:
            name = ap.ssid or ap.mac[:17]
            if len(name) > 20:
                name = name[:17] + "..."

            rssi = ap.latest_rssi or -99
            channel = str(ap.channel) if ap.channel else "?"

            # Signal bar
            bar_len = max(0, min(20, (rssi + 90) // 3))
            if rssi >= -50:
                bar_color = "green"
            elif rssi >= -70:
                bar_color = "yellow"
            else:
                bar_color = "red"
            signal_bar = Text()
            signal_bar.append("█" * bar_len, style=bar_color)
            signal_bar.append("░" * (20 - bar_len), style="dim")

            # Variance delta
            per_ap = self.variance.per_ap_results
            vr = per_ap.get(ap.mac)
            delta = f"{vr.std_delta:.1f}" if vr else "—"
            motion = f"{vr.motion_score:.0%}" if vr else "—"

            table.add_row(name, channel, f"{rssi}", signal_bar, delta, motion)

        if not aps:
            table.add_row("Scanning...", "", "", Text("░" * 20, style="dim"), "", "")

        return Panel(table, border_style="cyan")

    def _render_device_summary(self) -> Panel:
        """Render device count summary."""
        text = Text()
        text.append(f" APs: {self.tracker.ap_count}", style="bold cyan")
        text.append(f"  │  Clients: {self.tracker.client_count}", style="bold yellow")
        text.append(f"  │  Total MACs: {self.tracker.total_count}", style="bold")
        text.append(
            f"  │  Packets: {self.tracker.total_updates}",
            style="dim"
        )

        return Panel(text, title="[bold]Devices[/bold]", border_style="dim")

    def _render_footer(self) -> Panel:
        """Render footer with RuView connection status."""
        text = Text()

        # RuView status
        if self.udp_sender.is_connected:
            text.append(" ● RuView UDP ", style="bold green")
        else:
            text.append(" ○ RuView UDP ", style="bold red")
        text.append(
            f"({config.RUVIEW_HOST}:{config.RUVIEW_UDP_PORT})",
            style="dim"
        )

        text.append(
            f"  │  Frames sent: {self.udp_sender.frames_sent}",
            style="dim cyan"
        )

        if self.udp_sender.error_count > 0:
            text.append(
                f"  │  Errors: {self.udp_sender.error_count}",
                style="dim red"
            )

        text.append("  │  ", style="dim")
        text.append("RSSI-based sensing", style="dim yellow")
        text.append(" — not real CSI", style="dim")

        return Panel(text, style="dim", box=box.HEAVY)


def show_startup_banner(console: Console, interface: str, chipset: str, mode: str):
    """Show the startup banner before dashboard takes over."""
    banner = """
[bold blue]
   ██████╗ ███████╗███╗   ██╗███████╗ █████╗
  ██╔════╝ ██╔════╝████╗  ██║██╔════╝██╔══██╗
  ╚█████╗  █████╗  ██╔██╗ ██║███████╗╚█████╔╝
   ╚═══██╗ ██╔══╝  ██║╚██╗██║╚════██║██╔══██╗
  ██████╔╝ ███████╗██║ ╚████║███████║╚█████╔╝
  ╚═════╝  ╚══════╝╚═╝  ╚═══╝╚══════╝ ╚════╝
[/bold blue]
[bold cyan]  WiFi Sensing Daemon — RSSI-based Detection[/bold cyan]
[dim]  Pure software WiFi sensing • No extra hardware required[/dim]
"""
    console.print(banner)
    console.print(f"  [bold]Interface:[/bold] {interface}")
    console.print(f"  [bold]Chipset:[/bold]   {chipset}")
    console.print(f"  [bold]Mode:[/bold]      {mode}")
    console.print(f"  [bold]RuView:[/bold]    {config.RUVIEW_HOST}:{config.RUVIEW_UDP_PORT}")
    console.print()
