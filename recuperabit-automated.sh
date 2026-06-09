#!/bin/bash
# Automated RecuperaBit with ticket management

TICKET_NUMBER="$1"
CLIENT_NAME="$2"
DEVICE="$3"
OUTPUT_BASE="${4:-$HOME/recuperabit-recovery}"

# Validate inputs
if [ -z "$TICKET_NUMBER" ] || [ -z "$CLIENT_NAME" ] || [ -z "$DEVICE" ]; then
    echo "Usage: $0 <ticket_number> <client_name> <device> [output_base_dir]"
    echo "Example: $0 12345 ClientName /dev/loop0p2"
    exit 1
fi

# Sanitize names for filenames
SAFE_TICKET=$(echo "$TICKET_NUMBER" | sed 's/[^a-zA-Z0-9._-]/_/g')
SAFE_CLIENT=$(echo "$CLIENT_NAME" | sed 's/[^a-zA-Z0-9._-]/_/g')
TIMESTAMP=$(date +%Y%m%d-%H%M%S)

# Create paths
SAVE_NAME="${SAFE_TICKET}-${SAFE_CLIENT}"
OUTPUT_DIR="$OUTPUT_BASE/${SAVE_NAME}"
LOG_FILE="$HOME/recuperabit-${SAVE_NAME}-${TIMESTAMP}.log"
RECUPERABIT_DIR="$HOME/RecuperaBit"

echo "=== Automated RecuperaBit Recovery ==="
echo "Ticket: $TICKET_NUMBER"
echo "Client: $CLIENT_NAME"
echo "Device: $DEVICE"
echo "Save name: $SAVE_NAME"
echo "Output: $OUTPUT_DIR"
echo "Log: $LOG_FILE"
echo

# Create output directory
mkdir -p "$OUTPUT_DIR"

# Change to RecuperaBit directory
cd "$RECUPERABIT_DIR"

# Check if save file exists
SAVE_FILE="${SAVE_NAME}.save"
if [ -f "$SAVE_FILE" ]; then
    echo "Found existing save file: $SAVE_FILE"
    echo "Loading previous scan data..."
    
    # Create expect script for loading
    cat > /tmp/recuperabit-load.exp << 'EOF'
#!/usr/bin/expect -f
set save_name [lindex $argv 0]
set device [lindex $argv 1]
set output_dir [lindex $argv 2]
set log_file [lindex $argv 3]

log_file -a $log_file
spawn python3 main.py $device -o $output_dir

# Wait for prompt
expect "Type*to quit:"

# Load saved state
send "load $save_name\r"
expect ">"

# Show info and capture output to determine best partition
send "info\r"
expect {
    -re "Partition #(\[0-9\]+).*NTFS.*(\[0-9\]+) files.*Recoverable" {
        set part_num $expect_out(1,string)
        set file_count $expect_out(2,string)
        puts "Found recoverable NTFS partition #$part_num with $file_count files"
        exp_continue
    }
    ">" {
        # Done parsing partitions
    }
}

# For now, use partition 0, but this could be enhanced
# to select the partition with the most files
set selected_partition 0

# Show files in selected partition
send "recoverable $selected_partition\r"
expect ">"

# Recover all files from selected partition
send "restore $selected_partition all\r"

# Wait for completion (adjust timeout as needed)
set timeout 3600
expect {
    ">" { send "quit\r" }
    timeout { send "quit\r" }
}
expect eof
EOF
    
    chmod +x /tmp/recuperabit-load.exp
    expect /tmp/recuperabit-load.exp "$SAVE_NAME" "$DEVICE" "$OUTPUT_DIR" "$LOG_FILE"
    
else
    echo "No save file found. Running new scan..."
    echo "This will take 10-30 minutes..."
    
    # Create expect script for new scan
    cat > /tmp/recuperabit-scan.exp << 'EOF'
#!/usr/bin/expect -f
set save_name [lindex $argv 0]
set device [lindex $argv 1]
set output_dir [lindex $argv 2]
set log_file [lindex $argv 3]

log_file -a $log_file
spawn python3 main.py $device -o $output_dir

# Wait for prompt
expect "Type*to quit:"

# Start scan
send "\r"

# Wait for analysis to complete (long timeout)
set timeout 1800
expect "INFO:root:Analysis complete!"

# Wait a bit for prompt
sleep 2

# Show info
send "info\r"
expect ">"

# Save the scan
send "save $save_name\r"
expect ">"

# Show recoverable files from partition 0
send "recoverable 0\r"
expect ">"

# Recover all files
send "restore 0 all\r"

# Wait for recovery completion
set timeout 3600
expect {
    ">" { send "quit\r" }
    timeout { send "quit\r" }
}
expect eof
EOF
    
    chmod +x /tmp/recuperabit-scan.exp
    expect /tmp/recuperabit-scan.exp "$SAVE_NAME" "$DEVICE" "$OUTPUT_DIR" "$LOG_FILE"
fi

echo
echo "=== Recovery Complete ==="
echo "Files saved to: $OUTPUT_DIR"
echo "Log file: $LOG_FILE"
echo
echo "Summary:"
if [ -d "$OUTPUT_DIR" ]; then
    FILE_COUNT=$(find "$OUTPUT_DIR" -type f | wc -l)
    DIR_COUNT=$(find "$OUTPUT_DIR" -type d | wc -l)
    TOTAL_SIZE=$(du -sh "$OUTPUT_DIR" | cut -f1)
    
    echo "  Recovered files: $FILE_COUNT"
    echo "  Directories: $DIR_COUNT"
    echo "  Total size: $TOTAL_SIZE"
else
    echo "  No output directory found - check log for errors"
fi

# Clean up
rm -f /tmp/recuperabit-load.exp /tmp/recuperabit-scan.exp