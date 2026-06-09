#!/bin/bash
# Run RecuperaBit in background with initial scan

RECUPERABIT_DIR="${RECUPERABIT_DIR:-$HOME/RecuperaBit}"
OUTPUT_DIR="$HOME/recuperabit-recovery"
LOG_FILE="$HOME/recuperabit-scan-$(date +%Y%m%d-%H%M%S).log"

echo "=== Starting RecuperaBit Background Scan ==="
echo "Log: $LOG_FILE"
echo "Output: $OUTPUT_DIR"
echo

# Ensure output directory exists
mkdir -p "$OUTPUT_DIR"

# Start RecuperaBit with automated initial scan
cd "$RECUPERABIT_DIR"

# Use expect or here-doc to automate initial interaction
(
  echo ""  # Press enter to start scan
  sleep 600  # Wait 10 minutes for scan
  echo "info"  # Show partition info
  sleep 5
  echo "quit"  # Exit
) | python3 main.py /dev/loop0p2 -o "$OUTPUT_DIR" > "$LOG_FILE" 2>&1 &

PID=$!
echo "RecuperaBit started with PID: $PID"
echo
echo "Monitor progress with:"
echo "  tail -f $LOG_FILE"
echo
echo "After scan completes (~10-30 min), check results and run interactively:"
echo "  cd $RECUPERABIT_DIR"
echo "  sudo python3 main.py /dev/loop0p2 -o $OUTPUT_DIR"
echo
echo "Checking initial output..."
sleep 5
tail -20 "$LOG_FILE"