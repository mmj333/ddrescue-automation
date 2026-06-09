#!/bin/bash
# Smart partition detection combining multiple methods

detect_partition() {
    local part=$1
    local device=$2
    local output_dir=$3
    
    echo "=== Analyzing Partition #$part ==="
    
    # Method 1: File count heuristic
    local file_count=$(get_file_count $part)
    echo "File count: $file_count"
    
    # Method 2: Search for Windows markers and traceback
    local has_users_folder=0
    local has_windows_folder=0
    
    # Use expect to interact with RecuperaBit
    expect << EOF
spawn python3 $HOME/RecuperaBit/main.py $device -o $output_dir
expect "Type*to quit:"
send "load saved_scan\r"
expect ">"

# Search for NTUSER.DAT
send "search $part NTUSER.DAT\r"
expect {
    -re "(\[0-9\]+)\\s+NTUSER" {
        send "traceback $part \$expect_out(1,string)\r"
        expect {
            -re "/Users/|/Documents and Settings/" {
                set has_users_folder 1
            }
        }
    }
}

# Quick tree check for Windows folder
send "tree $part 1\r"
expect {
    -re "Windows|WINDOWS" {
        set has_windows_folder 1
    }
}

send "quit\r"
expect eof

# Return results
exit [expr \$has_users_folder * 10 + \$has_windows_folder]
EOF
    
    local result=$?
    local has_users=$((result / 10))
    local has_windows=$((result % 10))
    
    # Method 3: Score the partition
    local score=0
    
    # File count scoring
    if [ $file_count -ge 5000 ] && [ $file_count -le 50000 ]; then
        score=$((score + 50))
        echo "✓ Good file count range (+50)"
    elif [ $file_count -gt 100000 ]; then
        score=$((score - 30))
        echo "✗ Too many files, likely includes deleted (-30)"
    fi
    
    # Structure scoring
    if [ $has_users -eq 1 ]; then
        score=$((score + 40))
        echo "✓ Found Users folder via traceback (+40)"
    fi
    
    if [ $has_windows -eq 1 ]; then
        score=$((score + 30))
        echo "✓ Found Windows folder (+30)"
    fi
    
    echo "Total score: $score"
    echo
    
    return $score
}

# Main detection logic
DEVICE="$1"
OUTPUT_DIR="$2"

echo "=== Smart Partition Detection ==="
echo "Device: $DEVICE"
echo

# Get all recoverable partitions
# ... (partition collection code)

# Score each partition
BEST_PARTITION=""
BEST_SCORE=0

for part in $PARTITIONS; do
    detect_partition $part $DEVICE $OUTPUT_DIR
    score=$?
    
    if [ $score -gt $BEST_SCORE ]; then
        BEST_SCORE=$score
        BEST_PARTITION=$part
    fi
done

echo "=== Results ==="
echo "Best partition: #$BEST_PARTITION (score: $BEST_SCORE)"
echo
echo "Recommendation: restore $BEST_PARTITION all"