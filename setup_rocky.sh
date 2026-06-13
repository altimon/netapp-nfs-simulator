#!/usr/bin/env bash
# Install and configure the NetApp ONTAP simulator + NFS server on Rocky Linux.
# Run as root: sudo bash setup_rocky.sh [options]
#
# Options:
#   --subnet   CIDR     NFS client subnet  (default: auto-detect from primary IP)
#   --port     PORT     Simulator SSH port (default: 2223)
#   --lif-ip   IP       Simulated data LIF IP shown in ONTAP CLI (default: this host's IP)
#   --nfs-pass PASS     Simulator SSH password (default: netapp123)

set -euo pipefail

SIM_DIR="/opt/netapp-sim"
NFS_EXPORT_DIR="/srv/netapp/interview_test"

# ── defaults (overridable via config.env or CLI) ─────────────────────────────
NETAPP_PORT=2223
NETAPP_USER=admin
NETAPP_PASS=netapp123
NFS_SUBNET=""
NFS_SERVER_IP=""
ADMIN_LIF_IP=""
DATA_LIF_IP=""

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load config.env if present next to the script
if [[ -f "$SCRIPT_DIR/config.env" ]]; then
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/config.env"
fi

# ── CLI overrides ─────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --subnet)   NFS_SUBNET="$2";    shift 2 ;;
        --port)     NETAPP_PORT="$2";   shift 2 ;;
        --lif-ip)   DATA_LIF_IP="$2";  shift 2 ;;
        --nfs-pass) NETAPP_PASS="$2";   shift 2 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

# ── auto-detect network if not set ───────────────────────────────────────────
HOST_IP="$(ip route get 1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1); exit}')"
if [[ -z "$HOST_IP" ]]; then
    HOST_IP="$(hostname -I | awk '{print $1}')"
fi

[[ -z "$NFS_SERVER_IP" ]] && NFS_SERVER_IP="$HOST_IP"
[[ -z "$DATA_LIF_IP"   ]] && DATA_LIF_IP="$HOST_IP"
[[ -z "$ADMIN_LIF_IP"  ]] && ADMIN_LIF_IP="$HOST_IP"

if [[ -z "$NFS_SUBNET" ]]; then
    # Derive /24 from host IP (works for typical lab setups)
    NFS_SUBNET="$(echo "$HOST_IP" | cut -d. -f1-3).0/24"
fi

echo "==> Configuration"
echo "    Host IP:      $HOST_IP"
echo "    NFS subnet:   $NFS_SUBNET"
echo "    Sim port:     $NETAPP_PORT"
echo "    Data LIF IP:  $DATA_LIF_IP"
echo ""

# ── dependencies ─────────────────────────────────────────────────────────────
echo "==> Installing dependencies"
dnf install -y python3 python3-pip nfs-utils

echo "==> Installing asyncssh"
pip3 install --quiet asyncssh

# ── deploy simulator files ────────────────────────────────────────────────────
echo "==> Creating simulator directory"
mkdir -p "$SIM_DIR"

echo "==> Copying simulator files"
cp "$SCRIPT_DIR/simulator.py"      "$SIM_DIR/"
cp "$SCRIPT_DIR/tasks.py"          "$SIM_DIR/"
cp "$SCRIPT_DIR/state.json"        "$SIM_DIR/state.default.json"

if [[ ! -f "$SIM_DIR/state.json" ]]; then
    cp "$SCRIPT_DIR/state.json" "$SIM_DIR/"
fi

# ── patch state.json LIF IPs ─────────────────────────────────────────────────
echo "==> Patching LIF IPs in state.json"
python3 - <<PYEOF
import json, sys
path = "$SIM_DIR/state.json"
with open(path) as f:
    state = json.load(f)
for lif in state.get("lifs", []):
    if "data" in lif["name"] and "data" in lif.get("data_protocol",""):
        lif["address"] = "$DATA_LIF_IP"
    elif "admin" in lif["name"]:
        lif["address"] = "$ADMIN_LIF_IP"
# Also patch rad_nfs_policy clientmatch to match actual subnet
for svm in state.get("svms", []):
    pol = svm.get("export_policies", {}).get("rad_nfs_policy")
    if pol:
        for rule in pol["rules"]:
            rule["clientmatch"] = "$NFS_SUBNET"
with open(path, "w") as f:
    json.dump(state, f, indent=2)
print("  LIF IPs and export policy subnet updated.")
PYEOF

# ── write config.env ─────────────────────────────────────────────────────────
echo "==> Writing $SIM_DIR/config.env"
cat > "$SIM_DIR/config.env" <<EOF
NETAPP_PORT=$NETAPP_PORT
NETAPP_USER=$NETAPP_USER
NETAPP_PASS=$NETAPP_PASS
NETAPP_STATE=$SIM_DIR/state.json
NETAPP_HOST_KEY=$SIM_DIR/host_key
NFS_SUBNET=$NFS_SUBNET
NFS_SERVER_IP=$NFS_SERVER_IP
DATA_LIF_IP=$DATA_LIF_IP
ADMIN_LIF_IP=$ADMIN_LIF_IP
EOF

# ── NFS server ────────────────────────────────────────────────────────────────
echo "==> Creating NFS export directory"
mkdir -p "$NFS_EXPORT_DIR"
chown nobody:nobody "$NFS_EXPORT_DIR"
chmod 0777 "$NFS_EXPORT_DIR"

echo "==> Writing /etc/exports"
printf '%s  %s(rw,sync,no_subtree_check,no_root_squash)\n' \
    "$NFS_EXPORT_DIR" "$NFS_SUBNET" > /etc/exports
cat /etc/exports

echo "==> Enabling and starting NFS server"
systemctl enable --now nfs-server
exportfs -rav

# ── firewall ──────────────────────────────────────────────────────────────────
echo "==> Configuring firewall"
if systemctl is-active --quiet firewalld; then
    firewall-cmd --permanent --add-service=nfs
    firewall-cmd --permanent --add-service=mountd
    firewall-cmd --permanent --add-service=rpc-bind
    firewall-cmd --permanent --add-port="${NETAPP_PORT}/tcp"
    firewall-cmd --reload
    echo "  firewalld rules applied."
else
    echo "  firewalld not running — skipping (add rules manually if needed)."
fi

# ── systemd service ───────────────────────────────────────────────────────────
echo "==> Installing systemd service"
cp "$SCRIPT_DIR/netapp-sim.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now netapp-sim

# ── done ──────────────────────────────────────────────────────────────────────
echo ""
echo "==> Setup complete!"
echo ""
echo "  Simulator:  ssh ${NETAPP_USER}@${HOST_IP} -p ${NETAPP_PORT}  (password: ${NETAPP_PASS})"
echo "  NFS export: ${HOST_IP}:${NFS_EXPORT_DIR}"
echo "  Mount cmd:  sudo mount -t nfs ${HOST_IP}:${NFS_EXPORT_DIR} /mnt"
echo ""
echo "Service commands:"
echo "  systemctl status netapp-sim"
echo "  systemctl restart netapp-sim"
echo "  journalctl -u netapp-sim -f"
echo ""
echo "Reset simulator state:"
echo "  python3 ${SIM_DIR}/simulator.py --reset"
