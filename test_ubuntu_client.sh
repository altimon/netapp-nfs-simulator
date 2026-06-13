#!/usr/bin/env bash
# NFS client test script — run on Ubuntu VM.
# Usage: bash test_ubuntu_client.sh <rocky-vm-ip>

set -euo pipefail

ROCKY_IP="${1:-192.168.52.x}"
NFS_PATH="/interview_test"
MOUNT_POINT="/mnt"

echo "==> Checking NFS exports from $ROCKY_IP"
showmount -e "$ROCKY_IP"

echo ""
echo "==> Mounting NFS share"
sudo mount -t nfs "${ROCKY_IP}:${NFS_PATH}" "$MOUNT_POINT"

echo ""
echo "==> Verifying mount"
findmnt "$MOUNT_POINT"

echo ""
echo "==> NFS mount stats"
nfsstat -m 2>/dev/null || mount | grep nfs

echo ""
echo "==> Creating hello_world file"
touch "${MOUNT_POINT}/hello_world"
ls -l "${MOUNT_POINT}/hello_world"

echo ""
echo "==> All tests passed!"
echo ""
echo "To unmount:"
echo "  sudo umount $MOUNT_POINT"
