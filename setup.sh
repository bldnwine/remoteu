#!/usr/bin/env bash
# ==============================================================================
# setup.sh — Automated, secure setup for remoteu on Linux (Arch / Hyprland / Wayland)
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENABLE_AUTOSTART="prompt"

# Parse optional arguments
for arg in "$@"; do
    case "$arg" in
        --autostart)
            ENABLE_AUTOSTART="yes"
            ;;
        --no-autostart)
            ENABLE_AUTOSTART="no"
            ;;
        -h|--help)
            echo "Usage: ./setup.sh [--autostart | --no-autostart]"
            echo ""
            echo "Options:"
            echo "  --autostart       Enable remoteu.service on login via systemd user service"
            echo "  --no-autostart    Skip autostart configuration"
            exit 0
            ;;
    esac
done

echo "======================================================================"
echo "                   remoteu — Setup & Configuration"
echo "======================================================================"
echo ""

# ── 1. Check & Sync Python Dependencies ──────────────────────────────
echo "[1/4] Checking python environment & dependencies (uv)..."
if ! command -v uv &>/dev/null; then
    echo "  [!] Error: 'uv' is not installed or not in PATH."
    echo "      Install uv via: curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

echo "  -> Running 'uv sync' in $SCRIPT_DIR..."
uv sync --project "$SCRIPT_DIR"
echo "  [✓] Dependencies installed successfully."
echo ""

# ── 2. Configure Secure Udev Rule (uaccess) ──────────────────────────
echo "[2/4] Setting up secure /dev/uinput udev rules (uaccess)..."
echo "  Note: Uses TAG+=\"uaccess\" (systemd-logind seat ACL) so your user"
echo "        does NOT need to be in the unsafe 'input' keylogger group."

# Ensure uinput kernel module is configured to load on boot
if [ ! -f /etc/modules-load.d/uinput.conf ] || ! grep -q "^uinput" /etc/modules-load.d/uinput.conf 2>/dev/null; then
    echo "  -> Adding 'uinput' to /etc/modules-load.d/uinput.conf (sudo required)..."
    echo "uinput" | sudo tee /etc/modules-load.d/uinput.conf >/dev/null
fi

# Load the module immediately if not already active
sudo modprobe uinput 2>/dev/null || true

# Install udev rule (must be <73 so it runs before systemd's 73-seat-late.rules)
UDEV_RULE_FILE="/etc/udev/rules.d/70-uinput.rules"
UDEV_RULE_CONTENT='KERNEL=="uinput", SUBSYSTEM=="misc", TAG+="uaccess", OPTIONS+="static_node=uinput"'

echo "  -> Writing $UDEV_RULE_FILE (sudo required)..."
echo "$UDEV_RULE_CONTENT" | sudo tee "$UDEV_RULE_FILE" >/dev/null

echo "  -> Reloading udev rules & triggering..."
sudo udevadm control --reload-rules
sudo udevadm trigger /dev/uinput 2>/dev/null || sudo udevadm trigger

echo "  [✓] Udev rules configured and applied."
echo ""

# ── 3. Test /dev/uinput Permissions ──────────────────────────────────
echo "[3/4] Verifying /dev/uinput permissions..."
if [ -w /dev/uinput ]; then
    echo "  [✓] /dev/uinput is writable by $USER!"
else
    echo "  [!] Notice: /dev/uinput is not yet directly writable in this subshell."
    echo "      If this is your active desktop seat, ACL permissions will take effect."
    echo "      Checking POSIX ACL:"
    if command -v getfacl &>/dev/null; then
        getfacl -p /dev/uinput 2>/dev/null | grep -E "user:$USER|TAG" || true
    fi
fi
echo ""

# ── 4. Autostart Configuration (Opt-in) ──────────────────────────────
echo "[4/4] Autostart configuration (Hyprland / Desktop login)..."

if [ "$ENABLE_AUTOSTART" = "prompt" ]; then
    if [ -t 0 ]; then
        read -r -p "  Do you want to enable remoteu to autostart on login via systemd user service? [y/N]: " answer
        case "${answer:-N}" in
            [yY]|[yY][eE][sS])
                ENABLE_AUTOSTART="yes"
                ;;
            *)
                ENABLE_AUTOSTART="no"
                ;;
        esac
    else
        ENABLE_AUTOSTART="no"
    fi
fi

if [ "$ENABLE_AUTOSTART" = "yes" ]; then
    echo "  -> Installing systemd user unit..."
    SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
    mkdir -p "$SYSTEMD_USER_DIR"
    cp "$SCRIPT_DIR/remoteu.service" "$SYSTEMD_USER_DIR/remoteu.service"

    echo "  -> Enabling & starting remoteu.service..."
    systemctl --user daemon-reload
    systemctl --user enable --now remoteu.service
    echo "  [✓] remoteu.service is now active and will autostart on login!"
    echo "      Check status anytime with: systemctl --user status remoteu.service"
else
    echo "  [i] Autostart skipped."
    echo ""
    echo "  If you wish to enable autostart later, you can either:"
    echo "  1) Systemd user service:"
    echo "       mkdir -p ~/.config/systemd/user"
    echo "       cp $SCRIPT_DIR/remoteu.service ~/.config/systemd/user/"
    echo "       systemctl --user daemon-reload && systemctl --user enable --now remoteu.service"
    echo ""
    echo "  2) Hyprland config (~/.config/hypr/hyprland.lua):"
    echo '       hl.exec_cmd(launch("uv run --project " .. os.getenv("HOME") .. "/Projects/remoteu remoteu"))'
    echo "     Or standard hyprland.conf:"
    echo "       exec-once = uv run --project $SCRIPT_DIR remoteu"
fi

echo ""
echo "======================================================================"
echo "                     Setup Complete!"
echo "======================================================================"
echo "  Run manually:  uv run remoteu"
echo "  Access URL:    http://<your-ip>:5000"
echo "======================================================================"
