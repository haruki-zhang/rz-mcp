#!/bin/bash
# Host setup for the RZ/G3L EVK direct link (plan/PoC_Workflow_Plan.md, section 7.3). Run with sudo:
#
#   sudo bash workflow/board/setup-host.sh          # set up (safe to run again)
#   sudo bash workflow/board/setup-host.sh --undo   # remove everything this script added
#
# What it does:
#   1. udev rule: the EVK debug UART (FTDI FT234XD on Carrier II, 0403:6015) becomes group plugdev
#      and gets the stable name /dev/rzg3l-evk-console (no re-login needed; you are in plugdev).
#   2. NetworkManager profile "evk-direct" on the USB NIC: 192.168.50.1/24, never the default route,
#      no DNS, no NAT. The EVK cannot reach the company network and vice versa.
#   3. dnsmasq: DHCP (192.168.50.100-150) and TFTP (root /srv/tftp) on the USB NIC only; DNS off.
#   4. /srv/tftp owned by you, so the tools can stage files without sudo.
#   5. ufw (only if it is active): allow incoming traffic on the USB NIC only.
# NFS is not set up here; it is only needed later for NFS root.
set -euo pipefail

IFACE=enx207bd2aa6c7a
HOST_CIDR=192.168.50.1/24
DHCP_RANGE=192.168.50.100,192.168.50.150,255.255.255.0,12h
TFTP_ROOT=/srv/tftp
CON=evk-direct
UDEV=/etc/udev/rules.d/70-rzg3l-evk-console.rules
DNSMASQ_CONF=/etc/dnsmasq.d/rzg3l-evk.conf
OWNER=${SUDO_USER:?run this with sudo from your own account}

[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }
ufw_active() { command -v ufw >/dev/null && ufw status | grep -q "^Status: active"; }

if [ "${1:-}" = "--undo" ]; then
    rm -f "$UDEV" && udevadm control --reload
    nmcli -t -f NAME con show | grep -qx "$CON" && nmcli con delete "$CON"
    rm -f "$DNSMASQ_CONF"
    systemctl disable --now dnsmasq 2>/dev/null || true
    ufw_active && ufw delete allow in on "$IFACE" || true
    echo "undone. Left in place: the dnsmasq package (apt remove dnsmasq) and $TFTP_ROOT."
    exit 0
fi

ip link show "$IFACE" >/dev/null || { echo "USB NIC $IFACE not found; plug it in" >&2; exit 1; }

echo "== 1. udev rule for the EVK console"
cat > "$UDEV" <<'EOF'
# RZ/G3L SMARC EVK debug UART (Carrier II SER3, FTDI FT234XD) - rz-mcp workflow/board/setup-host.sh
SUBSYSTEM=="tty", ATTRS{idVendor}=="0403", ATTRS{idProduct}=="6015", GROUP="plugdev", MODE="0660", SYMLINK+="rzg3l-evk-console"
EOF
udevadm control --reload && udevadm trigger --subsystem-match=tty

echo "== 2. NetworkManager profile $CON on $IFACE"
if ! nmcli -t -f NAME con show | grep -qx "$CON"; then
    nmcli con add type ethernet ifname "$IFACE" con-name "$CON" >/dev/null
fi
nmcli con modify "$CON" ipv4.method manual ipv4.addresses "$HOST_CIDR" ipv4.gateway "" \
    ipv4.never-default yes ipv4.ignore-auto-dns yes ipv6.method disabled connection.autoconnect yes
nmcli con up "$CON" >/dev/null 2>&1 || echo "   (no link yet: comes up when the EVK is connected and powered)"

echo "== 3. dnsmasq (DHCP + TFTP on $IFACE only)"
install -d -o "$OWNER" -g "$OWNER" -m 0755 "$TFTP_ROOT"
mkdir -p /etc/dnsmasq.d
cat > "$DNSMASQ_CONF" <<EOF
# RZ/G3L EVK direct link - rz-mcp workflow/board/setup-host.sh
port=0
interface=$IFACE
bind-dynamic
dhcp-range=$DHCP_RANGE
dhcp-option=option:router
dhcp-option=option:dns-server
enable-tftp
tftp-root=$TFTP_ROOT
EOF
if ! dpkg -s dnsmasq >/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y dnsmasq
fi
systemctl enable dnsmasq >/dev/null 2>&1
systemctl restart dnsmasq

echo "== 4. ufw"
if ufw_active; then ufw allow in on "$IFACE" >/dev/null; echo "   allowed incoming on $IFACE"; else echo "   ufw not active; nothing to do"; fi

echo "== check"
echo "   dnsmasq: $(systemctl is-active dnsmasq)"
echo "   $CON: $(nmcli -t -f GENERAL.STATE con show "$CON" 2>/dev/null | cut -d: -f2 || echo inactive)"
ls -l /dev/rzg3l-evk-console 2>/dev/null || echo "   /dev/rzg3l-evk-console: appears when the EVK console (micro-USB 'UART Debug') is plugged in"
echo "   $TFTP_ROOT owned by $(stat -c %U "$TFTP_ROOT")"
