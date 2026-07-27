"""
Sens8 — Rich Terminal Dashboard
Live terminal UI with all sensing data.
Fixed: proper rendering, no crashes, handles small terminals.
"""

import time
import logging
import threading
from typing import Optional

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
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
    """Rich terminal dashboard for live sensing display."""

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
        self._thread: Optional[threading.Thread] = None

    @property
    def uptime(self) -> str:
        elapsed = int(time.time() - self._start_time)
        h, m, s = elapsed // 3600, (elapsed % 3600) // 60, elapsed % 60
        if h > 0:
            return f"{h}h {m}m {s}s"
        elif m > 0:
            return f"{m}m {s}s"
        return f"{s}s"

    def start(self):
        """Start dashboard in background thread."""
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="dashboard"
        )
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)

    def _run(self):
        """Main render loop."""
        interval = 1.0 / config.DASHBOARD_REFRESH_RATE

        try:
            with Live(
                self._render(),
                console=self.console,
                refresh_per_second=config.DASHBOARD_REFRESH_RATE,
                screen=True,
                vertical_overflow="ellipsis",
            ) as live:
                while self._running:
                    try:
                        live.update(self._render())
                    except Exception as e:
                        logger.debug(f"Render error: {e}")
                    time.sleep(interval)
        except Exception as e:
            logger.error(f"Dashboard fatal: {e}")
            # Fall back to simple output
            self._run_simple()

    def _run_simple(self):
        """Fallback: simple text output if Rich layout fails."""
        while self._running:
            try:
                self.console.clear()
                self.console.print(self._render_simple())
            except Exception:
                pass
            time.sleep(1)

    def _render(self) -> Group:
        """Build the full dashboard as a Group of panels."""
        panels = []

        # ─── Header ──────────────────────────────────────────
        panels.append(self._render_header())

        # ─── Calibration ─────────────────────────────────────
        if self.baseline.progress < 1.0:
            panels.append(self._render_calibration())

        # ─── Main sensing row ────────────────────────────────
        sensing_cols = Columns([
            self._render_person_count(),
            self._render_presence(),
        ], equal=True, expand=True)
        panels.append(sensing_cols)

        # ─── Motion + Vitals row ─────────────────────────────
        motion_cols = Columns([
            self._render_motion(),
            self._render_vitals(),
        ], equal=True, expand=True)
        panels.append(motion_cols)

        # ─── RSSI Heatmap ────────────────────────────────────
        panels.append(self._render_rssi_heatmap())

        # ─── Zones + Devices row ─────────────────────────────
        bottom_cols = Columns([
            self._render_zones(),
            self._render_device_summary(),
        ], equal=True, expand=True)
        panels.append(bottom_cols)

        # ─── Footer ──────────────────────────────────────────
        panels.append(self._render_footer())

        return Group(*panels)

    def _render_header(self) -> Panel:
        header = Text()
        header.append(" ⦿ SENS8 ", style="bold white on blue")
        header.append(" WiFi Sensing ", style="bold cyan")
        header.append(f"│ {self.interface} ", style="bold green")
        header.append(f"({self.chipset}) ", style="dim")
        header.append(f"│ {self.mode} ", style="bold yellow")
        header.append(f"│ ⏱ {self.uptime} ", style="dim cyan")

        # Scan rate
        pps = 0
        try:
            pps = self.tracker.total_updates / max(1, time.time() - self._start_time)
        except Exception:
            pass
        header.append(f"│ {pps:.1f} readings/s", style="dim")

        return Panel(header, box=box.HEAVY, style="blue")

    def _render_calibration(self) -> Panel:
        pct = int(self.baseline.progress * 100)
        bar = "█" * (pct // 2) + "░" * (50 - pct // 2)
        text = Text()
        text.append(f" ⏳ Calibrating... {pct}%  ", style="bold yellow")
        text.append(bar, style="yellow")
        return Panel(text, title="[bold]Calibration[/bold]", border_style="yellow")

    def _render_person_count(self) -> Panel:
        result = self.counter.result
        text = Text(justify="center")

        # Large count display
        if result.count == 0:
            text.append("0", style="bold dim white")
            text.append(" people", style="dim")
        elif result.count == 1:
            text.append("1", style="bold green")
            text.append(" person", style="green")
        elif result.count <= 3:
            text.append(f"{result.count}", style="bold yellow")
            text.append(" people", style="yellow")
        else:
            text.append("3+", style="bold red")
            text.append(" people", style="red")

        # Confidence bar
        conf = result.confidence
        conf_color = "green" if conf >= 0.6 else "yellow" if conf >= 0.4 else "red"
        conf_bar = "█" * int(conf * 10) + "░" * (10 - int(conf * 10))
        text.append(f"\n{conf_bar} ", style=conf_color)
        text.append(f"{conf:.0%} ", style=f"bold {conf_color}")
        text.append(f"({result.confidence_label})", style="dim")

        return Panel(
            text,
            title="[bold cyan]OCCUPANCY[/bold cyan] [dim](RSSI estimate)[/dim]",
            border_style="cyan",
            box=box.DOUBLE,
        )

    def _render_presence(self) -> Panel:
        state = self.presence.state
        text = Text(justify="center")

        if state.present:
            text.append("● PRESENT", style="bold green")
        else:
            text.append("○ ABSENT", style="bold dim")

        # Confidence
        conf = state.confidence
        conf_bar = "█" * int(conf * 10) + "░" * (10 - int(conf * 10))
        conf_color = "green" if conf >= 0.5 else "yellow"
        text.append(f"\n{conf_bar} ", style=conf_color)
        text.append(f"{conf:.0%}", style=f"bold {conf_color}")

        if state.duration > 0 and state.present:
            text.append(f"\nfor {int(state.duration)}s", style="dim cyan")

        trend = self.variance.get_motion_trend()
        trend_icons = {"rising": "↑", "falling": "↓", "stable": "→", "collecting": "…"}
        text.append(f"\ntrend: {trend_icons.get(trend, '?')} {trend}", style="dim")

        return Panel(
            text,
            title="[bold]Presence[/bold] [dim](RSSI estimate)[/dim]",
            border_style="green" if state.present else "dim",
            box=box.DOUBLE,
        )

    def _render_motion(self) -> Panel:
        score = self.variance.motion_score
        pct = int(score * 100)
        bar_width = 30

        text = Text()

        # Animated bar
        bar = ""
        for i in range(bar_width):
            frac = i / bar_width
            if frac < score:
                if score > 0.7:
                    bar += "█"
                elif score > 0.35:
                    bar += "▓"
                else:
                    bar += "▒"
            else:
                bar += "░"

        color = "red" if score > 0.7 else "yellow" if score > 0.15 else "green"
        text.append(f" {bar} ", style=color)
        text.append(f"{pct}%", style=f"bold {color}")

        # Threshold marker
        thresh_pct = int(config.PRESENCE_THRESHOLD * 100)
        text.append(f"\n threshold: {thresh_pct}%", style="dim")

        # Status label
        if score > 0.7:
            text.append(" │ ACTIVE MOTION", style="bold red")
        elif score > config.PRESENCE_THRESHOLD:
            text.append(" │ motion detected", style="yellow")
        else:
            text.append(" │ quiet", style="dim green")

        return Panel(text, title="[bold]Motion Score[/bold]", border_style=color)

    def _render_vitals(self) -> Panel:
        result = self.vitals.result
        text = Text()

        if result.is_reportable:
            text.append(f" ~{result.bpm:.0f} BPM ", style="bold cyan")
            conf_bar = "█" * int(result.confidence * 10) + "░" * (10 - int(result.confidence * 10))
            text.append(f"\n {conf_bar} {result.confidence:.0%}", style="dim cyan")
        else:
            text.append(" — insufficient data", style="dim")

        if result.stable_ap_ssid:
            text.append(f"\n via: {result.stable_ap_ssid}", style="dim")

        return Panel(
            text,
            title="[bold]Breathing[/bold] [dim](estimated, low conf)[/dim]",
            border_style="dim cyan",
        )

    def _render_rssi_heatmap(self) -> Panel:
        table = Table(
            box=box.SIMPLE_HEAVY, expand=True,
            title_style="bold cyan",
            show_edge=False,
            pad_edge=False,
        )

        table.add_column("AP / SSID", style="cyan", no_wrap=True, max_width=22)
        table.add_column("Ch", justify="center", style="dim", width=4)
        table.add_column("RSSI", justify="right", style="bold", width=6)
        table.add_column("Signal", min_width=16)
        table.add_column("Δσ", justify="right", style="dim", width=5)
        table.add_column("Motion", justify="right", width=7)

        aps = self.tracker.get_strongest_aps(n=12)
        per_ap = self.variance.per_ap_results

        for ap in aps:
            name = ap.ssid or ap.mac[:17]
            if len(name) > 22:
                name = name[:19] + "…"

            rssi = ap.latest_rssi or -99
            channel = str(ap.channel) if ap.channel else "?"

            # Signal bar
            bar_len = max(0, min(16, (rssi + 95) // 4))
            bar_color = "green" if rssi >= -50 else "yellow" if rssi >= -70 else "red"
            signal_bar = Text()
            signal_bar.append("█" * bar_len, style=bar_color)
            signal_bar.append("░" * (16 - bar_len), style="dim")

            # Variance
            vr = per_ap.get(ap.mac)
            delta = f"{vr.std_delta:.1f}" if vr else "—"
            motion = f"{vr.motion_score:.0%}" if vr else "—"
            motion_style = ""
            if vr and vr.motion_score > 0.5:
                motion_style = "bold yellow"
            elif vr and vr.motion_score > 0.2:
                motion_style = "yellow"

            table.add_row(
                name, channel, f"{rssi}",
                signal_bar, delta,
                Text(motion, style=motion_style),
            )

        if not aps:
            table.add_row(
                "Scanning…", "", "",
                Text("░" * 16, style="dim"), "", ""
            )

        return Panel(
            table,
            title="[bold]RSSI Heatmap[/bold]",
            border_style="cyan",
        )

    def _render_zones(self) -> Panel:
        zones = self.variance.zones
        text = Text()

        zone_info = {
            "near": ("🟢", "green", "Same Room"),
            "medium": ("🟡", "yellow", "Adjacent"),
            "far": ("🔴", "red", "Far"),
        }

        if zones:
            for zone_name, macs in zones.items():
                icon, color, label = zone_info.get(zone_name, ("⚪", "dim", zone_name))
                text.append(f" {icon} {label}", style=f"bold {color}")
                text.append(f" ({len(macs)})\n", style="dim")

                for mac in macs[:3]:
                    dev = self.tracker.get_device(mac)
                    if dev:
                        name = dev.ssid or dev.mac[:11]
                        rssi = dev.latest_rssi or 0
                        text.append(f"   {name} ", style="dim")
                        text.append(f"[{rssi}]\n", style=f"dim {color}")
        else:
            text.append(" Collecting data…", style="dim")

        return Panel(text, title="[bold]Zones[/bold]", border_style="dim")

    def _render_device_summary(self) -> Panel:
        text = Text()
        text.append(f" 📡 APs: ", style="dim")
        text.append(f"{self.tracker.ap_count}", style="bold cyan")
        text.append(f"\n 📱 Clients: ", style="dim")
        text.append(f"{self.tracker.client_count}", style="bold yellow")
        text.append(f"\n 🔢 Total MACs: ", style="dim")
        text.append(f"{self.tracker.total_count}", style="bold")
        text.append(f"\n 📊 Readings: ", style="dim")
        text.append(f"{self.tracker.total_updates}", style="dim cyan")

        # Calibration confidence
        if self.baseline.is_calibrated:
            text.append(f"\n ✓ Calibrated: ", style="dim")
            text.append(f"{self.baseline.confidence:.0%}", style="bold green")

        return Panel(text, title="[bold]Devices[/bold]", border_style="dim")

    def _render_footer(self) -> Panel:
        text = Text()

        # RuView UDP status
        if self.udp_sender.is_connected:
            text.append(" ● ", style="green")
            text.append("RuView UDP ", style="bold green")
        else:
            text.append(" ○ ", style="red")
            text.append("RuView UDP ", style="dim red")

        text.append(
            f"({config.RUVIEW_HOST}:{config.RUVIEW_UDP_PORT}) ", style="dim"
        )
        text.append(f"│ frames: {self.udp_sender.frames_sent} ", style="dim cyan")

        if self.udp_sender.error_count > 0:
            text.append(f"│ err: {self.udp_sender.error_count} ", style="dim red")

        text.append("│ ", style="dim")
        text.append("RSSI-based sensing — not real CSI", style="dim yellow")

        return Panel(text, box=box.HEAVY, style="dim")

    def _render_simple(self) -> str:
        """Fallback simple text rendering."""
        state = self.presence.state
        count = self.counter.result
        score = self.variance.motion_score

        lines = [
            f"═══ SENS8 ═══  {self.interface} ({self.mode})  uptime: {self.uptime}",
            f"Presence: {'PRESENT' if state.present else 'ABSENT'} ({state.confidence:.0%})",
            f"Motion: {score:.0%}",
            f"Occupancy: {count.display} ({count.confidence:.0%})",
            f"APs: {self.tracker.ap_count}  Clients: {self.tracker.client_count}",
            f"Readings: {self.tracker.total_updates}",
            f"RuView: {'connected' if self.udp_sender.is_connected else 'disconnected'}",
        ]
        return "\n".join(lines)


def show_startup_banner(console: Console, interface: str, chipset: str, mode: str):
    """Show startup banner."""
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
[dim]  Pure software · No extra hardware · WiFi stays connected[/dim]
"""
    console.print(banner)
    console.print(f"  [bold]Interface:[/bold] {interface}")
    console.print(f"  [bold]Chipset:[/bold]   {chipset}")
    console.print(f"  [bold]Mode:[/bold]      {mode}")
    console.print(f"  [bold]RuView:[/bold]    {config.RUVIEW_HOST}:{config.RUVIEW_UDP_PORT}")
    console.print()
