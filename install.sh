#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════
#  Sens8 — Install & Setup Script
#  Safe by default: uses managed-mode scanning, WiFi stays connected.
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
    echo -e "  ${GREEN}Safe mode: WiFi stays connected${NC}"
    echo ""
}

log_info()  { echo -e "  ${GREEN}[✓]${NC} $1"; }
log_warn()  { echo -e "  ${YELLOW}[!]${NC} $1"; }
log_error() { echo -e "  ${RED}[✗]${NC} $1"; }
log_step()  { echo -e "\n  ${CYAN}${BOLD}▸ $1${NC}"; }

check_root() {
    if [ "$EUID" -ne 0 ]; then
        log_error "This script must be run as root."
        echo -e "  Run with: ${BOLD}sudo bash install.sh${NC}"
        exit 1
    fi
}

install_system_deps() {
    log_step "Checking system dependencies..."

    local missing=()
    for dep in iw ip python3; do
        if ! command -v "$dep" &>/dev/null; then
            missing+=("$dep")
        fi
    done

    if [ ${#missing[@]} -gt 0 ]; then
        log_warn "Missing: ${missing[*]}"
        log_info "Installing..."
        apt-get update -qq 2>/dev/null
        apt-get install -y -qq iw net-tools wireless-tools python3 python3-pip ethtool 2>/dev/null
    fi

    log_info "System dependencies OK"
}

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

detect_interface() {
    log_step "Detecting WiFi interface..."

    IFACES=$(iw dev 2>/dev/null | grep "Interface" | awk '{print $2}' | grep -v "^mon")
    if [ -z "$IFACES" ]; then
        log_error "No WiFi interfaces found!"
        exit 1
    fi

    IFACE=""
    for pref in wlan0 wlan1; do
        if echo "$IFACES" | grep -q "^${pref}$"; then
            IFACE="$pref"
            break
        fi
    done
    [ -z "$IFACE" ] && IFACE=$(echo "$IFACES" | head -n1)

    log_info "Interface: ${BOLD}$IFACE${NC}"

    # Show connection info
    SSID=$(iw dev "$IFACE" info 2>/dev/null | grep "ssid" | awk '{print $2}')
    if [ -n "$SSID" ]; then
        log_info "Connected to: ${BOLD}$SSID${NC} (will stay connected)"
    fi

    # Show driver
    DRIVER=$(ethtool -i "$IFACE" 2>/dev/null | grep "driver:" | awk '{print $2}')
    [ -n "$DRIVER" ] && log_info "Driver: ${BOLD}$DRIVER${NC}"

    echo "$IFACE"
}

main() {
    banner
    check_root
    install_system_deps
    install_python_deps

    IFACE=$(detect_interface)

    echo ""
    log_info "Installation complete!"
    echo ""
    echo -e "  ${BOLD}Your WiFi will stay connected.${NC}"
    echo -e "  Sens8 uses managed-mode scanning by default."
    echo ""

    if [ "$1" = "--run" ] || [ "$1" = "-r" ]; then
        shift
        log_step "Launching Sens8..."
        echo ""
        cd "$SCRIPT_DIR"
        exec python3 main.py -i "$IFACE" "$@"
    else
        echo -e "  To start Sens8, run:"
        echo -e "    ${BOLD}sudo python3 main.py${NC}"
        echo ""
        echo -e "  Or with this script:"
        echo -e "    ${BOLD}sudo bash install.sh --run${NC}"
        echo ""
    fi
}

main "$@"
