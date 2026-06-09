#!/bin/bash
# Smart partition analyzer for RecuperaBit
# Identifies the most likely "active" Windows partition

analyze_partition() {
    local partition_num=$1
    local temp_file="/tmp/recuperabit_tree_${partition_num}.txt"
    
    echo "Analyzing partition #$partition_num..."
    
    # Get directory tree (limited depth to avoid huge outputs)
    timeout 30 python3 "$HOME/RecuperaBit/main.py" "$DEVICE" -o "$OUTPUT_DIR" << EOF 2>&1 | tee "$temp_file"
tree $partition_num 3
quit
EOF
    
    # Score the partition based on Windows filesystem indicators
    local score=0
    local reasons=""
    
    # Check for key Windows directories
    if grep -qi "Windows" "$temp_file"; then
        score=$((score + 10))
        reasons="${reasons}Found Windows directory (+10); "
    fi
    
    if grep -qi "Program Files" "$temp_file"; then
        score=$((score + 10))
        reasons="${reasons}Found Program Files (+10); "
    fi
    
    if grep -qi "Users" "$temp_file"; then
        score=$((score + 15))
        reasons="${reasons}Found Users directory (+15); "
    fi
    
    if grep -qi "Documents and Settings" "$temp_file"; then
        score=$((score + 10))
        reasons="${reasons}Found Documents and Settings (+10); "
    fi
    
    # Check for user profile indicators
    if grep -qiE "(Desktop|Documents|Downloads|Pictures|Music|Videos)" "$temp_file"; then
        score=$((score + 20))
        reasons="${reasons}Found user profile folders (+20); "
    fi
    
    # Check for system files
    if grep -qiE "(System32|SysWOW64)" "$temp_file"; then
        score=$((score + 5))
        reasons="${reasons}Found system folders (+5); "
    fi
    
    # Negative indicators (suggests deleted/cache files)
    if grep -qiE "(Temporary Internet Files|cache|Cache|TEMP|tmp)" "$temp_file" | head -20 | grep -c "cache\|Cache\|TEMP" > 5; then
        score=$((score - 15))
        reasons="${reasons}Many cache/temp entries (-15); "
    fi
    
    # Check for excessive fragmentation (many entries at root)
    local root_entries=$(grep -c "^/" "$temp_file" | head -100)
    if [ "$root_entries" -gt 50 ]; then
        score=$((score - 10))
        reasons="${reasons}Too many root entries (-10); "
    fi
    
    # File count sanity check
    local file_count=$(grep -oP 'Partition #'$partition_num'.*?(\d+) files' "$INFO_FILE" | grep -oP '\d+ files' | grep -oP '\d+')
    if [ "$file_count" -gt 100000 ]; then
        score=$((score - 20))
        reasons="${reasons}Excessive file count suggests deleted files (-20); "
    fi
    
    echo "Partition #$partition_num score: $score"
    echo "Reasons: $reasons"
    echo
    
    rm -f "$temp_file"
    
    echo "$partition_num:$score:$reasons"
}

# Main analysis
DEVICE="$1"
OUTPUT_DIR="$2"
INFO_FILE="/tmp/recuperabit_info.txt"

echo "=== RecuperaBit Smart Partition Analyzer ==="
echo "Getting partition information..."
echo

cd "$HOME/RecuperaBit"

# First get basic partition info
timeout 30 python3 main.py "$DEVICE" -o "$OUTPUT_DIR" << EOF 2>&1 | tee "$INFO_FILE"
info
quit
EOF

# Extract recoverable partitions
PARTITIONS=$(grep -oP 'Partition #\d+.*Recoverable' "$INFO_FILE" | grep -oP '#\d+' | grep -oP '\d+')

echo "Found recoverable partitions: $PARTITIONS"
echo

# Analyze each partition
BEST_PARTITION=""
BEST_SCORE=-999

for part in $PARTITIONS; do
    result=$(analyze_partition "$part")
    part_num=$(echo "$result" | cut -d: -f1)
    score=$(echo "$result" | cut -d: -f2)
    
    if [ "$score" -gt "$BEST_SCORE" ]; then
        BEST_SCORE="$score"
        BEST_PARTITION="$part_num"
    fi
done

echo "=== Analysis Complete ==="
echo "Best partition: #$BEST_PARTITION (score: $BEST_SCORE)"
echo
echo "Recommendation: Use 'restore $BEST_PARTITION all' to recover this partition"

# Cleanup
rm -f "$INFO_FILE"