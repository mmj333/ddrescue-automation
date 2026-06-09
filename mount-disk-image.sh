#!/bin/bash

# Script to mount full disk images with multiple partitions
# Supports drag-and-drop of image files

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Function to print colored output
print_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

# Function to cleanup on exit
cleanup() {
    if [ -n "$LOOP_DEVICE" ]; then
        print_info "Cleaning up loop device $LOOP_DEVICE"
        sudo losetup -d "$LOOP_DEVICE" 2>/dev/null || true
    fi
}

trap cleanup EXIT

# Check for required tools
for tool in losetup parted mount; do
    if ! command -v $tool &> /dev/null; then
        print_error "$tool is required but not installed"
        exit 1
    fi
done

# Get image file
if [ -n "$1" ]; then
    IMAGE_FILE="$1"
else
    echo "Enter path to disk image file (you can drag and drop):"
    read -r IMAGE_FILE
    # Remove quotes if present (from drag and drop)
    IMAGE_FILE="${IMAGE_FILE%\"}"
    IMAGE_FILE="${IMAGE_FILE#\"}"
    IMAGE_FILE="${IMAGE_FILE%\'}"
    IMAGE_FILE="${IMAGE_FILE#\'}"
fi

# Verify image file exists
if [ ! -f "$IMAGE_FILE" ]; then
    print_error "Image file not found: $IMAGE_FILE"
    exit 1
fi

print_info "Working with image: $IMAGE_FILE"

# Create loop device
print_info "Creating loop device..."
LOOP_DEVICE=$(sudo losetup --show -f -P "$IMAGE_FILE")
if [ -z "$LOOP_DEVICE" ]; then
    print_error "Failed to create loop device"
    exit 1
fi
print_info "Loop device created: $LOOP_DEVICE"

# Wait for partitions to be detected
sleep 2

# List partitions
print_info "Detecting partitions..."
echo
sudo parted "$LOOP_DEVICE" print
echo

# Find available partitions
PARTITIONS=$(ls ${LOOP_DEVICE}p* 2>/dev/null || ls ${LOOP_DEVICE}* | grep -E "${LOOP_DEVICE}[0-9]+" || true)

if [ -z "$PARTITIONS" ]; then
    print_warning "No partitions found in image"
    print_info "This might be a raw filesystem image. Try mounting $LOOP_DEVICE directly."
    exit 1
fi

# Create mount base directory
MOUNT_BASE="/mnt/recovery-$(basename "$IMAGE_FILE" .img)-$(date +%s)"
print_info "Creating mount directory: $MOUNT_BASE"
sudo mkdir -p "$MOUNT_BASE"

# Mount each partition
echo
print_info "Mounting partitions..."
MOUNTED_COUNT=0

for PARTITION in $PARTITIONS; do
    PART_NUM=$(echo "$PARTITION" | grep -o '[0-9]*$')
    MOUNT_POINT="$MOUNT_BASE/partition${PART_NUM}"
    
    print_info "Attempting to mount $PARTITION to $MOUNT_POINT"
    sudo mkdir -p "$MOUNT_POINT"
    
    # Try to detect filesystem type
    FS_TYPE=$(sudo blkid -o value -s TYPE "$PARTITION" 2>/dev/null || echo "auto")
    
    # Try to mount
    if sudo mount -t "$FS_TYPE" -o ro "$PARTITION" "$MOUNT_POINT" 2>/dev/null; then
        print_info "✓ Successfully mounted $PARTITION ($FS_TYPE) at $MOUNT_POINT"
        MOUNTED_COUNT=$((MOUNTED_COUNT + 1))
    else
        print_warning "✗ Failed to mount $PARTITION"
        sudo rmdir "$MOUNT_POINT" 2>/dev/null
    fi
done

echo
if [ $MOUNTED_COUNT -eq 0 ]; then
    print_error "No partitions could be mounted"
    sudo rmdir "$MOUNT_BASE" 2>/dev/null
    exit 1
fi

print_info "Successfully mounted $MOUNTED_COUNT partition(s)"
echo
print_info "Mounted partitions are available at:"
ls -la "$MOUNT_BASE"
echo

# Show disk usage
print_info "Partition sizes:"
df -h "$MOUNT_BASE"/*
echo

# Interactive menu
while true; do
    echo "Options:"
    echo "1. Browse mounted partitions"
    echo "2. Show partition details"
    echo "3. Unmount and exit"
    echo
    read -p "Select option [1-3]: " choice
    
    case $choice in
        1)
            print_info "Opening file browser..."
            if command -v nautilus &> /dev/null; then
                nautilus "$MOUNT_BASE" &
            elif command -v thunar &> /dev/null; then
                thunar "$MOUNT_BASE" &
            elif command -v dolphin &> /dev/null; then
                dolphin "$MOUNT_BASE" &
            else
                print_info "No GUI file browser found. Mount points:"
                echo "$MOUNT_BASE"
            fi
            ;;
        2)
            for mp in "$MOUNT_BASE"/*; do
                if mountpoint -q "$mp" 2>/dev/null; then
                    echo
                    print_info "=== $(basename "$mp") ==="
                    df -h "$mp"
                    echo "Files: $(sudo find "$mp" -type f 2>/dev/null | wc -l)"
                    echo "Directories: $(sudo find "$mp" -type d 2>/dev/null | wc -l)"
                fi
            done
            echo
            ;;
        3)
            print_info "Unmounting partitions..."
            for mp in "$MOUNT_BASE"/*; do
                if mountpoint -q "$mp" 2>/dev/null; then
                    sudo umount "$mp"
                    print_info "Unmounted $(basename "$mp")"
                fi
            done
            sudo rmdir "$MOUNT_BASE"/* 2>/dev/null
            sudo rmdir "$MOUNT_BASE" 2>/dev/null
            print_info "Cleanup complete"
            exit 0
            ;;
        *)
            print_error "Invalid option"
            ;;
    esac
    echo
done