#!/bin/bash
# Diagnose why /dev/sdj2 HFS+ mount is effectively read-only

echo "=== Current mount ==="
mount | grep sdj

echo ""
echo "=== Kernel messages (hfs) ==="
dmesg | grep -i "hfs\|sdj" | tail -20

echo ""
echo "=== Root write test ==="
touch /media/hfs/root_write_test.tmp 2>&1 && echo "Root CAN write" && rm -f /media/hfs/root_write_test.tmp || echo "Root CANNOT write (filesystem is truly read-only)"

echo ""
echo "=== Journal status (fsck -n) ==="
# fsck.hfsplus readonly check
fsck.hfsplus -n /dev/sdj2 2>&1 | head -20
