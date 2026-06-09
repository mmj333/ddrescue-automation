#!/bin/bash
# Analyze RecuperaBit output to find best partition

DEVICE="$1"
OUTPUT_DIR="$2"

echo "=== Analyzing RecuperaBit Partitions ==="
echo "Device: $DEVICE"
echo

cd "$HOME/RecuperaBit"

# Run RecuperaBit briefly just to get partition info
timeout 60 python3 main.py "$DEVICE" -o "$OUTPUT_DIR" << EOF 2>&1 | grep -E "(Partition #|Analysis complete)"
info
quit
EOF

echo
echo "To manually check partitions in your current RecuperaBit session:"
echo "1. Type 'info' to see all partitions"
echo "2. Look for partitions marked as 'Recoverable' with file counts"
echo "3. Use 'recoverable N' where N is the partition number"
echo "4. Typically, the partition with the most files is your main data partition"
echo
echo "Example output:"
echo "  Partition #0 -> Partition (NTFS, 71.60 GB, 2877 files, Recoverable)"
echo "  Partition #1 -> Partition (NTFS, 100 MB, 15 files, Recoverable)"
echo
echo "In this case, you'd want partition #0 with 2877 files"