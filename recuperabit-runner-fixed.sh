#!/bin/bash
# Fixed RecuperaBit runner with proper path handling

RECUPERABIT_DIR="${RECUPERABIT_DIR:-$HOME/RecuperaBit}"

# Get arguments
DEVICE="${1:-/dev/loop0p2}"
OUTPUT_DIR="${2:-$HOME/recuperabit-recovery}"

# Remove any quotes from the output directory path
OUTPUT_DIR=$(echo "$OUTPUT_DIR" | sed "s/'//g" | sed 's/"//g')

# Use absolute path without quotes
OUTPUT_DIR=$(realpath "$OUTPUT_DIR" 2>/dev/null || echo "$OUTPUT_DIR")

LOG_FILE="$HOME/recuperabit-scan-$(date +%Y%m%d-%H%M%S).log"

echo "=== RecuperaBit Runner (Fixed) ==="
echo "Device: $DEVICE"
echo "Output: $OUTPUT_DIR"
echo "Log: $LOG_FILE"
echo

# Create output directory if it doesn't exist
mkdir -p "$OUTPUT_DIR"

# IMPORTANT: RecuperaBit creates subdirectories, so we should account for that
# It will create: OUTPUT_DIR/PartitionXXX/Root/...

# Change to RecuperaBit directory (REQUIRED - it looks for main.py here)
cd "$RECUPERABIT_DIR" || exit 1

echo "Starting RecuperaBit..."
echo "NOTE: Files will be saved to: $OUTPUT_DIR/PartitionXXX/Root/"
echo
echo "Commands:"
echo "  Press Enter to start scan"
echo "  'info' - Show partition info after scan"
echo "  'tree <part#>' - Show directory structure"
echo "  'restore <part#> <root_id>' - Restore from root (usually ID 5)"
echo

# Run RecuperaBit with clean output path (no quotes needed)
python3 main.py "$DEVICE" -o "$OUTPUT_DIR" 2>&1 | tee "$LOG_FILE"