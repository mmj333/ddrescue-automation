#!/bin/bash
# analyze-mft-clusters.sh - Parse MFT to find data cluster locations
# Creates a domain file for targeted ddrescue recovery
# Usage: analyze-mft-clusters.sh <device> <partition_offset_sectors> <output_dir>
#
# Example: analyze-mft-clusters.sh /dev/sdc 2048 ./analysis_output

set -e

DEVICE="${1:-/dev/sdc}"
PARTITION_OFFSET="${2:-2048}"  # sectors (usually 2048 for modern drives = 1MB)
OUTPUT_DIR="${3:-./mft_analysis}"
CLUSTER_SIZE="${4:-4096}"  # bytes (usually 4096 for NTFS)

echo "=============================================="
echo "MFT Cluster Analysis Tool"
echo "=============================================="
echo "Device:           $DEVICE"
echo "Partition offset: $PARTITION_OFFSET sectors"
echo "Output directory: $OUTPUT_DIR"
echo "Cluster size:     $CLUSTER_SIZE bytes"
echo ""

# Create output directory
mkdir -p "$OUTPUT_DIR"

# Get filesystem info
echo "[1/5] Getting NTFS filesystem parameters..."
fsstat -o "$PARTITION_OFFSET" "$DEVICE" > "$OUTPUT_DIR/fsstat.txt" 2>&1 || true

# Extract key values from fsstat
CLUSTER_SIZE_DETECTED=$(grep "Cluster Size:" "$OUTPUT_DIR/fsstat.txt" | awk '{print $3}' || echo "$CLUSTER_SIZE")
TOTAL_CLUSTERS=$(grep "Total Cluster Range:" "$OUTPUT_DIR/fsstat.txt" | awk '{print $5}' | tr -d ')' || echo "unknown")

echo "  Cluster size: $CLUSTER_SIZE_DETECTED bytes"
echo "  Total clusters: $TOTAL_CLUSTERS"
echo ""

# List all files with their MFT entry numbers
echo "[2/5] Extracting file list from MFT..."
fls -r -p -o "$PARTITION_OFFSET" "$DEVICE" > "$OUTPUT_DIR/file_list.txt" 2>&1

FILE_COUNT=$(wc -l < "$OUTPUT_DIR/file_list.txt")
echo "  Found $FILE_COUNT entries (files + directories)"
echo ""

# Filter to just regular files (not deleted, not directories)
echo "[3/5] Filtering active files..."
grep -E "^r/r" "$OUTPUT_DIR/file_list.txt" > "$OUTPUT_DIR/active_files.txt" || true
ACTIVE_COUNT=$(wc -l < "$OUTPUT_DIR/active_files.txt")
echo "  Active files: $ACTIVE_COUNT"
echo ""

# Extract MFT entry numbers for active files
echo "[4/5] Extracting cluster locations for each file..."
> "$OUTPUT_DIR/cluster_ranges.txt"
> "$OUTPUT_DIR/file_clusters.txt"

# Process each file to get its data runs
PROCESSED=0
while IFS= read -r line; do
    # Extract MFT entry number (format: "r/r 1234-128-1: filename")
    MFT_ENTRY=$(echo "$line" | grep -oP '\d+-\d+-\d+' | head -1 | cut -d'-' -f1)
    FILENAME=$(echo "$line" | sed 's/^[^:]*:\s*//')

    if [ -n "$MFT_ENTRY" ]; then
        # Get istat output for this file
        ISTAT_OUT=$(istat -o "$PARTITION_OFFSET" "$DEVICE" "$MFT_ENTRY" 2>/dev/null) || continue

        # Extract data runs (cluster ranges)
        # Look for lines like "0 1 2345" in the $DATA attribute section
        DATA_RUNS=$(echo "$ISTAT_OUT" | awk '/^\$DATA Attribute/,/^\$/' | grep -E "^[0-9]+" | awk '{print $3}' | grep -v "^$")

        if [ -n "$DATA_RUNS" ]; then
            echo "$MFT_ENTRY|$FILENAME|$DATA_RUNS" >> "$OUTPUT_DIR/file_clusters.txt"
            echo "$DATA_RUNS" >> "$OUTPUT_DIR/cluster_ranges.txt"
        fi

        PROCESSED=$((PROCESSED + 1))
        if [ $((PROCESSED % 100)) -eq 0 ]; then
            echo -ne "  Processed $PROCESSED files...\r"
        fi
    fi
done < "$OUTPUT_DIR/active_files.txt"

echo "  Processed $PROCESSED files with data                    "
echo ""

# Create summary of cluster ranges
echo "[5/5] Generating cluster range summary..."

# Sort and merge overlapping/adjacent cluster ranges
sort -n "$OUTPUT_DIR/cluster_ranges.txt" | uniq > "$OUTPUT_DIR/clusters_sorted.txt"

# Count unique clusters
UNIQUE_CLUSTERS=$(wc -l < "$OUTPUT_DIR/clusters_sorted.txt")
echo "  Unique data clusters: $UNIQUE_CLUSTERS"

# Calculate total data size
if [ "$UNIQUE_CLUSTERS" -gt 0 ]; then
    TOTAL_DATA_BYTES=$((UNIQUE_CLUSTERS * CLUSTER_SIZE_DETECTED))
    TOTAL_DATA_GB=$(echo "scale=2; $TOTAL_DATA_BYTES / 1073741824" | bc)
    echo "  Estimated data size: ${TOTAL_DATA_GB} GB"
fi

echo ""
echo "=============================================="
echo "Analysis complete!"
echo "=============================================="
echo ""
echo "Output files:"
echo "  $OUTPUT_DIR/fsstat.txt        - Filesystem parameters"
echo "  $OUTPUT_DIR/file_list.txt     - All MFT entries"
echo "  $OUTPUT_DIR/active_files.txt  - Active (non-deleted) files"
echo "  $OUTPUT_DIR/file_clusters.txt - Files with their cluster locations"
echo "  $OUTPUT_DIR/clusters_sorted.txt - All data clusters (sorted)"
echo ""
echo "Next step: Run create-data-domain.sh to generate ddrescue domain file"
