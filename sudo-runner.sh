#!/bin/bash
# Universal sudo runner - accepts scripts as arguments
# Usage: sudo /path/to/sudo-runner.sh /path/to/script.sh [args...]

if [ $# -eq 0 ]; then
    echo "Usage: $0 <script-to-run> [arguments...]"
    echo "Example: $0 \"$(dirname "$(readlink -f "$0")")/run-recuperabit.sh\" /dev/loop0p2"
    exit 1
fi

SCRIPT_TO_RUN="$1"
shift  # Remove first argument, pass the rest to the script

if [ ! -f "$SCRIPT_TO_RUN" ]; then
    echo "Error: Script not found: $SCRIPT_TO_RUN"
    exit 1
fi

if [ ! -x "$SCRIPT_TO_RUN" ]; then
    echo "Warning: Script is not executable, attempting to run anyway"
fi

echo "=== Running with sudo: $SCRIPT_TO_RUN ==="
echo "Arguments: $@"
echo

# Execute the script with remaining arguments
"$SCRIPT_TO_RUN" "$@"