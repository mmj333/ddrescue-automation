#!/bin/bash
# Extract MFT from NTFS partition for visualization
# Usage: extract-mft.sh <partition> <output_dir> [mft_cluster] [cluster_size]

PARTITION="$1"
OUTPUT_DIR="$2"
MFT_CLUSTER="${3:-786432}"
CLUSTER_SIZE="${4:-4096}"

if [ -z "$PARTITION" ] || [ -z "$OUTPUT_DIR" ]; then
    echo "Usage: $0 <partition> <output_dir> [mft_cluster] [cluster_size]"
    echo "Example: $0 /dev/sdc1 ./12345Client_iterative 786432 4096"
    exit 1
fi

# Calculate MFT offset
MFT_OFFSET=$((MFT_CLUSTER * CLUSTER_SIZE))

# Extract 256MB of MFT (enough for most drives)
MFT_SIZE=$((256 * 1024 * 1024))
OUTPUT_FILE="$OUTPUT_DIR/mft.raw"

echo "Extracting MFT from $PARTITION"
echo "  MFT cluster: $MFT_CLUSTER"
echo "  Cluster size: $CLUSTER_SIZE"
echo "  MFT offset: $MFT_OFFSET bytes"
echo "  Extracting: $((MFT_SIZE / 1024 / 1024)) MB"
echo "  Output: $OUTPUT_FILE"
echo ""

# Use dd to extract
dd if="$PARTITION" of="$OUTPUT_FILE" bs=4096 skip=$((MFT_OFFSET / 4096)) count=$((MFT_SIZE / 4096)) status=progress 2>&1

# Verify extraction
if [ -f "$OUTPUT_FILE" ]; then
    SIZE=$(stat -c%s "$OUTPUT_FILE")
    echo ""
    echo "Extracted: $OUTPUT_FILE ($SIZE bytes)"

    # Check for FILE signature
    MAGIC=$(xxd -l 4 "$OUTPUT_FILE" 2>/dev/null | grep -o 'FILE' || echo "")
    if [ "$MAGIC" = "FILE" ]; then
        echo "Verified: Valid MFT signature (FILE)"

        # Count approximate number of records
        RECORDS=$((SIZE / 1024))
        echo "Approximate MFT records: $RECORDS"
    else
        echo "WARNING: No FILE signature found - MFT may not have been recovered yet"
        # Show first bytes for debugging
        echo "First 16 bytes:"
        xxd -l 16 "$OUTPUT_FILE"
    fi
else
    echo "ERROR: Extraction failed"
    exit 1
fi
