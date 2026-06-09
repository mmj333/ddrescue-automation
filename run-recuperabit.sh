#!/bin/bash
# Reusable RecuperaBit runner script

RECUPERABIT_DIR="${RECUPERABIT_DIR:-$HOME/RecuperaBit}"

# Check if device is provided as argument
DEVICE="${1:-/dev/loop0p2}"
OUTPUT_DIR="${2:-$HOME/recuperabit-recovery}"
LOG_FILE="$HOME/recuperabit-scan-$(date +%Y%m%d-%H%M%S).log"

echo "=== RecuperaBit Runner ==="
echo "Device: $DEVICE"
echo "Output: $OUTPUT_DIR"
echo "Log: $LOG_FILE"
echo

# Create output directory
mkdir -p "$OUTPUT_DIR"

# Change to RecuperaBit directory
cd "$RECUPERABIT_DIR"

# For automated scan, we'll use expect or provide input
# For now, let's create a simple interactive wrapper
echo "Starting RecuperaBit..."
echo "Commands:"
echo "  Press Enter to start scan"
echo "  'info' - Show partition info after scan"
echo "  'tree <id>' - Show directory structure"
echo "  'csv <id> file.csv' - Export file list"
echo "  'recoverd <id> <dir_id>' - Recover directory"
echo

# Run RecuperaBit
python3 main.py "$DEVICE" -o "$OUTPUT_DIR" 2>&1 | tee "$LOG_FILE"