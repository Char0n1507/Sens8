#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════
#  Sens8 — Install & Setup Script
#  Auto-detects WiFi interface, sets monitor mode, installs dependencies
# ═══════════════════════════════════════════════════════════════════════

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

banner() {
    echo -e "${CYAN}${BOLD}"
    echo "   ██████╗ ███████╗███╗   ██╗███████╗ █████╗ "
    echo "  ██╔════╝ ██╔════╝████╗  ██║██╔════╝██╔══██╗"
    echo "  ╚█████╗  █████╗  ██╔██╗ ██║███████╗╚█████╔╝"
    echo "   ╚═══██╗ ██╔══╝  ██║╚██╗██║╚════██║██╔══██╗"
    echo "  ██████╔╝ ███████╗██║ ╚████║███████║╚█████╔╝"
    echo "  ╚═════╝  ╚══════╝╚═╝  ╚═══╝╚══════╝ ╚════╝ "
    echo -e "${NC}"
    echo -e "  ${BOLD}WiFi Sensing Daemon — Installer${NC}"
    echo ""
}

log_info()  { echo -e "  ${GREEN}[✓]${NC} $1"; }
log_warn()  { echo -e "  ${YELLOW}[!]${NC} $1"; }
log_error() { echo -e "  ${RED}[✗]${NC} $1"; }
log_step()  { echo -e "\n  ${CYAN}${BOLD}▸ $1${NC}"; }

# ─── Root Check ──────────────────────────────────────────────────────
check_root() {
    if [ "$EUID" -ne 0 ]; then
        log_error "This script must be run as root."
        echo -e "  Run with: ${BOLD}sudo bash install.sh${NC}"
        exit 1
    fi
}

# ─── System Dependencies ─────────────────────────────────────────────
install_system_deps() {
    log_step "Checking system dependencies..."

    local deps=("iw" "ip" "python3" "pip3")
    local missing=()

    for dep in "${deps[@]}"; do
        if ! command -v "$dep" &>/dev/null; then
            missing+=("$dep")
        fi
    done

    if [ ${#missing[@]} -gt 0 ]; then
        log_warn "Missing: ${missing[*]}"
        log_info "Installing system dependencies..."
        apt-get update -qq
        apt-get install -y -qq iw net-tools wireless-tools python3 python3-pip aircrack-ng ethtool 2>/dev/null
    fi

    log_info "System dependencies OK"
}

# ─── Python Dependencies ─────────────────────────────────────────────
install_python_deps() {
    log_step "Installing Python dependencies..."

    cd "$SCRIPT_DIR"

    if [ -f "requirements.txt" ]; then
        pip3 install -r requirements.txt --quiet --break-system-packages 2>/dev/null || \
        pip3 install -r requirements.txt --quiet 2>/dev/null || \
        python3 -m pip install -r requirements.txt --quiet --break-system-packages 2>/dev/null || \
        python3 -m pip install -r requirements.txt --quiet
        log_info "Python dependencies installed"
    else
        log_error "requirements.txt not found!"
        exit 1
    fi
}

# ─── Interface Detection ─────────────────────────────────────────────
detect_interface() {
    log_step "Detecting WiFi interface..."

    # Get list of wireless interfaces
    IFACES=$(iw dev 2>/dev/null | grep "Interface" | awk '{print $2}')

    if [ -z "$IFACES" ]; then
        log_error "No WiFi interfaces found!"
        echo -e "  Make sure a WiFi adapter is connected."
        exit 1
    fi

    # Prefer wlan0, wlan1
    IFACE=""
    for pref in wlan0 wlan1; do
        if echo "$IFACES" | grep -q "^${pref}$"; then
            IFACE="$pref"
            break
        fi
    done

    # Fallback to first found
    if [ -z "$IFACE" ]; then
        IFACE=$(echo "$IFACES" | head -n1)
    fi

    log_info "Detected interface: ${BOLD}$IFACE${NC}"

    # Check monitor mode support
    PHY=$(iw dev "$IFACE" info 2>/dev/null | grep wiphy | awk '{print "phy"$2}')
    if [ -n "$PHY" ]; then
        if iw phy "$PHY" info 2>/dev/null | grep -qi "monitor"; then
            log_info "Monitor mode: ${GREEN}supported${NC}"
        else
            log_warn "Monitor mode: ${YELLOW}not supported${NC} (will use managed mode fallback)"
        fi

        # Show chipset
        DRIVER=$(ethtool -i "$IFACE" 2>/dev/null | grep "driver:" | awk '{print $2}')
        if [ -n "$DRIVER" ]; then
            log_info "Driver: ${BOLD}$DRIVER${NC}"
        fi
    fi

    echo "$IFACE"
}

# ─── Monitor Mode Setup ──────────────────────────────────────────────
setup_monitor() {
    local iface="$1"
    log_step "Setting up monitor mode on $iface..."

    # Kill interfering processes
    airmon-ng check kill 2>/dev/null || true

    # Check current mode
    CURRENT_MODE=$(iw dev "$iface" info 2>/dev/null | grep "type" | awk '{print $2}')

    if [ "$CURRENT_MODE" = "monitor" ]; then
        log_info "Already in monitor mode"
        return 0
    fi

    # Set monitor mode
    ip link set "$iface" down 2>/dev/null
    iw dev "$iface" set type monitor 2>/dev/null
    ip link set "$iface" up 2>/dev/null

    # Verify
    NEW_MODE=$(iw dev "$iface" info 2>/dev/null | grep "type" | awk '{print $2}')
    if [ "$NEW_MODE" = "monitor" ]; then
        log_info "Monitor mode enabled successfully"
    else
        log_warn "Could not set monitor mode (mode: $NEW_MODE)"
        log_warn "Sens8 will fall back to managed mode scanning"
        # Restore
        ip link set "$iface" down 2>/dev/null
        iw dev "$iface" set type managed 2>/dev/null
        ip link set "$iface" up 2>/dev/null
    fi
}

# ─── Launch ──────────────────────────────────────────────────────────
launch() {
    log_step "Launching Sens8..."
    echo ""

    cd "$SCRIPT_DIR"
    exec python3 main.py -i "$1" "${@:2}"
}

# ─── Main ────────────────────────────────────────────────────────────
main() {
    banner
    check_root
    install_system_deps
    install_python_deps

    IFACE=$(detect_interface)
    setup_monitor "$IFACE"

    echo ""
    log_info "Installation complete!"
    echo ""

    # Parse extra args
    EXTRA_ARGS=""
    if [ "$1" = "--run" ] || [ "$1" = "-r" ]; then
        shift
        launch "$IFACE" "$@"
    else
        echo -e "  To start Sens8, run:"
        echo -e "    ${BOLD}sudo python3 main.py${NC}"
        echo ""
        echo -e "  Or re-run this script with --run:"
        echo -e "    ${BOLD}sudo bash install.sh --run${NC}"
        echo ""
    fi
}

main "$@"
