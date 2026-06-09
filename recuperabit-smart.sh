#!/bin/bash
# Smart RecuperaBit partition selector

DEVICE="$1"
OUTPUT_DIR="$2"
SAVE_NAME="$3"
LOG_FILE="$4"

RECUPERABIT_DIR="$HOME/RecuperaBit"
cd "$RECUPERABIT_DIR"

echo "=== Smart RecuperaBit Recovery ==="
echo "Analyzing partitions to find the best one to recover..."
echo

# First, get partition information
PARTITION_INFO=$(mktemp)

# Run RecuperaBit just to get partition info
python3 main.py "$DEVICE" -o "$OUTPUT_DIR" 2>&1 << EOF | tee "$PARTITION_INFO"
info
quit
EOF

# Parse partition information
echo
echo "=== Partition Analysis ==="

# Look for lines like:
# Partition #0 -> Partition (NTFS, 71.60 GB, 2877 files, Recoverable)
# Extract partition number, size, file count, and status

BEST_PARTITION=""
BEST_FILE_COUNT=0
PARTITION_COUNT=0

while IFS= read -r line; do
    if [[ $line =~ Partition\ #([0-9]+).*NTFS.*([0-9]+)\ files.*Recoverable ]]; then
        PART_NUM="${BASH_REMATCH[1]}"
        FILE_COUNT="${BASH_REMATCH[2]}"
        PARTITION_COUNT=$((PARTITION_COUNT + 1))
        
        echo "Found recoverable partition #$PART_NUM with $FILE_COUNT files"
        
        if [ "$FILE_COUNT" -gt "$BEST_FILE_COUNT" ]; then
            BEST_PARTITION="$PART_NUM"
            BEST_FILE_COUNT="$FILE_COUNT"
        fi
    fi
done < "$PARTITION_INFO"

echo
echo "Total partitions found: $PARTITION_COUNT"

if [ -z "$BEST_PARTITION" ]; then
    echo "No recoverable NTFS partitions found!"
    echo "Defaulting to partition 0"
    BEST_PARTITION="0"
else
    echo "Selected partition #$BEST_PARTITION with $BEST_FILE_COUNT files"
fi

# Now run the actual recovery
echo
echo "Starting recovery from partition #$BEST_PARTITION..."

cat > /tmp/recuperabit-smart.exp << EOF
#!/usr/bin/expect -f
set partition $BEST_PARTITION
set save_name "$SAVE_NAME"
set device "$DEVICE"
set output_dir "$OUTPUT_DIR"
set log_file "$LOG_FILE"

log_file -a \$log_file
spawn python3 main.py \$device -o \$output_dir

# Wait for prompt
expect "Type*to quit:"

# Check if save exists
send "load \$save_name\r"
expect {
    "Scanner loaded" {
        puts "Loaded existing scan"
    }
    "No such" {
        puts "No save found, scanning..."
        send "\r"
        set timeout 1800
        expect "INFO:root:Analysis complete!"
        sleep 2
        send "save \$save_name\r"
        expect ">"
    }
}

# Show info
send "info\r"
expect ">"

# Show files in selected partition
send "recoverable \$partition\r"
expect ">"

# Recover all files from selected partition
send "restore \$partition all\r"

# Wait for completion
set timeout 3600
expect {
    ">" { send "quit\r" }
    timeout { send "quit\r" }
}
expect eof
EOF

chmod +x /tmp/recuperabit-smart.exp
expect /tmp/recuperabit-smart.exp

# Cleanup
rm -f "$PARTITION_INFO" /tmp/recuperabit-smart.exp

echo
echo "Recovery complete!"