#!/bin/bash
# Test RecuperaBit on damaged NTFS image

IMAGE_PATH="${IMAGE_PATH:-/path/to/disk.img}"  # override via: IMAGE_PATH=... ./test-recuperabit.sh
OUTPUT_DIR="$HOME/recuperabit-recovery"
LOG_FILE="$HOME/recuperabit.log"

echo "=== Testing RecuperaBit on damaged NTFS ==="
echo "Image: $IMAGE_PATH"
echo "Output: $OUTPUT_DIR"
echo

# Create output directory in home
mkdir -p "$OUTPUT_DIR"

# Ensure loop device is set up (need to use our sudo script)
if ! losetup -l | grep -q "$IMAGE_PATH"; then
    echo "Loop device not found. Please run:"
    echo "sudo losetup -P /dev/loop0 \"$IMAGE_PATH\""
    exit 1
fi

echo "Starting RecuperaBit scan..."
echo "This will analyze the damaged NTFS partition and may take a while."
echo "Output will be logged to: $LOG_FILE"
echo

# Run RecuperaBit on the partition
cd "${RECUPERABIT_DIR:-$HOME/RecuperaBit}"

# First, do a quick scan to see what it finds
echo "Running initial scan (this may take 10-30 minutes)..."
echo "Check progress with: tail -f $LOG_FILE"
echo

# Run with timeout first to see if it works
timeout 5m python3 main.py /dev/loop0p2 -o "$OUTPUT_DIR" 2>&1 | tee "$LOG_FILE"

echo
echo "Initial scan complete. Check $LOG_FILE for results."
echo "To run full interactive session: cd \"${RECUPERABIT_DIR:-$HOME/RecuperaBit}\" && python3 main.py /dev/loop0p2 -o $OUTPUT_DIR"