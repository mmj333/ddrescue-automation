#!/bin/bash

# Recovery Manager - Comprehensive data recovery management tools
# Includes: DDRescue automation, disk image mounting, and file recovery
# Version: 1.1.0

UDEV_RULES_DIR="/etc/udev/rules.d"
SUDOERS_FILE="/etc/sudoers.d/recovery-sudo"
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
# RSYNC_RECOVERY_DIR is assigned below, after get_real_home is defined.

# Color definitions
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
PURPLE='\033[0;35m'
NC='\033[0m' # No Color

# Function to get the real user's home directory (handles sudo)
get_real_home() {
    if [ -n "$SUDO_USER" ]; then
        echo "/home/$SUDO_USER"
    else
        echo "$HOME"
    fi
}

# Session tracking directory
DDRESCUE_SESSION_DIR="$(get_real_home)/.ddrescue_recovery"

# Companion rsync-recovery checkout (override with env var if elsewhere)
RSYNC_RECOVERY_DIR="${RSYNC_RECOVERY_DIR:-$(get_real_home)/Projects/rsync-recovery}"

# Auto-elevate to sudo if not already running as root
if [ "$EUID" -ne 0 ]; then
    echo -e "${YELLOW}This script requires sudo privileges for disk operations.${NC}"
    echo -e "${CYAN}Elevating privileges...${NC}"
    echo ""
    # Preserve DISPLAY and XAUTHORITY for GUI apps if needed
    sudo DISPLAY="$DISPLAY" XAUTHORITY="$XAUTHORITY" "$0" "$@"
    exit $?
fi

# Resize terminal window for better visibility (40 rows x 100 columns)
# Only if running in a terminal that supports it
if [ -t 1 ]; then
    printf '\e[8;40;100t' 2>/dev/null
fi

# Source partition analyzer from rsync_recovery if available
# Note: Save SCRIPT_DIR first as partition_analyzer.sh overwrites it
DDRESCUE_SCRIPT_DIR="$SCRIPT_DIR"
if [ -f "$RSYNC_RECOVERY_DIR/partition_analyzer.sh" ]; then
    source "$RSYNC_RECOVERY_DIR/partition_analyzer.sh"
    PARTITION_ANALYZER_AVAILABLE=true
else
    PARTITION_ANALYZER_AVAILABLE=false
fi
# Restore our script directory
SCRIPT_DIR="$DDRESCUE_SCRIPT_DIR"

#######################################
# DDRescue Destination Selection
#######################################

# Show available destination drives for ddrescue (whole-drive cloning)
# Filters by total size, shows partition info to help identify drives with data
# Usage: show_ddrescue_destinations <source_device> <source_size_bytes>
# Sets: DDRESCUE_DEST_DRIVES array, DDRESCUE_DEST_COUNT
show_ddrescue_destinations() {
    local source_dev="$1"
    local source_size="$2"
    local source_size_gb=$((source_size / 1024 / 1024 / 1024))

    # Strip partition number from source to get base device
    local source_base=$(echo "$source_dev" | sed 's/[0-9]*$//' | sed 's/p$//')

    echo -e "${GREEN}Select Destination Drive${NC}"
    echo "========================="
    echo -e "Source: ${CYAN}${source_dev}${NC} (${source_size_gb} GB) - destination must be >= this size"
    echo ""

    DDRESCUE_DEST_DRIVES=()
    DDRESCUE_DEST_COUNT=0
    local display_count=0

    # Get all block devices (whole drives only, no partitions)
    while read -r line; do
        local dev_name=$(echo "$line" | awk '{print $1}')
        local dev_size=$(echo "$line" | awk '{print $2}')
        local dev_model=$(echo "$line" | awk '{$1=$2=""; print $0}' | sed 's/^ *//')

        local dev_path="/dev/$dev_name"

        # Skip if this is the source drive
        if [ "$dev_path" = "$source_base" ] || [ "$dev_path" = "$source_dev" ]; then
            continue
        fi

        # Skip loop devices, ram disks, etc.
        if [[ "$dev_name" =~ ^(loop|ram|zram) ]]; then
            continue
        fi

        # Get size in bytes
        local size_bytes=$(blockdev --getsize64 "$dev_path" 2>/dev/null)
        if [ -z "$size_bytes" ] || [ "$size_bytes" -eq 0 ]; then
            continue
        fi

        local size_gb=$((size_bytes / 1024 / 1024 / 1024))
        display_count=$((display_count + 1))

        # Check size compatibility
        local size_ok="false"
        local size_indicator=""
        if [ "$size_bytes" -ge "$source_size" ]; then
            size_ok="true"
            size_indicator="${GREEN}✓${NC}"
        else
            size_indicator="${RED}✗ TOO SMALL${NC}"
        fi

        # Get partition and filesystem info (to identify drives with data)
        local part_info=""
        local has_data="false"
        local partitions=$(lsblk -no NAME,SIZE,FSTYPE,LABEL "$dev_path" 2>/dev/null | tail -n +2)

        if [ -n "$partitions" ]; then
            has_data="true"
            # Summarize partitions
            local part_count=$(echo "$partitions" | wc -l)
            local fs_types=$(echo "$partitions" | awk '{print $3}' | sort -u | grep -v "^$" | tr '\n' ',' | sed 's/,$//')
            local labels=$(echo "$partitions" | awk '{print $4}' | grep -v "^$" | head -2 | tr '\n' ',' | sed 's/,$//')

            if [ -n "$fs_types" ]; then
                part_info="${YELLOW}${part_count} partition(s): ${fs_types}${NC}"
                if [ -n "$labels" ]; then
                    part_info="$part_info ${YELLOW}[$labels]${NC}"
                fi
            fi
        else
            part_info="${GREEN}(empty/unformatted)${NC}"
        fi

        # Display the drive
        printf "  [%d] %-12s %4d GB  %-20s %s\n" "$display_count" "$dev_path" "$size_gb" "$dev_model" ""
        echo -e "      $size_indicator  $part_info"

        # Store in array (only if size is OK)
        if [ "$size_ok" = "true" ]; then
            DDRESCUE_DEST_DRIVES+=("$dev_path|$size_bytes|$dev_model|$has_data")
        else
            # Still store but mark as too small
            DDRESCUE_DEST_DRIVES+=("$dev_path|$size_bytes|$dev_model|$has_data|TOOSMALL")
        fi

    done < <(lsblk -d -n -o NAME,SIZE,MODEL 2>/dev/null | grep -E "^(sd|nvme|hd)")

    DDRESCUE_DEST_COUNT=$display_count

    echo ""
    echo "  [M] Enter device path manually"
    echo "  [I] Image file (write to a file on a mounted filesystem)"
    echo ""
}

# Select a ddrescue destination from the displayed list
# Usage: select_ddrescue_destination <source_size_bytes>
# Returns: Sets SELECTED_DEST_DEVICE or returns 1 on cancel/error
select_ddrescue_destination() {
    local source_size="$1"
    local source_size_gb=$((source_size / 1024 / 1024 / 1024))

    read -p "Select destination [1-$DDRESCUE_DEST_COUNT, M=manual device, I=image file]: " choice

    if [[ "$choice" =~ ^[Ii]$ ]]; then
        select_image_file_destination "$source_size"
        return $?
    fi

    if [[ "$choice" =~ ^[Mm]$ ]]; then
        read -p "Enter destination device or image file path: " SELECTED_DEST_DEVICE

        # Allow either a block device or a regular file path (for image-file destinations)
        if [ -b "$SELECTED_DEST_DEVICE" ]; then
            :  # block device — fall through to size check below
        elif [ -f "$SELECTED_DEST_DEVICE" ] || [ -d "$(dirname "$SELECTED_DEST_DEVICE")" ]; then
            # Image file (existing or to-be-created). Free-space check handled downstream.
            return 0
        else
            echo -e "${RED}Error: $SELECTED_DEST_DEVICE is neither a block device nor a valid path${NC}"
            return 1
        fi

        # Check size
        local dest_size=$(blockdev --getsize64 "$SELECTED_DEST_DEVICE" 2>/dev/null)
        local dest_size_gb=$((dest_size / 1024 / 1024 / 1024))

        if [ "$dest_size" -lt "$source_size" ]; then
            echo -e "${RED}Error: Destination (${dest_size_gb} GB) is smaller than source (${source_size_gb} GB)${NC}"
            return 1
        fi

        return 0
    fi

    if [[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -ge 1 ] && [ "$choice" -le "$DDRESCUE_DEST_COUNT" ]; then
        local selected="${DDRESCUE_DEST_DRIVES[$((choice-1))]}"
        SELECTED_DEST_DEVICE=$(echo "$selected" | cut -d'|' -f1)

        # Check if marked as too small
        if echo "$selected" | grep -q "TOOSMALL"; then
            local dest_size=$(echo "$selected" | cut -d'|' -f2)
            local dest_size_gb=$((dest_size / 1024 / 1024 / 1024))
            echo -e "${RED}Error: Selected drive (${dest_size_gb} GB) is smaller than source (${source_size_gb} GB)${NC}"
            return 1
        fi

        return 0
    fi

    echo -e "${RED}Invalid selection${NC}"
    return 1
}

# Select an image-file destination on a mounted filesystem.
# Usage: select_image_file_destination <source_size_bytes>
# Returns: Sets SELECTED_DEST_DEVICE to the chosen file path; returns 1 on cancel/error.
select_image_file_destination() {
    local source_size="$1"
    local source_size_gb=$((source_size / 1024 / 1024 / 1024))

    echo ""
    echo -e "${GREEN}Image File Destination${NC}"
    echo "======================"
    echo -e "Need at least ${CYAN}${source_size_gb} GB${NC} of free space."
    echo ""
    echo "Mounted filesystems with sufficient free space:"
    echo ""

    local -a FS_PATHS=()
    local -a FS_FREE=()
    local idx=0

    while IFS= read -r line; do
        local mp avail used pcent fsname size_h
        mp=$(echo "$line" | awk '{print $6}')
        fsname=$(echo "$line" | awk '{print $1}')
        size_h=$(echo "$line" | awk '{print $2}')
        avail=$(echo "$line" | awk '{print $4}')

        [ -z "$mp" ] && continue
        case "$mp" in
            /|/boot|/boot/efi|/proc|/sys|/dev|/run|/snap*|/var/snap*) continue ;;
        esac

        local avail_bytes
        avail_bytes=$(df -B1 --output=avail "$mp" 2>/dev/null | tail -1 | tr -d ' ')
        # Fallback for FUSE/NTFS mounts where df -B1 returns empty or non-numeric
        if [ -z "$avail_bytes" ] || ! [[ "$avail_bytes" =~ ^[0-9]+$ ]]; then
            avail_bytes=$(python3 -c "import os; s=os.statvfs('$mp'); print(s.f_bavail*s.f_frsize)" 2>/dev/null)
        fi
        [ -z "$avail_bytes" ] && continue
        if [ "$avail_bytes" -lt "$source_size" ]; then
            continue
        fi

        idx=$((idx + 1))
        FS_PATHS+=("$mp")
        FS_FREE+=("$avail")
        printf "  [%d] %-40s  size: %-8s free: %s\n" "$idx" "$mp" "$size_h" "$avail"
    done < <(df -h --output=source,size,used,avail,pcent,target 2>/dev/null | tail -n +2)

    if [ "$idx" -eq 0 ]; then
        echo -e "  ${RED}No mounted filesystem has ${source_size_gb} GB free.${NC}"
        echo ""
        echo "  You can still enter a path manually (e.g. on a network share)."
    fi
    echo ""
    echo "  [M] Enter path manually"
    echo ""

    local choice
    read -p "Select destination filesystem [1-$idx, M]: " choice

    local target_dir=""
    if [[ "$choice" =~ ^[Mm]$ ]]; then
        read -p "Enter destination directory: " target_dir
    elif [[ "$choice" =~ ^/ ]]; then
        # User typed a path directly at the prompt — accept it
        target_dir="$choice"
    elif [[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -ge 1 ] && [ "$choice" -le "$idx" ]; then
        target_dir="${FS_PATHS[$((choice-1))]}"
    else
        echo -e "${RED}Invalid selection${NC}"
        return 1
    fi

    if [ ! -d "$target_dir" ]; then
        read -p "Directory does not exist. Create $target_dir? [y/N]: " mk
        if [[ "$mk" =~ ^[Yy]$ ]]; then
            mkdir -p "$target_dir" || { echo -e "${RED}mkdir failed${NC}"; return 1; }
        else
            return 1
        fi
    fi

    local default_name="${JOB_NAME:-recovery}.img"
    local image_name
    read -p "Image filename [$default_name]: " image_name
    image_name="${image_name:-$default_name}"
    [[ "$image_name" != *.img && "$image_name" != *.dd && "$image_name" != *.bin ]] && image_name="${image_name}.img"

    local image_path="${target_dir%/}/${image_name}"

    # Verify free space on the chosen target
    local avail_bytes
    avail_bytes=$(df -B1 --output=avail "$target_dir" 2>/dev/null | tail -1 | tr -d ' ')
    if [ -n "$avail_bytes" ] && [ "$avail_bytes" -lt "$source_size" ]; then
        local avail_gb=$((avail_bytes / 1024 / 1024 / 1024))
        echo -e "${RED}Error: $target_dir has ${avail_gb} GB free, need ${source_size_gb} GB${NC}"
        return 1
    fi

    if [ -e "$image_path" ]; then
        echo -e "${YELLOW}Warning: $image_path already exists.${NC}"
        read -p "Overwrite? [y/N]: " ow
        [[ ! "$ow" =~ ^[Yy]$ ]] && return 1
    fi

    SELECTED_DEST_DEVICE="$image_path"
    echo ""
    echo -e "${GREEN}Image destination: $SELECTED_DEST_DEVICE${NC}"
    return 0
}

#######################################
# Session Tracking Functions
#######################################

# Get most recent ticket/customer from session history
# Sets LAST_TICKET and LAST_CUSTOMER if found
get_last_session_info() {
    LAST_TICKET=""
    LAST_CUSTOMER=""
    if [ -d "$DDRESCUE_SESSION_DIR" ]; then
        local latest=$(ls -t "$DDRESCUE_SESSION_DIR"/session_* 2>/dev/null | head -1)
        if [ -n "$latest" ] && [ -f "$latest" ]; then
            LAST_TICKET=$(grep '^TICKET_NUMBER=' "$latest" 2>/dev/null | cut -d'"' -f2)
            LAST_CUSTOMER=$(grep '^CUSTOMER_NAME=' "$latest" 2>/dev/null | cut -d'"' -f2)
        fi
    fi
}

# Prompt for ticket and customer with suggestions from last session
# Sets: TICKET_NUMBER, CUSTOMER_NAME, CUSTOMER_SAFE, JOB_NAME
# Args: $1 = job name separator (empty or "_")
# Returns 1 on cancel
prompt_job_info() {
    local separator="${1:-}"

    get_last_session_info

    echo -e "${GREEN}Job Information${NC}"
    echo "---------------"

    if [ -n "$LAST_TICKET" ]; then
        echo -e "  ${CYAN}Last job: $LAST_TICKET - $LAST_CUSTOMER${NC}"
        read -p "Ticket number [$LAST_TICKET]: " TICKET_NUMBER
        TICKET_NUMBER=${TICKET_NUMBER:-$LAST_TICKET}
    else
        read -p "Ticket number: " TICKET_NUMBER
    fi

    if [ -z "$TICKET_NUMBER" ]; then
        echo -e "${YELLOW}Cancelled - ticket number required${NC}"
        return 1
    fi

    # If same ticket, suggest same customer
    local customer_default=""
    if [ "$TICKET_NUMBER" = "$LAST_TICKET" ] && [ -n "$LAST_CUSTOMER" ]; then
        customer_default="$LAST_CUSTOMER"
    fi

    if [ -n "$customer_default" ]; then
        read -p "Customer name [$customer_default]: " CUSTOMER_NAME
        CUSTOMER_NAME=${CUSTOMER_NAME:-$customer_default}
    else
        read -p "Customer name: " CUSTOMER_NAME
    fi

    if [ -z "$CUSTOMER_NAME" ]; then
        echo -e "${YELLOW}Cancelled - customer name required${NC}"
        return 1
    fi

    CUSTOMER_SAFE=$(echo "$CUSTOMER_NAME" | tr ' ' '_' | tr -cd '[:alnum:]_-')
    JOB_NAME="${TICKET_NUMBER}${separator}${CUSTOMER_SAFE}"
    return 0
}

# Save a ddrescue session for later resume
save_ddrescue_session() {
    local ticket="$1"
    local customer="$2"
    local source_dev="$3"
    local dest_dev="$4"
    local log_file="$5"
    local job_dir="$6"
    local mode="$7"
    local fs_type="$8"
    local recovery_script="$9"

    mkdir -p "$DDRESCUE_SESSION_DIR"

    local timestamp=$(date +%Y%m%d_%H%M%S)
    local session_file="$DDRESCUE_SESSION_DIR/session_${timestamp}"

    # Get device info for identification
    local source_model=$(lsblk -no MODEL "$source_dev" 2>/dev/null | head -1 | sed 's/ *$//')
    local source_serial=$(udevadm info --query=property --name="$source_dev" 2>/dev/null | grep "ID_SERIAL_SHORT=" | cut -d= -f2)
    local source_size=$(lsblk -no SIZE "$source_dev" 2>/dev/null | head -1)

    local dest_model=$(lsblk -no MODEL "$dest_dev" 2>/dev/null | head -1 | sed 's/ *$//')
    local dest_serial=$(udevadm info --query=property --name="$dest_dev" 2>/dev/null | grep "ID_SERIAL_SHORT=" | cut -d= -f2)
    local dest_size=$(lsblk -no SIZE "$dest_dev" 2>/dev/null | head -1)

    cat > "$session_file" << EOF
# DDRescue Recovery Session
# Created: $(date)
TICKET_NUMBER="$ticket"
CUSTOMER_NAME="$customer"
SOURCE_DEVICE="$source_dev"
SOURCE_MODEL="$source_model"
SOURCE_SERIAL="$source_serial"
SOURCE_SIZE="$source_size"
DEST_DEVICE="$dest_dev"
DEST_MODEL="$dest_model"
DEST_SERIAL="$dest_serial"
DEST_SIZE="$dest_size"
LOG_FILE="$log_file"
JOB_DIR="$job_dir"
RECOVERY_MODE="$mode"
FS_TYPE="$fs_type"
RECOVERY_SCRIPT="$recovery_script"
TIMESTAMP="$(date)"
EOF

    chmod 600 "$session_file"
    echo "$session_file"
}

# List recent ddrescue sessions
list_ddrescue_sessions() {
    echo -e "${YELLOW}Recent DDRescue Sessions:${NC}"
    echo "=========================="

    if [ ! -d "$DDRESCUE_SESSION_DIR" ]; then
        echo "No sessions found."
        return 1
    fi

    local count=0
    for session_file in $(ls -t "$DDRESCUE_SESSION_DIR"/session_* 2>/dev/null | head -10); do
        count=$((count + 1))

        # Source the session file
        source "$session_file" 2>/dev/null

        echo ""
        # Show mode indicator
        local mode_label=""
        case "$RECOVERY_MODE" in
            full-clone) mode_label="${YELLOW}[FULL CLONE]${NC}" ;;
            bootable-first) mode_label="${CYAN}[TARGETED/BOOT-FIRST]${NC}" ;;
            data-first|*) mode_label="${CYAN}[TARGETED]${NC}" ;;
        esac
        echo -e "${GREEN}[$count]${NC} $TICKET_NUMBER - $CUSTOMER_NAME $mode_label"
        echo "    Source: $SOURCE_DEVICE ($SOURCE_MODEL, $SOURCE_SIZE)"
        echo "    Dest:   $DEST_DEVICE ($DEST_MODEL, $DEST_SIZE)"
        if [ -n "$FS_TYPE" ] && [ "$RECOVERY_MODE" != "full-clone" ]; then
            echo "    FS:     $FS_TYPE"
        fi
        echo "    Log:    $LOG_FILE"
        echo "    Date:   $TIMESTAMP"

        # Check if job directory exists and has state
        if [ -d "$JOB_DIR" ] && [ -f "$JOB_DIR/recovery_state.json" ]; then
            # Try to get progress from state file
            local stage=$(grep -o '"stage":[^,}]*' "$JOB_DIR/recovery_state.json" 2>/dev/null | cut -d: -f2)
            local final_pct=$(grep -o '"final_recovery_pct":[^,}]*' "$JOB_DIR/recovery_state.json" 2>/dev/null | cut -d: -f2)

            if [ -n "$final_pct" ]; then
                echo -e "    ${GREEN}Progress: ${final_pct}% recovered${NC}"
            elif [ -n "$stage" ]; then
                echo -e "    ${CYAN}Stage: $stage${NC}"
            fi
        fi

        # Check if log file exists and get stats
        if [ -f "$LOG_FILE" ]; then
            local rescued=$(grep -E "^rescued:" "$LOG_FILE" 2>/dev/null | tail -1 | awk '{print $2}')
            if [ -n "$rescued" ]; then
                echo -e "    ${CYAN}Rescued: $rescued${NC}"
            fi
        else
            echo -e "    ${YELLOW}Log file not found${NC}"
        fi
    done

    if [ $count -eq 0 ]; then
        echo "No sessions found."
        return 1
    fi

    return 0
}

# Helper: run targeted recovery Python script with current session vars
_resume_targeted_recovery() {
    local BOOTABLE_FLAG=""
    if [ "$RECOVERY_MODE" = "bootable-first" ]; then
        BOOTABLE_FLAG="--bootable-first"
    fi

    local script_to_use="$RECOVERY_SCRIPT"
    if [ -z "$script_to_use" ] || [ ! -f "$script_to_use" ]; then
        echo -e "${YELLOW}No recovery script saved in session - defaulting to NTFS${NC}"
        script_to_use="$SCRIPT_DIR/scripts/iterative-targeted-recovery.py"
    fi

    echo "Running: $script_to_use"
    python3 "$script_to_use" \
        $BOOTABLE_FLAG \
        "$SOURCE_DEVICE" \
        "$DEST_DEVICE" \
        "$LOG_FILE" \
        "$JOB_DIR"

    return $?
}

# Resume a ddrescue session
# Find the current /dev path for a drive by its saved serial, preserving any
# partition suffix (e.g. sdd3 -> sdc3, nvme0n1p4 -> nvme1n1p4). Echoes the new
# path if a connected drive matches the serial, nothing otherwise. Used on resume
# when a drive re-enumerated to a different device node.
_relocate_by_serial() {
    local saved_serial="$1" original_path="$2"
    [ -z "$saved_serial" ] && return 1
    case "$original_path" in /dev/*) : ;; *) return 1 ;; esac
    local name part sep
    name=$(basename "$original_path")
    if [[ "$name" =~ ^(nvme[0-9]+n[0-9]+|mmcblk[0-9]+)p([0-9]+)$ ]]; then
        sep="p"; part="${BASH_REMATCH[2]}"
    elif [[ "$name" =~ ^sd[a-z]+([0-9]+)$ ]]; then
        sep=""; part="${BASH_REMATCH[1]}"
    else
        sep=""; part=""
    fi
    local d sn
    while read -r d; do
        [ -z "$d" ] && continue
        sn=$(udevadm info --query=property --name="/dev/$d" 2>/dev/null | sed -n 's/^ID_SERIAL_SHORT=//p' | head -1)
        [ -z "$sn" ] && sn=$(udevadm info --query=property --name="/dev/$d" 2>/dev/null | sed -n 's/^ID_SERIAL=//p' | head -1)
        if [ "$sn" = "$saved_serial" ]; then
            if [ -n "$part" ]; then echo "/dev/${d}${sep}${part}"; else echo "/dev/$d"; fi
            return 0
        fi
    done < <(lsblk -dn -o NAME 2>/dev/null | grep -E '^(sd[a-z]+|nvme[0-9]+n[0-9]+|mmcblk[0-9]+)$')
    return 1
}

resume_ddrescue_session() {
    if ! list_ddrescue_sessions; then
        return 1
    fi

    echo ""
    read -p "Select session to resume [1-10]: " selection

    local session_files=($(ls -t "$DDRESCUE_SESSION_DIR"/session_* 2>/dev/null | head -10))

    if [[ "$selection" =~ ^[0-9]+$ ]] && [ "$selection" -ge 1 ] && [ "$selection" -le ${#session_files[@]} ]; then
        local selected_file="${session_files[$((selection-1))]}"
        source "$selected_file"

        echo ""
        echo -e "${CYAN}Resuming session: $TICKET_NUMBER - $CUSTOMER_NAME${NC}"
        echo ""

        # Verify devices still exist; if a drive re-enumerated to a new node,
        # relocate it by its saved serial (same as the engine does on resume).
        local _state_json="${JOB_DIR:-$(dirname "$LOG_FILE")}/recovery_state.json"

        if [ ! -b "$SOURCE_DEVICE" ]; then
            local _src_serial=$(python3 -c "import json,sys; print((json.load(open(sys.argv[1])).get('source_identity') or {}).get('serial') or '')" "$_state_json" 2>/dev/null)
            local _src_new=$(_relocate_by_serial "$_src_serial" "$SOURCE_DEVICE")
            if [ -n "$_src_new" ] && [ -b "$_src_new" ]; then
                echo -e "${GREEN}Source drive moved: $SOURCE_DEVICE -> $_src_new (verified by serial)${NC}"
                SOURCE_DEVICE="$_src_new"
            else
                echo -e "${RED}Error: Source device $SOURCE_DEVICE not found${NC}"
                echo "The drive may have been disconnected or assigned a different device name."
                echo ""
                echo "Available drives:"
                lsblk -d -o NAME,SIZE,MODEL,SERIAL | grep -E "^(sd|nvme)"
                echo ""
                read -p "Enter new source device (or press Enter to cancel): " NEW_SOURCE
                if [ -z "$NEW_SOURCE" ]; then
                    return 1
                fi
                SOURCE_DEVICE="$NEW_SOURCE"
            fi
        fi

        # Relocate a moved block-device destination by serial too (image-file
        # destinations keep their path and are validated by the check below).
        if [[ "$DEST_DEVICE" == /dev/* ]] && [ ! -b "$DEST_DEVICE" ]; then
            local _dst_serial=$(python3 -c "import json,sys; print((json.load(open(sys.argv[1])).get('dest_identity') or {}).get('serial') or '')" "$_state_json" 2>/dev/null)
            local _dst_new=$(_relocate_by_serial "$_dst_serial" "$DEST_DEVICE")
            if [ -n "$_dst_new" ] && [ -b "$_dst_new" ]; then
                echo -e "${GREEN}Destination drive moved: $DEST_DEVICE -> $_dst_new (verified by serial)${NC}"
                DEST_DEVICE="$_dst_new"
            fi
        fi

        if [ ! -b "$DEST_DEVICE" ] && [ ! -f "$DEST_DEVICE" ] && [ ! -d "$(dirname "$DEST_DEVICE")" ]; then
            echo -e "${RED}Error: Destination device $DEST_DEVICE not found${NC}"
            echo ""
            read -p "Enter new destination device (or press Enter to cancel): " NEW_DEST
            if [ -z "$NEW_DEST" ]; then
                return 1
            fi
            DEST_DEVICE="$NEW_DEST"
        fi

        echo ""
        echo "Source: $SOURCE_DEVICE"
        echo "Dest:   $DEST_DEVICE"
        echo "Log:    $LOG_FILE"
        if [ -n "$FS_TYPE" ]; then
            echo "FS:     $FS_TYPE"
        fi
        echo "Mode:   $RECOVERY_MODE"

        local exit_code=0

        # Check recovery progress
        local recovery_pct=""
        local bad_sectors=""
        if [ -f "$JOB_DIR/recovery_state.json" ]; then
            recovery_pct=$(grep -o '"final_recovery_pct":[^,}]*' "$JOB_DIR/recovery_state.json" 2>/dev/null | cut -d: -f2 | tr -d ' ')
            bad_sectors=$(grep -o '"bad_sectors":[^,}]*' "$JOB_DIR/recovery_state.json" 2>/dev/null | cut -d: -f2 | tr -d ' ')
        fi

        # For targeted recoveries with progress, show resume menu
        if [ "$RECOVERY_MODE" != "full-clone" ] && [ -n "$recovery_pct" ]; then
            echo ""
            echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
            echo -e "  Recovery Progress: ${GREEN}${recovery_pct}%${NC}"
            [ -n "$bad_sectors" ] && [ "$bad_sectors" != "0" ] && \
                echo -e "  Bad Sectors: ${YELLOW}${bad_sectors} bytes${NC}"
            echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
            echo ""
            echo "  1. Continue targeted recovery (resume wizard)"
            echo "  2. Clone remaining areas (free space, other partitions, everything else)"
            echo "     Useful for: deleted file recovery, other partitions, or just getting everything"
            echo "  3. Aggressive retry on bad sectors only"
            echo "  4. Generate recovery visualization"
            echo "  0. Back to main menu"
            echo ""

            read -p "Select option [0-4]: " resume_choice

            case "$resume_choice" in
                1)
                    echo -e "\n${GREEN}Resuming targeted recovery...${NC}"
                    _resume_targeted_recovery
                    exit_code=$?
                    ;;
                2)
                    echo ""
                    offer_remaining_clone "$SOURCE_DEVICE" "$DEST_DEVICE" "$LOG_FILE"
                    exit_code=$?
                    ;;
                3)
                    echo -e "\n${GREEN}Running aggressive retry on bad sectors...${NC}"
                    if [ -f "$LOG_FILE" ]; then
                        echo "Command: ddrescue -d -f -A -r3 -M $SOURCE_DEVICE $DEST_DEVICE $LOG_FILE"
                        echo ""
                        ddrescue -d -f -A -r3 -M "$SOURCE_DEVICE" "$DEST_DEVICE" "$LOG_FILE"
                        exit_code=$?
                    else
                        echo -e "${RED}Log file not found: $LOG_FILE${NC}"
                        exit_code=1
                    fi
                    ;;
                4)
                    offer_visualization "$JOB_DIR" "$LOG_FILE"
                    return 0
                    ;;
                0|"")
                    return 0
                    ;;
                *)
                    echo -e "${RED}Invalid selection${NC}"
                    return 1
                    ;;
            esac
        else
            # No progress info or full clone — resume directly
            if [ "$RECOVERY_MODE" = "full-clone" ]; then
                echo -e "\n${GREEN}Resuming full drive clone...${NC}"
                echo "Command: ddrescue -d -f $SOURCE_DEVICE $DEST_DEVICE $LOG_FILE"
                echo ""
                ddrescue -d -f "$SOURCE_DEVICE" "$DEST_DEVICE" "$LOG_FILE"
                exit_code=$?
            else
                _resume_targeted_recovery
                exit_code=$?
            fi

            # Offer to clone remaining areas after wizard completes
            offer_remaining_clone "$SOURCE_DEVICE" "$DEST_DEVICE" "$LOG_FILE"
        fi

        # Offer to generate visualization
        offer_visualization "$JOB_DIR" "$LOG_FILE"

        return $exit_code
    else
        echo -e "${RED}Invalid selection${NC}"
        return 1
    fi
}

function list_rules() {
    echo "=== DDRescue Udev Rules ==="
    echo
    
    local found=0
    for rule in $UDEV_RULES_DIR/99-ddrescue-*.rules; do
        if [ -f "$rule" ]; then
            found=1
            basename "$rule"
            echo "Content:"
            grep -E "KERNEL|#" "$rule" | sed 's/^/  /'
            
            # Check for potential duplicates (same device)
            local model=$(grep -o 'ATTRS{model}=="[^"]*"' "$rule" | head -1)
            local serial=$(grep -o 'ATTRS{serial}=="[^"]*"' "$rule" | head -1)
            
            if [ -n "$model" ] || [ -n "$serial" ]; then
                local dup_count=0
                for other_rule in $UDEV_RULES_DIR/99-ddrescue-*.rules; do
                    if [ "$other_rule" != "$rule" ] && [ -f "$other_rule" ]; then
                        if [ -n "$serial" ] && grep -q "$serial" "$other_rule"; then
                            ((dup_count++))
                        elif [ -n "$model" ] && grep -q "$model" "$other_rule"; then
                            ((dup_count++))
                        fi
                    fi
                done
                
                if [ $dup_count -gt 0 ]; then
                    echo "  WARNING: $dup_count other rule(s) match the same device!"
                fi
            fi
            echo
        fi
    done
    
    if [ $found -eq 0 ]; then
        echo "No ddrescue rules found in $UDEV_RULES_DIR"
    fi
}

function remove_rule() {
    local job_name="$1"
    local rule_file="$UDEV_RULES_DIR/99-ddrescue-${job_name}.rules"
    
    if [ -f "$rule_file" ]; then
        echo -e "${YELLOW}Found rule: $rule_file${NC}"
        echo "Content:"
        cat "$rule_file"
        echo
        
        # Extract device info for safety confirmation
        local model=$(grep -o 'ATTRS{model}=="[^"]*"' "$rule_file" | cut -d'"' -f2 | head -1)
        local serial=$(grep -o 'ATTRS{serial}=="[^"]*"' "$rule_file" | cut -d'"' -f2 | head -1)
        
        echo -e "${YELLOW}WARNING: This will remove the auto-run rule for:${NC}"
        [ -n "$model" ] && echo "  Model: $model"
        [ -n "$serial" ] && echo "  Serial: $serial"
        echo "  Config: $SCRIPT_DIR/ddrescue-${job_name}.conf"
        echo "  Script: $SCRIPT_DIR/ddrescue-${job_name}.sh"
        echo
        read -p "Are you sure you want to remove this rule? [y/N]: " confirm
        if [[ "$confirm" =~ ^[Yy]$ ]]; then
            if sudo rm "$rule_file"; then
                echo "Rule removed"
                
                # Also clean up associated files
                read -p "Also remove associated config and script files? [y/N]: " cleanup
                if [[ "$cleanup" =~ ^[Yy]$ ]]; then
                    cd "$SCRIPT_DIR"
                    rm -f "ddrescue-${job_name}.conf"
                    rm -f "ddrescue-${job_name}.sh"
                    echo "Associated files removed"
                fi
                return 0
            else
                echo "Failed to remove rule"
                return 1
            fi
        fi
    else
        echo "Rule not found: $rule_file"
        return 1
    fi
}

function remove_all_rules() {
    echo "This will remove ALL ddrescue auto-run rules"
    list_rules
    echo
    read -p "Are you sure you want to remove all rules? [y/N]: " confirm
    if [[ "$confirm" =~ ^[Yy]$ ]]; then
        sudo rm -f $UDEV_RULES_DIR/99-ddrescue-*.rules
        echo "All ddrescue rules removed"
        
        read -p "Also remove all associated configs and scripts? [y/N]: " cleanup
        if [[ "$cleanup" =~ ^[Yy]$ ]]; then
            cd "$SCRIPT_DIR"
            rm -f ddrescue-*.conf
            rm -f ddrescue-*.sh
            echo "All associated files removed"
        fi
        
        # Check if sudoers file exists
        if [ -f "$SUDOERS_FILE" ]; then
            echo
            echo "Passwordless sudo is still configured for ddrescue."
            read -p "Remove passwordless sudo configuration? [y/N]: " remove_sudo
            if [[ "$remove_sudo" =~ ^[Yy]$ ]]; then
                sudo rm -f "$SUDOERS_FILE"
                echo "Passwordless sudo configuration removed"
            fi
        fi
    fi
}

function reload_rules() {
    echo "Reloading udev rules..."
    sudo udevadm control --reload-rules
    sudo udevadm trigger
    echo "Udev rules reloaded"
}

function check_sudo_status() {
    if [ -f "$SUDOERS_FILE" ]; then
        echo "Passwordless sudo is configured for ddrescue:"
        sudo grep -v "^#" "$SUDOERS_FILE" | grep -v "^$"
        return 0
    else
        echo "No passwordless sudo configuration found for ddrescue"
        return 1
    fi
}

function monitor_ddrescue() {
    echo "=== DDRescue Output Monitor ==="
    echo
    
    # Find most recent ddrescue-auto.log file
    local latest_log=""
    local latest_time=0
    
    # Search for log files in common locations
    for conf in "$SCRIPT_DIR"/ddrescue-*.conf; do
        if [ -f "$conf" ]; then
            # Extract log directory from config
            local log_dir=$(grep "^LOG_DIR=" "$conf" 2>/dev/null | cut -d'"' -f2)
            if [ -n "$log_dir" ] && [ -f "$log_dir/ddrescue-auto.log" ]; then
                local mod_time=$(stat -c %Y "$log_dir/ddrescue-auto.log" 2>/dev/null || stat -f %m "$log_dir/ddrescue-auto.log" 2>/dev/null)
                if [ -n "$mod_time" ] && [ "$mod_time" -gt "$latest_time" ]; then
                    latest_time=$mod_time
                    latest_log="$log_dir/ddrescue-auto.log"
                fi
            fi
        fi
    done
    
    if [ -n "$latest_log" ]; then
        echo "Found most recent log: $latest_log"
        echo "Last modified: $(date -r "$latest_log" 2>/dev/null || ls -la "$latest_log" | awk '{print $6, $7, $8}')"
        echo
        echo "Press Ctrl+C to stop monitoring"
        echo "=========================================="
        tail -f "$latest_log"
    else
        echo "No ddrescue-auto.log files found."
        echo
        echo "Logs are created when ddrescue runs automatically or when you test manually."
        echo "Check your configuration files for log locations."
    fi
}

#######################################
# DDRescue Targeted Recovery
#######################################
function ddrescue_targeted_recovery() {
    echo "=== DDRescue Targeted Recovery ==="
    echo ""
    echo -e "${CYAN}This tool recovers data from failing drives using an intelligent workflow:${NC}"
    echo "  1. Boot sector → NTFS parameters"
    echo "  2. MFT header  → File allocation map"
    echo "  3. \$Bitmap     → Which clusters have data"
    echo "  4. Data only   → Skip empty space (faster!)"
    echo ""

    # Check for root
    if [ "$EUID" -ne 0 ]; then
        echo -e "${RED}Error: This tool requires root privileges${NC}"
        echo "Please restart with: sudo $SCRIPT_DIR/recovery-manager.sh"
        return 1
    fi

    # Collect job info
    if ! prompt_job_info ""; then
        return 1
    fi

    echo ""

    # Source drive selection
    if [ "$PARTITION_ANALYZER_AVAILABLE" = true ]; then
        echo -e "${GREEN}Select Source Drive (Failing Drive)${NC}"
        echo "======================================"
        echo ""
        show_source_menu
        echo ""
        read -p "Select source [1-$PARTITION_MENU_COUNT]: " SOURCE_CHOICE

        # Read the displayed partitions
        local temp_file="/tmp/rsync_displayed_partitions_$$"
        if [ -f "$temp_file" ]; then
            mapfile -t DISPLAYED_PARTITIONS < "$temp_file"
            rm -f "$temp_file"
        fi

        if [ "$SOURCE_CHOICE" = "$PARTITION_MENU_COUNT" ]; then
            # Manual entry
            read -p "Enter source device (e.g., /dev/sdc): " SOURCE_DEVICE
        elif [[ "$SOURCE_CHOICE" =~ ^[0-9]+$ ]] && [ "$SOURCE_CHOICE" -ge 1 ] && [ "$SOURCE_CHOICE" -lt "$PARTITION_MENU_COUNT" ]; then
            local selected="${DISPLAYED_PARTITIONS[$((SOURCE_CHOICE-1))]}"
            SOURCE_PARTITION=$(echo "$selected" | cut -d'|' -f1)
            # Targeted recovery operates on the partition itself (cluster bitmap is
            # partition-scoped). Keep SOURCE_DEVICE = SOURCE_PARTITION here.
            SOURCE_DEVICE="$SOURCE_PARTITION"
            # Derive the parent whole-disk path for disk-level operations like SMART.
            parent=$(lsblk -no PKNAME "$SOURCE_PARTITION" 2>/dev/null | head -1 | tr -d ' ')
            if [ -n "$parent" ]; then
                SOURCE_DISK="/dev/$parent"
            else
                SOURCE_DISK="$SOURCE_PARTITION"
            fi
        else
            echo -e "${RED}Invalid selection${NC}"
            return 1
        fi
    else
        # Fallback: basic device listing
        echo -e "${GREEN}Available Drives:${NC}"
        lsblk -d -o NAME,SIZE,MODEL,TRAN,STATE 2>/dev/null | head -20
        echo ""
        read -p "Enter source device (e.g., /dev/sdc): " SOURCE_DEVICE
    fi

    if [ ! -b "$SOURCE_DEVICE" ]; then
        echo -e "${RED}Error: $SOURCE_DEVICE is not a valid block device${NC}"
        return 1
    fi

    echo ""
    echo -e "${GREEN}Source selected: $SOURCE_DEVICE${NC}"

    # Get source size for destination comparison
    SOURCE_SIZE=$(blockdev --getsize64 "$SOURCE_DEVICE" 2>/dev/null)
    SOURCE_SIZE_GB=$((SOURCE_SIZE / 1024 / 1024 / 1024))
    echo "  Size: ${SOURCE_SIZE_GB} GB"

    # Detect filesystem type (gentle - reads from kernel cache)
    echo ""
    echo -e "${CYAN}Detecting filesystem type...${NC}"

    # Try to detect filesystem on the first partition or the device itself
    DETECTED_FS=""
    FS_PARTITION=""

    # Probe the user-selected partition first (if available), then fall back to
    # the conventional first-partition / whole-device locations.
    fs_candidates=()
    if [ -n "$SOURCE_PARTITION" ] && [ -b "$SOURCE_PARTITION" ] && \
       [ "$SOURCE_PARTITION" != "$SOURCE_DEVICE" ]; then
        fs_candidates+=("$SOURCE_PARTITION")
    fi
    fs_candidates+=("${SOURCE_DEVICE}1" "${SOURCE_DEVICE}p1" "$SOURCE_DEVICE")

    for part in "${fs_candidates[@]}"; do
        if [ -b "$part" ]; then
            fs_type=$(lsblk -no FSTYPE "$part" 2>/dev/null | head -1 | tr -d ' ')
            if [ -n "$fs_type" ]; then
                DETECTED_FS="$fs_type"
                FS_PARTITION="$part"
                break
            fi
        fi
    done

    # Also try blkid as fallback (still gentle - uses cache)
    if [ -z "$DETECTED_FS" ]; then
        for part in "${fs_candidates[@]}"; do
            if [ -b "$part" ]; then
                fs_type=$(blkid -s TYPE -o value "$part" 2>/dev/null | head -1)
                if [ -n "$fs_type" ]; then
                    DETECTED_FS="$fs_type"
                    FS_PARTITION="$part"
                    break
                fi
            fi
        done
    fi

    # Normalize filesystem names
    case "$DETECTED_FS" in
        ntfs|NTFS)
            DETECTED_FS="ntfs"
            FS_DISPLAY="NTFS"
            ;;
        hfsplus|hfs+|HFS+)
            DETECTED_FS="hfsplus"
            FS_DISPLAY="HFS+ (macOS)"
            ;;
        hfs|HFS)
            DETECTED_FS="hfs"
            FS_DISPLAY="HFS (Classic Mac)"
            ;;
        apfs|APFS)
            DETECTED_FS="apfs"
            FS_DISPLAY="APFS (macOS)"
            ;;
        ext4|ext3|ext2)
            DETECTED_FS="$DETECTED_FS"
            FS_DISPLAY="$DETECTED_FS (Linux)"
            ;;
        *)
            FS_DISPLAY="$DETECTED_FS"
            ;;
    esac

    # Present filesystem detection to user
    if [ -n "$DETECTED_FS" ]; then
        echo -e "  Detected: ${GREEN}$FS_DISPLAY${NC} on $FS_PARTITION"
    else
        echo -e "  ${YELLOW}Could not detect filesystem (drive may be damaged or unformatted)${NC}"
    fi

    # Let user confirm or override
    echo ""
    echo "Select filesystem for recovery:"
    echo "  1. NTFS (Windows)"
    echo "  2. HFS+ (macOS)"
    if [ "$DETECTED_FS" = "ntfs" ]; then
        echo -e "  ${GREEN}→ Detected: NTFS - press Enter to use${NC}"
        FS_DEFAULT=1
    elif [ "$DETECTED_FS" = "hfsplus" ] || [ "$DETECTED_FS" = "hfs" ]; then
        echo -e "  ${GREEN}→ Detected: HFS+ - press Enter to use${NC}"
        FS_DEFAULT=2
    else
        echo -e "  ${YELLOW}→ No detection - please select manually${NC}"
        FS_DEFAULT=""
    fi

    read -p "Filesystem [1-2, default=$FS_DEFAULT]: " FS_CHOICE
    FS_CHOICE=${FS_CHOICE:-$FS_DEFAULT}

    case "$FS_CHOICE" in
        1)
            RECOVERY_SCRIPT="$SCRIPT_DIR/scripts/iterative-targeted-recovery.py"
            FS_NAME="NTFS"
            ;;
        2)
            RECOVERY_SCRIPT="$SCRIPT_DIR/scripts/iterative-targeted-recovery-hfs.py"
            FS_NAME="HFS+"
            ;;
        *)
            echo -e "${RED}Invalid selection - must choose filesystem${NC}"
            return 1
            ;;
    esac

    # Verify script exists
    if [ ! -f "$RECOVERY_SCRIPT" ]; then
        echo -e "${RED}Error: Recovery script not found: $RECOVERY_SCRIPT${NC}"
        return 1
    fi

    echo -e "Using: ${GREEN}$FS_NAME${NC} recovery script"

    # Show SMART info for source (if smartctl available)
    if command -v smartctl &> /dev/null; then
        echo ""
        echo -e "${YELLOW}SMART Health Check:${NC}"
        smart_target="${SOURCE_DISK:-$SOURCE_DEVICE}"
        smartctl -H "$smart_target" 2>/dev/null | grep -E "(PASSED|FAILED|result)" || echo "  Could not read SMART data"
        # Show critical attributes
        smartctl -A "$smart_target" 2>/dev/null | grep -E "(Reallocated|Pending|Uncorrectable)" | head -5
    fi

    echo ""

    # Destination drive selection (ddrescue needs whole drive >= source size)
    echo ""
    show_ddrescue_destinations "$SOURCE_DEVICE" "$SOURCE_SIZE"

    if ! select_ddrescue_destination "$SOURCE_SIZE"; then
        return 1
    fi
    DEST_DEVICE="$SELECTED_DEST_DEVICE"

    # Image files may not exist yet — accept a path whose parent directory exists
    if [ ! -b "$DEST_DEVICE" ] && [ ! -f "$DEST_DEVICE" ] && [ ! -d "$(dirname "$DEST_DEVICE")" ]; then
        echo -e "${RED}Error: $DEST_DEVICE is not a valid device or file path${NC}"
        return 1
    fi

    # Prevent same source and dest
    if [ "$SOURCE_DEVICE" = "$DEST_DEVICE" ]; then
        echo -e "${RED}Error: Source and destination cannot be the same!${NC}"
        return 1
    fi

    # Get destination size for display (handle block device vs image file)
    local DEST_SIZE=0
    local DEST_SIZE_GB=0
    local DEST_IS_FILE=false
    if [ -b "$DEST_DEVICE" ]; then
        DEST_SIZE=$(blockdev --getsize64 "$DEST_DEVICE" 2>/dev/null)
        DEST_SIZE_GB=$((DEST_SIZE / 1024 / 1024 / 1024))
        echo ""
        echo -e "${GREEN}Destination selected: $DEST_DEVICE (${DEST_SIZE_GB} GB block device)${NC}"
    else
        DEST_IS_FILE=true
        local dest_dir
        dest_dir=$(dirname "$DEST_DEVICE")
        local avail_bytes
        avail_bytes=$(df -B1 --output=avail "$dest_dir" 2>/dev/null | tail -1 | tr -d ' ')
        local avail_gb=$(( ${avail_bytes:-0} / 1024 / 1024 / 1024 ))
        echo ""
        echo -e "${GREEN}Destination selected: $DEST_DEVICE (image file, ${avail_gb} GB free on ${dest_dir})${NC}"
    fi

    # Set up paths - keep everything in job directory
    JOB_DIR="$SCRIPT_DIR/tickets/${JOB_NAME}_recovery"
    mkdir -p "$JOB_DIR"

    # If a previous run left recovery_state.json, offer to clear it before the Python
    # script sees it as a "resume" and enforces the old drive identities / partition_offset.
    if [ -f "$JOB_DIR/recovery_state.json" ]; then
        echo -e "${YELLOW}⚠️  Job directory already has state from a previous run:${NC}"
        echo "     $JOB_DIR/recovery_state.json"
        echo ""
        echo "  1. Start FRESH (clear state — correct choice when source/dest changed)"
        echo "  2. Resume previous run (keep state — correct choice when retrying the same job)"
        echo ""
        read -p "Select [1-2, default=1]: " STALE_CHOICE
        STALE_CHOICE=${STALE_CHOICE:-1}
        if [ "$STALE_CHOICE" = "1" ]; then
            rm -f "$JOB_DIR/recovery_state.json"
            echo -e "${GREEN}State cleared — starting fresh.${NC}"
        else
            echo -e "${CYAN}Keeping existing state — resuming previous run.${NC}"
        fi
        echo ""
    fi

    LOG_FILE="$JOB_DIR/ddrescue.log"

    # Create Desktop symlink for easy monitoring
    local DESKTOP_LINK="$(get_real_home)/Desktop/${JOB_NAME}_DDRescue.log"
    ln -sf "$LOG_FILE" "$DESKTOP_LINK" 2>/dev/null

    echo ""
    echo -e "${CYAN}╔════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}║  Recovery Summary                                          ║${NC}"
    echo -e "${CYAN}╚════════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo "  Ticket:      $TICKET_NUMBER"
    echo "  Customer:    $CUSTOMER_NAME"
    echo "  Source:      $SOURCE_DEVICE"
    echo "  Destination: $DEST_DEVICE"
    echo "  Job folder:  $JOB_DIR"
    echo "  Log file:    $LOG_FILE"
    echo "  Desktop link: $DESKTOP_LINK"
    echo ""

    # Recovery mode selection
    echo -e "${GREEN}Recovery Mode:${NC}"
    echo "  1. Data-first (recover files, then make bootable) [RECOMMENDED]"
    echo "  2. Bootable-first (GPT/EFI first, then data)"
    echo ""
    read -p "Select mode [1-2, default=1]: " MODE_CHOICE
    MODE_CHOICE=${MODE_CHOICE:-1}

    local BOOTABLE_FLAG=""
    if [ "$MODE_CHOICE" = "2" ]; then
        BOOTABLE_FLAG="--bootable-first"
        echo "Mode: Bootable-first"
    else
        echo "Mode: Data-first"
    fi

    echo ""
    echo -e "${YELLOW}WARNING: This will write to $DEST_DEVICE${NC}"
    echo -e "${YELLOW}All existing data on destination will be OVERWRITTEN${NC}"
    echo ""

    # Check destination for existing data
    local dest_has_data=false
    local dest_is_mounted=false
    local dest_partitions=""

    if [ "$DEST_IS_FILE" = true ]; then
        # Image-file destination: check for an existing file at the target path.
        if [ -e "$DEST_DEVICE" ]; then
            local existing_size
            existing_size=$(stat -c %s "$DEST_DEVICE" 2>/dev/null || echo 0)
            local existing_human
            existing_human=$(numfmt --to=iec --suffix=B "$existing_size" 2>/dev/null || echo "${existing_size} bytes")
            echo -e "${YELLOW}Image file already exists at $DEST_DEVICE (${existing_human}).${NC}"
            echo -e "${YELLOW}ddrescue will treat this as a resumable target if the map file is present.${NC}"
            dest_has_data=true
        fi
    else
        # Block-device destination: check for partitions, partition tables, mounts.
        dest_partitions=$(lsblk -no NAME,SIZE,FSTYPE,LABEL,MOUNTPOINT "$DEST_DEVICE" 2>/dev/null | tail -n +2)

        if [ -n "$dest_partitions" ]; then
            echo -e "${RED}╔════════════════════════════════════════════════════════════╗${NC}"
            echo -e "${RED}║  DESTINATION DRIVE IS NOT EMPTY!                           ║${NC}"
            echo -e "${RED}╚════════════════════════════════════════════════════════════╝${NC}"
            echo ""
            echo "Existing partitions on $DEST_DEVICE:"
            echo "$dest_partitions" | while read -r line; do
                echo "  $line"
            done
            echo ""
            dest_has_data=true

            # Check if any partitions are mounted
            if echo "$dest_partitions" | grep -qE "/media|/mnt|/home"; then
                dest_is_mounted=true
                echo -e "${RED}⚠️  WARNING: One or more partitions are MOUNTED!${NC}"
                echo ""
            fi

            # Check for recognizable labels
            if echo "$dest_partitions" | grep -qiE "backup|data|documents|photos|recovery"; then
                echo -e "${RED}⚠️  WARNING: Drive appears to contain user data!${NC}"
                echo ""
            fi
        fi

        # Check if destination has a partition table at all
        local dest_pt=$(blkid -p -o value -s PTTYPE "$DEST_DEVICE" 2>/dev/null)
        if [ -n "$dest_pt" ]; then
            echo "Partition table: $dest_pt"
            dest_has_data=true
        fi
    fi

    # Final confirmation - more serious if data detected
    if [ "$dest_is_mounted" = true ]; then
        echo -e "${RED}Cannot write to mounted drive. Please unmount first:${NC}"
        echo "  sudo umount ${DEST_DEVICE}*"
        return 1
    elif [ "$dest_has_data" = true ]; then
        echo -e "${RED}Type 'YES' (uppercase) to confirm destruction of this data:${NC}"
        read -p "> " CONFIRM
        if [ "$CONFIRM" != "YES" ]; then
            echo "Cancelled."
            return 0
        fi
    else
        echo -e "${GREEN}Destination appears empty or unformatted.${NC}"
        read -p "Start recovery? [y/N]: " CONFIRM
        if [[ ! "$CONFIRM" =~ ^[Yy]$ ]]; then
            echo "Cancelled."
            return 0
        fi
    fi

    echo ""
    echo -e "${GREEN}Starting targeted recovery...${NC}"
    echo ""

    # Determine mode name for session
    local mode_name="data-first"
    if [ -n "$BOOTABLE_FLAG" ]; then
        mode_name="bootable-first"
    fi

    # Save session for resume capability
    local session_file=$(save_ddrescue_session \
        "$TICKET_NUMBER" \
        "$CUSTOMER_NAME" \
        "$SOURCE_DEVICE" \
        "$DEST_DEVICE" \
        "$LOG_FILE" \
        "$JOB_DIR" \
        "$mode_name" \
        "$FS_NAME" \
        "$RECOVERY_SCRIPT")
    echo "Session saved: $session_file"
    echo ""

    # Launch the appropriate iterative targeted recovery script
    echo "Running: $RECOVERY_SCRIPT"
    python3 "$RECOVERY_SCRIPT" \
        $BOOTABLE_FLAG \
        "$SOURCE_DEVICE" \
        "$DEST_DEVICE" \
        "$LOG_FILE" \
        "$JOB_DIR"

    local exit_code=$?

    echo ""
    if [ $exit_code -eq 0 ]; then
        echo -e "${GREEN}Recovery completed successfully!${NC}"
    else
        echo -e "${RED}Recovery finished with errors (exit code: $exit_code)${NC}"
    fi

    echo ""
    echo "Log file: $LOG_FILE"
    echo "Job data: $JOB_DIR"
    echo "To resume later: Select 'R' from main menu"

    # Offer to clone remaining areas (if targeted recovery didn't already do it)
    offer_remaining_clone "$SOURCE_DEVICE" "$DEST_DEVICE" "$LOG_FILE"

    # Offer to generate visualization
    offer_visualization "$JOB_DIR" "$LOG_FILE"

    return $exit_code
}

#######################################
# DDRescue Full Drive Clone
#######################################
function ddrescue_full_clone() {
    echo "=== DDRescue Full Drive Clone ==="
    echo ""
    echo -e "${CYAN}This clones an entire drive sector-by-sector (including empty space).${NC}"
    echo "Use this when:"
    echo "  • Drive is mostly full (>70% used)"
    echo "  • You need an exact clone including free space"
    echo "  • Filesystem is damaged and allocation bitmap unreadable"
    echo ""

    # Check for root
    if [ "$EUID" -ne 0 ]; then
        echo -e "${RED}Error: This tool requires root privileges${NC}"
        return 1
    fi

    # Collect job info
    if ! prompt_job_info "_"; then
        return 1
    fi

    echo ""

    # Source drive selection (reuse partition analyzer)
    if [ "$PARTITION_ANALYZER_AVAILABLE" = true ]; then
        echo -e "${GREEN}Select Source Drive (Drive to Clone)${NC}"
        echo "======================================"
        echo ""
        show_source_menu
        echo ""
        read -p "Select source [1-$PARTITION_MENU_COUNT]: " SOURCE_CHOICE

        local temp_file="/tmp/rsync_displayed_partitions_$$"
        if [ -f "$temp_file" ]; then
            mapfile -t DISPLAYED_PARTITIONS < "$temp_file"
            rm -f "$temp_file"
        fi

        if [ "$SOURCE_CHOICE" = "$PARTITION_MENU_COUNT" ]; then
            read -p "Enter source device (e.g., /dev/sdc): " SOURCE_DEVICE
        elif [[ "$SOURCE_CHOICE" =~ ^[0-9]+$ ]] && [ "$SOURCE_CHOICE" -ge 1 ] && [ "$SOURCE_CHOICE" -lt "$PARTITION_MENU_COUNT" ]; then
            local selected="${DISPLAYED_PARTITIONS[$((SOURCE_CHOICE-1))]}"
            SOURCE_PARTITION=$(echo "$selected" | cut -d'|' -f1)
            # Resolve whole-disk path (handles SATA/NVMe/MMC/loop uniformly).
            parent=$(lsblk -no PKNAME "$SOURCE_PARTITION" 2>/dev/null | head -1 | tr -d ' ')
            if [ -n "$parent" ]; then
                SOURCE_DEVICE="/dev/$parent"
            else
                SOURCE_DEVICE=$(echo "$SOURCE_PARTITION" | sed -E 's/(p)?[0-9]+$//')
            fi
        else
            echo -e "${RED}Invalid selection${NC}"
            return 1
        fi
    else
        echo -e "${GREEN}Available Drives:${NC}"
        lsblk -d -o NAME,SIZE,MODEL,TRAN,STATE 2>/dev/null | head -20
        echo ""
        read -p "Enter source device (e.g., /dev/sdc): " SOURCE_DEVICE
    fi

    if [ ! -b "$SOURCE_DEVICE" ]; then
        echo -e "${RED}Error: $SOURCE_DEVICE is not a valid block device${NC}"
        return 1
    fi

    echo ""
    echo -e "${GREEN}Source selected: $SOURCE_DEVICE${NC}"

    # Show source size and usage
    local SOURCE_SIZE=$(blockdev --getsize64 "$SOURCE_DEVICE" 2>/dev/null)
    local SOURCE_SIZE_GB=$((SOURCE_SIZE / 1024 / 1024 / 1024))
    echo "  Size: ${SOURCE_SIZE_GB} GB"

    # Show SMART info
    if command -v smartctl &> /dev/null; then
        echo ""
        echo -e "${YELLOW}SMART Health Check:${NC}"
        smartctl -H "$SOURCE_DEVICE" 2>/dev/null | grep -E "(PASSED|FAILED|result)" || echo "  Could not read SMART data"
        smartctl -A "$SOURCE_DEVICE" 2>/dev/null | grep -E "(Reallocated|Pending|Uncorrectable)" | head -5
    fi

    echo ""

    # Destination drive selection (ddrescue needs whole drive >= source size)
    show_ddrescue_destinations "$SOURCE_DEVICE" "$SOURCE_SIZE"

    if ! select_ddrescue_destination "$SOURCE_SIZE"; then
        return 1
    fi
    DEST_DEVICE="$SELECTED_DEST_DEVICE"

    # Prevent same source and dest
    if [ "$SOURCE_DEVICE" = "$DEST_DEVICE" ]; then
        echo -e "${RED}Error: Source and destination cannot be the same!${NC}"
        return 1
    fi

    # Get destination size for display (handle block device vs image file)
    local DEST_SIZE=0
    local DEST_SIZE_GB=0
    local DEST_IS_FILE=false
    if [ -b "$DEST_DEVICE" ]; then
        DEST_SIZE=$(blockdev --getsize64 "$DEST_DEVICE" 2>/dev/null)
        DEST_SIZE_GB=$((DEST_SIZE / 1024 / 1024 / 1024))
        echo ""
        echo -e "${GREEN}Destination selected: $DEST_DEVICE (${DEST_SIZE_GB} GB block device)${NC}"
    else
        DEST_IS_FILE=true
        local dest_dir
        dest_dir=$(dirname "$DEST_DEVICE")
        local avail_bytes
        avail_bytes=$(df -B1 --output=avail "$dest_dir" 2>/dev/null | tail -1 | tr -d ' ')
        local avail_gb=$(( ${avail_bytes:-0} / 1024 / 1024 / 1024 ))
        echo ""
        echo -e "${GREEN}Destination selected: $DEST_DEVICE (image file, ${avail_gb} GB free on ${dest_dir})${NC}"
    fi

    # Set up paths
    JOB_DIR="$SCRIPT_DIR/tickets/${JOB_NAME}_fullclone"
    mkdir -p "$JOB_DIR"
    LOG_FILE="$JOB_DIR/ddrescue.log"

    # Create Desktop symlink
    local DESKTOP_LINK="$(get_real_home)/Desktop/${JOB_NAME}_DDRescue.log"
    ln -sf "$LOG_FILE" "$DESKTOP_LINK" 2>/dev/null

    echo ""
    echo -e "${CYAN}╔════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}║  Full Clone Summary                                        ║${NC}"
    echo -e "${CYAN}╚════════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo "  Ticket:      $TICKET_NUMBER"
    echo "  Customer:    $CUSTOMER_NAME"
    echo "  Source:      $SOURCE_DEVICE (${SOURCE_SIZE_GB} GB)"
    echo "  Destination: $DEST_DEVICE"
    echo "  Job folder:  $JOB_DIR"
    echo "  Log file:    $LOG_FILE"
    echo "  Desktop link: $DESKTOP_LINK"
    echo ""

    # DDRescue options
    echo -e "${GREEN}Recovery Options:${NC}"
    echo "  1. Standard (-d direct, -f force) [RECOMMENDED]"
    echo "  2. Aggressive (-d -r3 -A, retries bad sectors)"
    echo "  3. Custom (enter your own flags)"
    echo ""
    read -p "Select options [1-3, default=1]: " OPT_CHOICE
    OPT_CHOICE=${OPT_CHOICE:-1}

    local DDRESCUE_FLAGS=""
    case "$OPT_CHOICE" in
        1)
            DDRESCUE_FLAGS="-d -f"
            echo "Using: Standard flags (-d -f)"
            ;;
        2)
            DDRESCUE_FLAGS="-d -f -r3 -A"
            echo "Using: Aggressive flags (-d -f -r3 -A)"
            ;;
        3)
            read -p "Enter custom ddrescue flags: " DDRESCUE_FLAGS
            echo "Using: Custom flags ($DDRESCUE_FLAGS)"
            ;;
        *)
            DDRESCUE_FLAGS="-d -f"
            echo "Using: Standard flags (-d -f)"
            ;;
    esac

    echo ""
    echo -e "${YELLOW}WARNING: This will write to $DEST_DEVICE${NC}"
    echo -e "${YELLOW}All existing data on destination will be OVERWRITTEN${NC}"
    echo ""

    # Destination empty check (same as targeted recovery)
    local dest_has_data=false
    local dest_is_mounted=false
    local dest_partitions=""

    if [ -b "$DEST_DEVICE" ]; then
        dest_partitions=$(lsblk -no NAME,SIZE,FSTYPE,LABEL,MOUNTPOINT "$DEST_DEVICE" 2>/dev/null | tail -n +2)

        if [ -n "$dest_partitions" ]; then
            echo -e "${RED}╔════════════════════════════════════════════════════════════╗${NC}"
            echo -e "${RED}║  DESTINATION DRIVE IS NOT EMPTY!                           ║${NC}"
            echo -e "${RED}╚════════════════════════════════════════════════════════════╝${NC}"
            echo ""
            echo "Existing partitions on $DEST_DEVICE:"
            echo "$dest_partitions" | while read -r line; do
                echo "  $line"
            done
            echo ""
            dest_has_data=true

            if echo "$dest_partitions" | grep -qE "/media|/mnt|/home"; then
                dest_is_mounted=true
                echo -e "${RED}⚠️  WARNING: One or more partitions are MOUNTED!${NC}"
                echo ""
            fi

            if echo "$dest_partitions" | grep -qiE "backup|data|documents|photos|recovery"; then
                echo -e "${RED}⚠️  WARNING: Drive appears to contain user data!${NC}"
                echo ""
            fi
        fi

        local dest_pt=$(blkid -p -o value -s PTTYPE "$DEST_DEVICE" 2>/dev/null)
        if [ -n "$dest_pt" ]; then
            echo "Partition table: $dest_pt"
            dest_has_data=true
        fi
    fi

    # Final confirmation
    if [ "$dest_is_mounted" = true ]; then
        echo -e "${RED}Cannot write to mounted drive. Please unmount first:${NC}"
        echo "  sudo umount ${DEST_DEVICE}*"
        return 1
    elif [ "$dest_has_data" = true ]; then
        echo -e "${RED}Type 'YES' (uppercase) to confirm destruction of this data:${NC}"
        read -p "> " CONFIRM
        if [ "$CONFIRM" != "YES" ]; then
            echo "Cancelled."
            return 0
        fi
    else
        echo -e "${GREEN}Destination appears empty or unformatted.${NC}"
        read -p "Start full clone? [y/N]: " CONFIRM
        if [[ ! "$CONFIRM" =~ ^[Yy]$ ]]; then
            echo "Cancelled."
            return 0
        fi
    fi

    echo ""
    echo -e "${GREEN}Starting full drive clone...${NC}"
    echo ""

    # Save session for resume capability
    local session_file=$(save_ddrescue_session \
        "$TICKET_NUMBER" \
        "$CUSTOMER_NAME" \
        "$SOURCE_DEVICE" \
        "$DEST_DEVICE" \
        "$LOG_FILE" \
        "$JOB_DIR" \
        "full-clone" \
        "full" \
        "ddrescue")
    echo "Session saved: $session_file"
    echo ""

    # Build and run the ddrescue command
    local DDRESCUE_CMD="ddrescue $DDRESCUE_FLAGS $SOURCE_DEVICE $DEST_DEVICE $LOG_FILE"
    echo "Command: $DDRESCUE_CMD"
    echo ""

    # Run ddrescue
    eval "$DDRESCUE_CMD"
    local exit_code=$?

    echo ""
    if [ $exit_code -eq 0 ]; then
        echo -e "${GREEN}Clone completed successfully!${NC}"
    else
        echo -e "${RED}Clone finished with errors (exit code: $exit_code)${NC}"
    fi

    # Show final stats from log
    if [ -f "$LOG_FILE" ]; then
        echo ""
        echo "Recovery Statistics:"
        grep -E "^(rescued|errsize|errors|non-tried)" "$LOG_FILE" 2>/dev/null | tail -4
    fi

    echo ""
    echo "Log file: $LOG_FILE"
    echo "Job data: $JOB_DIR"
    echo "To resume later: Select 'R' from main menu"

    # Offer visualization
    offer_visualization "$JOB_DIR" "$LOG_FILE"

    return $exit_code
}

#######################################
# Visualizer Functions
#######################################

run_visualizer() {
    echo "=== Recovery Map Visualizer ==="
    echo ""

    local VISUALIZER="$SCRIPT_DIR/visualizer/visualize.py"

    if [ ! -f "$VISUALIZER" ]; then
        echo -e "${RED}Error: Visualizer not found at $VISUALIZER${NC}"
        return 1
    fi

    echo "Options:"
    echo "  1. Generate from ddrescue log file"
    echo "  2. Generate from recovery job directory (includes file mapping)"
    echo "  3. View recent visualizations"
    echo ""
    read -p "Select option [1-3]: " VIS_CHOICE

    case "$VIS_CHOICE" in
        1)
            # List recent log files
            echo ""
            echo "Recent ddrescue log files:"
            local count=0
            local log_files=()
            for log in $(find "$HOME/Desktop" "$SCRIPT_DIR" -name "*.log" -type f 2>/dev/null | head -10); do
                # Check if it looks like a ddrescue log
                if head -5 "$log" 2>/dev/null | grep -q "current_pos\|rescued"; then
                    count=$((count + 1))
                    log_files+=("$log")
                    local log_size=$(stat -c%s "$log" 2>/dev/null)
                    local log_date=$(stat -c%y "$log" 2>/dev/null | cut -d. -f1)
                    echo "  [$count] $log"
                    echo "       Modified: $log_date"
                fi
            done

            if [ $count -eq 0 ]; then
                echo "  No ddrescue log files found."
                echo ""
                read -p "Enter log file path manually: " LOG_FILE
            else
                echo ""
                read -p "Select log [1-$count] or enter path: " LOG_CHOICE
                if [[ "$LOG_CHOICE" =~ ^[0-9]+$ ]] && [ "$LOG_CHOICE" -ge 1 ] && [ "$LOG_CHOICE" -le $count ]; then
                    LOG_FILE="${log_files[$((LOG_CHOICE-1))]}"
                else
                    LOG_FILE="$LOG_CHOICE"
                fi
            fi

            if [ ! -f "$LOG_FILE" ]; then
                echo -e "${RED}Error: Log file not found${NC}"
                return 1
            fi

            # Generate output filename
            local base_name=$(basename "$LOG_FILE" .log)
            local OUTPUT_FILE="$SCRIPT_DIR/visualizer/${base_name}_recovery_map.html"

            echo ""
            echo "Generating visualization..."
            python3 "$VISUALIZER" --log "$LOG_FILE" --output "$OUTPUT_FILE"

            if [ -f "$OUTPUT_FILE" ]; then
                echo -e "${GREEN}Visualization saved to: $OUTPUT_FILE${NC}"
                echo ""
                read -p "Open in browser? [Y/n]: " OPEN_CHOICE
                if [[ ! "$OPEN_CHOICE" =~ ^[Nn]$ ]]; then
                    xdg-open "$OUTPUT_FILE" 2>/dev/null || \
                    sensible-browser "$OUTPUT_FILE" 2>/dev/null || \
                    echo "Please open manually: $OUTPUT_FILE"
                fi
            fi
            ;;

        2)
            # List job directories
            echo ""
            echo "Recovery job directories:"
            local count=0
            local job_dirs=()
            for job in "$SCRIPT_DIR"/*_recovery "$SCRIPT_DIR"/*_analysis; do
                if [ -d "$job" ] && [ -f "$job/recovery_state.json" ]; then
                    count=$((count + 1))
                    job_dirs+=("$job")
                    local job_name=$(basename "$job")
                    echo "  [$count] $job_name"
                fi
            done

            if [ $count -eq 0 ]; then
                echo "  No recovery jobs found."
                echo ""
                read -p "Enter job directory path: " JOB_DIR
            else
                echo ""
                read -p "Select job [1-$count] or enter path: " JOB_CHOICE
                if [[ "$JOB_CHOICE" =~ ^[0-9]+$ ]] && [ "$JOB_CHOICE" -ge 1 ] && [ "$JOB_CHOICE" -le $count ]; then
                    JOB_DIR="${job_dirs[$((JOB_CHOICE-1))]}"
                else
                    JOB_DIR="$JOB_CHOICE"
                fi
            fi

            if [ ! -d "$JOB_DIR" ]; then
                echo -e "${RED}Error: Job directory not found${NC}"
                return 1
            fi

            # Put visualization in job directory
            local OUTPUT_FILE="$JOB_DIR/recovery_map.html"

            echo ""
            echo "Generating visualization with file mapping..."
            python3 "$VISUALIZER" --job "$JOB_DIR" --output "$OUTPUT_FILE"

            if [ -f "$OUTPUT_FILE" ]; then
                echo -e "${GREEN}Visualization saved to: $OUTPUT_FILE${NC}"
                echo ""
                read -p "Open in browser? [Y/n]: " OPEN_CHOICE
                if [[ ! "$OPEN_CHOICE" =~ ^[Nn]$ ]]; then
                    xdg-open "$OUTPUT_FILE" 2>/dev/null || \
                    sensible-browser "$OUTPUT_FILE" 2>/dev/null || \
                    echo "Please open manually: $OUTPUT_FILE"
                fi
            fi
            ;;

        3)
            # List existing visualizations
            echo ""
            echo "Existing visualizations:"
            local count=0
            local html_files=()

            # Check job directories first
            for html in "$SCRIPT_DIR"/*_recovery/recovery_map.html "$SCRIPT_DIR"/*_analysis/recovery_map.html; do
                if [ -f "$html" ]; then
                    count=$((count + 1))
                    html_files+=("$html")
                    local job_name=$(basename "$(dirname "$html")")
                    local mod_date=$(stat -c%y "$html" 2>/dev/null | cut -d. -f1)
                    echo "  [$count] $job_name/recovery_map.html"
                    echo "       Modified: $mod_date"
                fi
            done

            # Also check visualizer directory for legacy files
            for html in "$SCRIPT_DIR/visualizer/"*.html; do
                if [ -f "$html" ]; then
                    count=$((count + 1))
                    html_files+=("$html")
                    local html_name=$(basename "$html")
                    local mod_date=$(stat -c%y "$html" 2>/dev/null | cut -d. -f1)
                    echo "  [$count] visualizer/$html_name"
                    echo "       Modified: $mod_date"
                fi
            done

            if [ $count -eq 0 ]; then
                echo "  No visualizations found."
                return
            fi

            echo ""
            read -p "Select visualization [1-$count] or press Enter to cancel: " VIS_CHOICE
            if [[ "$VIS_CHOICE" =~ ^[0-9]+$ ]] && [ "$VIS_CHOICE" -ge 1 ] && [ "$VIS_CHOICE" -le $count ]; then
                local selected="${html_files[$((VIS_CHOICE-1))]}"
                echo "Opening: $selected"
                xdg-open "$selected" 2>/dev/null || \
                sensible-browser "$selected" 2>/dev/null || \
                echo "Please open manually: $selected"
            fi
            ;;

        *)
            echo -e "${RED}Invalid option${NC}"
            ;;
    esac
}

# Function to offer cloning remaining areas after targeted recovery
offer_remaining_clone() {
    local source="$1"
    local dest="$2"
    local log_file="$3"

    # Skip if source or dest not set
    [ -z "$source" ] || [ -z "$dest" ] || [ -z "$log_file" ] && return 0

    # Skip if source device is gone
    if [ ! -b "$source" ]; then
        echo ""
        echo "Source device ($source) no longer accessible - cannot continue cloning."
        return 0
    fi

    # Calculate drive size and what's been recovered
    local drive_size
    drive_size=$(blockdev --getsize64 "$source" 2>/dev/null) || return 0

    local recovered=0
    if [ -f "$log_file" ]; then
        recovered=$(awk '!/^#/ && NF==3 && $3=="+" {
            gsub(/^0x/,"",$2);
            cmd="printf \"%d\" 0x"$2; cmd | getline val; close(cmd);
            total += val
        } END { printf "%d", total }' "$log_file" 2>/dev/null)
    fi

    [ -z "$recovered" ] && recovered=0

    local remaining=$((drive_size - recovered))
    [ "$remaining" -le 0 ] && return 0

    local remaining_gb=$((remaining / 1073741824))
    local pct_done=$((recovered * 100 / drive_size))

    echo ""
    echo -e "${CYAN}============================================${NC}"
    echo -e "${CYAN}  CONTINUE CLONING?${NC}"
    echo -e "${CYAN}============================================${NC}"
    echo "Targeted recovery covered ~${pct_done}% of the drive."
    echo "${remaining_gb} GB remains uncloned."
    echo ""
    echo "  1. Clone remaining areas (other partitions + free space)"
    echo "     Useful for: deleted file recovery, other partitions,"
    echo "     corrupt partition table, or just getting everything"
    echo "  2. Skip - done with this recovery"
    echo ""

    read -p "Continue cloning remaining areas? [1/2, default=2]: " CLONE_CHOICE
    if [ "$CLONE_CHOICE" != "1" ]; then
        echo "Skipped."
        return 0
    fi

    echo ""
    echo "Cloning remaining ${remaining_gb} GB..."
    echo "Using existing log - ddrescue will skip already-recovered areas."
    echo "Command: ddrescue -d -f $source $dest $log_file"
    echo ""

    ddrescue -d -f "$source" "$dest" "$log_file"
    local clone_exit=$?

    if [ $clone_exit -eq 0 ]; then
        echo -e "\n${GREEN}Full clone completed successfully!${NC}"
    else
        echo -e "\n${YELLOW}Clone finished with exit code: $clone_exit${NC}"
        echo "Resume with: ddrescue -d -f $source $dest $log_file"
    fi
}

# Function to offer visualization after recovery
offer_visualization() {
    local job_dir="$1"
    local log_file="$2"

    echo ""
    read -p "Generate recovery map visualization? [y/N]: " GEN_VIS
    if [[ "$GEN_VIS" =~ ^[Yy]$ ]]; then
        local VISUALIZER="$SCRIPT_DIR/visualizer/visualize.py"
        if [ -f "$VISUALIZER" ]; then
            # Put visualization in job directory
            local OUTPUT_FILE="$job_dir/recovery_map.html"

            echo "Generating visualization..."
            python3 "$VISUALIZER" --job "$job_dir" --output "$OUTPUT_FILE" 2>/dev/null || \
            python3 "$VISUALIZER" --log "$log_file" --output "$OUTPUT_FILE" 2>/dev/null

            if [ -f "$OUTPUT_FILE" ]; then
                echo -e "${GREEN}Saved: $OUTPUT_FILE${NC}"
                xdg-open "$OUTPUT_FILE" 2>/dev/null &
            fi
        fi
    fi
}

#######################################
# Mac Volume Manager (APFS + HFS+)
#######################################
function mac_volume_menu() {
    echo "=== Mac Volume Manager (APFS / HFS+) ==="
    echo ""

    # Check driver status
    local apfs_available=false
    local hfs_available=false

    # Check APFS (Paragon uapfs)
    if lsmod | grep -q "uapfs"; then
        echo -e "  APFS driver:  ${GREEN}uapfs loaded${NC}"
        apfs_available=true
    else
        echo -n "  APFS driver:  "
        if modprobe jnl 2>/dev/null && modprobe uapfs 2>/dev/null; then
            echo -e "${GREEN}uapfs loaded${NC}"
            apfs_available=true
        else
            echo -e "${YELLOW}not available${NC} (optional, APFS only — see DEPENDENCIES.md)"
        fi
    fi

    # Check HFS+ (built-in kernel module)
    if modinfo hfsplus &>/dev/null; then
        echo -e "  HFS+ driver:  ${GREEN}hfsplus available${NC}"
        hfs_available=true
    else
        echo -e "  HFS+ driver:  ${RED}not available${NC}"
    fi

    echo ""
    echo "Options:"
    echo "  1. Mount a Mac volume (auto-detects APFS and HFS+)"
    echo "  2. Unmount a Mac volume"
    echo "  3. Show currently mounted Mac volumes"
    echo "  4. Cancel"
    echo ""
    read -p "Select option [1-4]: " mac_option

    case "$mac_option" in
        1) mac_mount_volume "$apfs_available" "$hfs_available" ;;
        2) mac_unmount_volume ;;
        3) mac_show_mounted ;;
        4) return 0 ;;
        *) echo -e "${RED}Invalid option${NC}" ;;
    esac
}

function mac_detect_partitions() {
    # Detect APFS and HFS+ partitions on connected drives
    # Populates MAC_PARTITIONS array with "device|size|label|disk_model|fstype" entries
    MAC_PARTITIONS=()
    MAC_PART_COUNT=0

    while read -r dev; do
        [ -z "$dev" ] && continue
        local dev_path="/dev/$dev"

        local fs_type=$(blkid -s TYPE -o value "$dev_path" 2>/dev/null)
        local part_type=$(blkid -s PART_ENTRY_TYPE -o value "$dev_path" 2>/dev/null)

        local detected_fs=""

        # Check for APFS
        if [ "$fs_type" = "apfs" ] || [ "$fs_type" = "APFS" ]; then
            detected_fs="apfs"
        elif [ "$part_type" = "7c3457ef-0000-11aa-aa11-00306543ecac" ]; then
            detected_fs="apfs"
        fi

        # Check for HFS+
        if [ "$fs_type" = "hfsplus" ] || [ "$fs_type" = "hfs+" ]; then
            detected_fs="hfsplus"
        elif [ "$fs_type" = "hfs" ]; then
            detected_fs="hfs"
        # Apple HFS/HFS+ partition GUID
        elif [ "$part_type" = "48465300-0000-11aa-aa11-00306543ecac" ]; then
            detected_fs="hfsplus"
        fi

        if [ -n "$detected_fs" ]; then
            local size=$(lsblk -no SIZE "$dev_path" 2>/dev/null | head -1 | tr -d ' ')
            local label=$(blkid -s LABEL -o value "$dev_path" 2>/dev/null)
            local parent_disk=$(lsblk -no PKNAME "$dev_path" 2>/dev/null | head -1)
            local disk_model=""
            if [ -n "$parent_disk" ]; then
                disk_model=$(lsblk -no MODEL "/dev/$parent_disk" 2>/dev/null | head -1 | sed 's/ *$//')
            fi

            MAC_PARTITIONS+=("${dev_path}|${size}|${label:-unlabeled}|${disk_model:-unknown}|${detected_fs}")
            MAC_PART_COUNT=$((MAC_PART_COUNT + 1))
        fi
    done < <(lsblk -rno NAME,TYPE 2>/dev/null | awk '$2=="part" {print $1}')
}

function mac_mount_volume() {
    local apfs_available="$1"
    local hfs_available="$2"

    echo ""
    echo -e "${GREEN}Scanning for Mac partitions (APFS / HFS+)...${NC}"

    mac_detect_partitions

    local SELECTED_DEV=""
    local SELECTED_FS=""

    if [ "$MAC_PART_COUNT" -eq 0 ]; then
        echo -e "${YELLOW}No APFS or HFS+ partitions detected on connected drives.${NC}"
        echo ""
        echo "You can enter a device path manually."
        echo "  (e.g., /dev/sdc2 for an APFS container, /dev/sdc3 for HFS+)"
        echo ""
        read -p "Enter device path (or press Enter to cancel): " manual_dev
        if [ -z "$manual_dev" ]; then
            return 0
        fi
        if [ ! -b "$manual_dev" ]; then
            echo -e "${RED}Error: $manual_dev is not a valid block device${NC}"
            return 1
        fi
        SELECTED_DEV="$manual_dev"

        # Try to detect FS on the manual device
        local manual_fs=$(blkid -s TYPE -o value "$manual_dev" 2>/dev/null)
        case "$manual_fs" in
            apfs|APFS) SELECTED_FS="apfs" ;;
            hfsplus|hfs+|hfs|HFS) SELECTED_FS="hfsplus" ;;
            *)
                echo ""
                echo "Could not auto-detect filesystem. Select type:"
                echo "  1. APFS"
                echo "  2. HFS+"
                read -p "Select [1-2]: " fs_pick
                case "$fs_pick" in
                    1) SELECTED_FS="apfs" ;;
                    2) SELECTED_FS="hfsplus" ;;
                    *) echo -e "${RED}Invalid selection${NC}"; return 1 ;;
                esac
                ;;
        esac
    else
        echo ""
        echo -e "${CYAN}Found Mac partitions:${NC}"
        echo ""

        local i=0
        for entry in "${MAC_PARTITIONS[@]}"; do
            i=$((i + 1))
            local dev=$(echo "$entry" | cut -d'|' -f1)
            local size=$(echo "$entry" | cut -d'|' -f2)
            local label=$(echo "$entry" | cut -d'|' -f3)
            local model=$(echo "$entry" | cut -d'|' -f4)
            local fstype=$(echo "$entry" | cut -d'|' -f5)

            # FS type badge
            local fs_badge=""
            case "$fstype" in
                apfs) fs_badge="${PURPLE}APFS${NC}" ;;
                hfsplus) fs_badge="${BLUE}HFS+${NC}" ;;
                hfs) fs_badge="${BLUE}HFS${NC}" ;;
            esac

            # Check if already mounted
            local mount_status=""
            if mount | grep -q "^${dev} "; then
                local mpoint=$(mount | grep "^${dev} " | awk '{print $3}')
                mount_status="${GREEN}[mounted: $mpoint]${NC}"
            fi

            printf "  [%d] %-12s %6s  " "$i" "$dev" "$size"
            echo -e "$fs_badge  $label  $model"
            if [ -n "$mount_status" ]; then
                echo -e "      $mount_status"
            fi
        done

        echo ""
        echo "  [M] Enter device path manually"
        echo ""
        read -p "Select partition [1-$MAC_PART_COUNT or M]: " mac_choice

        if [[ "$mac_choice" =~ ^[Mm]$ ]]; then
            read -p "Enter device path: " SELECTED_DEV
            if [ ! -b "$SELECTED_DEV" ]; then
                echo -e "${RED}Error: $SELECTED_DEV is not a valid block device${NC}"
                return 1
            fi
            # Detect FS
            local manual_fs=$(blkid -s TYPE -o value "$SELECTED_DEV" 2>/dev/null)
            case "$manual_fs" in
                apfs|APFS) SELECTED_FS="apfs" ;;
                hfsplus|hfs+|hfs|HFS) SELECTED_FS="hfsplus" ;;
                *)
                    echo "Select filesystem type:"
                    echo "  1. APFS"
                    echo "  2. HFS+"
                    read -p "Select [1-2]: " fs_pick
                    case "$fs_pick" in
                        1) SELECTED_FS="apfs" ;;
                        2) SELECTED_FS="hfsplus" ;;
                        *) echo -e "${RED}Invalid${NC}"; return 1 ;;
                    esac
                    ;;
            esac
        elif [[ "$mac_choice" =~ ^[0-9]+$ ]] && [ "$mac_choice" -ge 1 ] && [ "$mac_choice" -le "$MAC_PART_COUNT" ]; then
            local selected="${MAC_PARTITIONS[$((mac_choice-1))]}"
            SELECTED_DEV=$(echo "$selected" | cut -d'|' -f1)
            SELECTED_FS=$(echo "$selected" | cut -d'|' -f5)
        else
            echo -e "${RED}Invalid selection${NC}"
            return 1
        fi
    fi

    # Check driver availability for selected FS
    if [ "$SELECTED_FS" = "apfs" ] && [ "$apfs_available" != "true" ]; then
        echo -e "${RED}Error: APFS driver (uapfs) is not available.${NC}"
        echo "Optional (APFS mounting only): Paragon "APFS for Linux" — https://www.paragon-software.com/ (see DEPENDENCIES.md)"
        return 1
    fi
    if [ "$SELECTED_FS" = "hfsplus" ] || [ "$SELECTED_FS" = "hfs" ]; then
        if [ "$hfs_available" != "true" ]; then
            echo -e "${RED}Error: HFS+ kernel module not available.${NC}"
            echo "Try: sudo apt install hfsprogs"
            return 1
        fi
    fi

    # Check if already mounted
    if mount | grep -q "^${SELECTED_DEV} "; then
        local existing_mount=$(mount | grep "^${SELECTED_DEV} " | awk '{print $3}')
        echo -e "${YELLOW}This partition is already mounted at: $existing_mount${NC}"
        echo ""
        read -p "Open file manager there? [y/N]: " open_fm
        if [[ "$open_fm" =~ ^[Yy]$ ]]; then
            xdg-open "$existing_mount" 2>/dev/null &
        fi
        mac_offer_rsync "$existing_mount"
        return 0
    fi

    # Choose mount point
    echo ""
    local fs_short="mac"
    [ "$SELECTED_FS" = "apfs" ] && fs_short="apfs"
    [ "$SELECTED_FS" = "hfsplus" ] || [ "$SELECTED_FS" = "hfs" ] && fs_short="hfs"
    local default_mount="/media/$fs_short"
    if mountpoint -q "$default_mount" 2>/dev/null; then
        local n=2
        while mountpoint -q "/media/${fs_short}${n}" 2>/dev/null; do
            n=$((n + 1))
        done
        default_mount="/media/${fs_short}${n}"
    fi

    read -p "Mount point [$default_mount]: " mount_point
    mount_point=${mount_point:-$default_mount}

    if [ ! -d "$mount_point" ]; then
        mkdir -p "$mount_point"
        echo "Created mount point: $mount_point"
    fi

    # Get real user info for permissions
    local real_user=$(logname 2>/dev/null || echo "$SUDO_USER")
    local real_uid=$(id -u "$real_user" 2>/dev/null)
    local real_gid=$(id -g "$real_user" 2>/dev/null)

    local mount_cmd=""

    if [ "$SELECTED_FS" = "apfs" ]; then
        # APFS mount options
        echo ""
        echo "Mount options:"
        echo "  1. Read-only (safest for recovery)"
        echo "  2. Read-write"
        echo "  3. Read-only with all subvolumes"
        echo "  4. Read-write with all subvolumes"
        echo ""
        read -p "Select [1-4, default=1]: " mount_mode
        mount_mode=${mount_mode:-1}

        local mount_opts="nls=utf8"
        case "$mount_mode" in
            1) mount_opts="$mount_opts,ro" ;;
            2) mount_opts="$mount_opts,rw" ;;
            3) mount_opts="$mount_opts,ro,subvolumes" ;;
            4) mount_opts="$mount_opts,rw,subvolumes" ;;
            *) mount_opts="$mount_opts,ro" ;;
        esac

        if [ -n "$real_uid" ]; then
            mount_opts="$mount_opts,uid=$real_uid,gid=$real_gid,fmask=000,dmask=000"
        fi

        # Check for encryption
        echo ""
        read -p "Is this volume encrypted (FileVault)? [y/N]: " is_encrypted
        if [[ "$is_encrypted" =~ ^[Yy]$ ]]; then
            echo "Enter the macOS account password or Recovery Key:"
            read -s -p "Password: " apfs_password
            echo ""
            mount_opts="$mount_opts,pass1='${apfs_password}'"
        fi

        mount_cmd="mount -t uapfs -o $mount_opts $SELECTED_DEV $mount_point"

    else
        # HFS+ mount options
        echo ""
        echo "Mount options:"
        echo "  1. Read-only (safest for recovery)"
        echo "  2. Read-write (force mount)"
        echo ""
        read -p "Select [1-2, default=1]: " mount_mode
        mount_mode=${mount_mode:-1}

        local mount_opts=""
        case "$mount_mode" in
            1) mount_opts="ro" ;;
            2) mount_opts="force,rw" ;;
            *) mount_opts="ro" ;;
        esac

        if [ -n "$real_uid" ]; then
            mount_opts="$mount_opts,uid=$real_uid,gid=$real_gid,umask=002"
        fi

        mount_cmd="mount -t hfsplus -o $mount_opts $SELECTED_DEV $mount_point"
    fi

    # Attempt mount
    echo ""
    local fs_display="APFS"
    [ "$SELECTED_FS" = "hfsplus" ] || [ "$SELECTED_FS" = "hfs" ] && fs_display="HFS+"
    echo -e "${CYAN}Mounting $SELECTED_DEV ($fs_display) → $mount_point${NC}"
    echo "  Command: $mount_cmd"
    echo ""

    if eval "$mount_cmd" 2>&1; then
        echo ""
        echo -e "${GREEN}Successfully mounted!${NC}"
        echo ""

        # Show contents summary
        echo "Volume contents:"
        ls -la "$mount_point" 2>/dev/null | head -15
        local file_count=$(find "$mount_point" -maxdepth 2 -type f 2>/dev/null | wc -l)
        echo ""
        echo "  Files (depth 2): ~$file_count"

        # APFS subvolumes check
        if [ "$SELECTED_FS" = "apfs" ] && [ -d "$mount_point/Ufsd_Volumes" ]; then
            echo ""
            echo -e "${CYAN}Subvolumes found:${NC}"
            ls -la "$mount_point/Ufsd_Volumes/" 2>/dev/null
        fi

        # Verify mount
        echo ""
        mount | grep "^${SELECTED_DEV} " | while read -r line; do
            echo -e "  ${GREEN}$line${NC}"
        done

        # Fix HFS+ permissions if mounted read-only (files may be owned by root)
        if [ "$SELECTED_FS" != "apfs" ] && [ -n "$real_uid" ]; then
            # HFS+ doesn't support uid/gid mount options like APFS does
            # but files should still be readable
            echo ""
            echo -e "${YELLOW}Note: HFS+ files are owned by their original UIDs.${NC}"
            echo "  Use sudo or rsync_recovery to copy files if permission denied."
        fi

        # Offer to open file manager
        echo ""
        read -p "Open file manager? [y/N]: " open_fm
        if [[ "$open_fm" =~ ^[Yy]$ ]]; then
            if [ -n "$real_user" ]; then
                su - "$real_user" -c "DISPLAY=$DISPLAY XAUTHORITY=$XAUTHORITY xdg-open '$mount_point'" 2>/dev/null &
            else
                xdg-open "$mount_point" 2>/dev/null &
            fi
        fi

        # Offer rsync recovery
        mac_offer_rsync "$mount_point"
    else
        echo -e "${RED}Mount failed!${NC}"
        echo ""
        if [ "$SELECTED_FS" = "apfs" ]; then
            echo "Possible issues:"
            echo "  - Volume may be encrypted (try with password)"
            echo "  - Volume may be damaged"
        else
            echo "Possible issues:"
            echo "  - Volume may need fsck: sudo fsck.hfsplus -f $SELECTED_DEV"
            echo "  - Journal may be dirty (try read-only first)"
            echo "  - Volume may be damaged"
        fi
        echo "  - Check: dmesg | tail -20"
        return 1
    fi
}

function mac_offer_rsync() {
    local mount_point="$1"

    echo ""
    echo -e "${CYAN}Would you like to copy files from this volume?${NC}"
    echo "  1. Launch rsync_recovery.sh (smart recovery with filtering)"
    echo "  2. No, just leave it mounted"
    echo ""
    read -p "Select [1-2, default=2]: " rsync_choice

    if [ "$rsync_choice" = "1" ]; then
        local rsync_script="$RSYNC_RECOVERY_DIR/rsync_recovery.sh"
        if [ -f "$rsync_script" ]; then
            echo ""
            echo -e "${GREEN}Launching rsync_recovery.sh...${NC}"
            echo -e "${YELLOW}Note: The volume is mounted at: $mount_point${NC}"
            echo -e "${YELLOW}Select it as the source in rsync_recovery.${NC}"
            echo ""
            bash "$rsync_script"
        else
            echo -e "${RED}Error: rsync_recovery.sh not found at $rsync_script${NC}"
        fi
    fi
}

function mac_unmount_volume() {
    echo ""
    echo -e "${GREEN}Currently mounted Mac volumes:${NC}"
    echo ""

    local mounted_vols=()
    local count=0

    # Find APFS (uapfs) and HFS+ (hfsplus) mounts
    while read -r line; do
        [ -z "$line" ] && continue
        count=$((count + 1))
        local dev=$(echo "$line" | awk '{print $1}')
        local mpoint=$(echo "$line" | awk '{print $3}')
        local fstype=$(echo "$line" | sed 's/.*type \([^ ]*\).*/\1/')
        mounted_vols+=("$dev|$mpoint|$fstype")

        local fs_badge=""
        case "$fstype" in
            uapfs) fs_badge="${PURPLE}APFS${NC}" ;;
            hfsplus) fs_badge="${BLUE}HFS+${NC}" ;;
        esac

        printf "  [%d] %-12s → %s  " "$count" "$dev" "$mpoint"
        echo -e "$fs_badge"
    done < <(mount | grep -E "type (uapfs|hfsplus)")

    if [ "$count" -eq 0 ]; then
        echo "  No Mac volumes are currently mounted."
        return 0
    fi

    echo ""
    echo "  [A] Unmount ALL Mac volumes"
    echo ""
    read -p "Select volume to unmount [1-$count or A]: " unmount_choice

    if [[ "$unmount_choice" =~ ^[Aa]$ ]]; then
        echo ""
        for entry in "${mounted_vols[@]}"; do
            local mpoint=$(echo "$entry" | cut -d'|' -f2)
            echo -n "Unmounting $mpoint... "
            if umount "$mpoint" 2>/dev/null; then
                echo -e "${GREEN}OK${NC}"
                rmdir "$mpoint" 2>/dev/null
            else
                echo -e "${RED}FAILED (busy?)${NC}"
                echo "  Try: lsof +f -- '$mpoint' | head"
            fi
        done
    elif [[ "$unmount_choice" =~ ^[0-9]+$ ]] && [ "$unmount_choice" -ge 1 ] && [ "$unmount_choice" -le "$count" ]; then
        local entry="${mounted_vols[$((unmount_choice-1))]}"
        local mpoint=$(echo "$entry" | cut -d'|' -f2)

        echo -n "Unmounting $mpoint... "
        if umount "$mpoint" 2>/dev/null; then
            echo -e "${GREEN}OK${NC}"
            rmdir "$mpoint" 2>/dev/null
        else
            echo -e "${RED}FAILED${NC}"
            echo "  The volume may be busy. Close any open files/terminals using it."
            read -p "  Force unmount (lazy)? [y/N]: " force_unmount
            if [[ "$force_unmount" =~ ^[Yy]$ ]]; then
                if umount -l "$mpoint" 2>/dev/null; then
                    echo -e "  ${GREEN}Lazy unmount scheduled${NC}"
                    rmdir "$mpoint" 2>/dev/null
                else
                    echo -e "  ${RED}Force unmount also failed${NC}"
                fi
            fi
        fi
    else
        echo -e "${RED}Invalid selection${NC}"
    fi
}

function mac_show_mounted() {
    echo ""
    echo -e "${GREEN}Currently mounted Mac volumes:${NC}"
    echo ""

    local count=0
    while read -r line; do
        [ -z "$line" ] && continue
        count=$((count + 1))
        local dev=$(echo "$line" | awk '{print $1}')
        local mpoint=$(echo "$line" | awk '{print $3}')
        local fstype=$(echo "$line" | sed 's/.*type \([^ ]*\).*/\1/')
        local opts=$(echo "$line" | sed 's/.*(\(.*\))/\1/')

        local fs_badge=""
        case "$fstype" in
            uapfs) fs_badge="${PURPLE}[APFS]${NC}" ;;
            hfsplus) fs_badge="${BLUE}[HFS+]${NC}" ;;
        esac

        echo -e "  $fs_badge ${CYAN}$dev${NC} → ${GREEN}$mpoint${NC}"
        echo "    Options: $opts"

        local usage=$(df -h "$mpoint" 2>/dev/null | tail -1)
        if [ -n "$usage" ]; then
            local used=$(echo "$usage" | awk '{print $3}')
            local avail=$(echo "$usage" | awk '{print $4}')
            local pct=$(echo "$usage" | awk '{print $5}')
            echo "    Used: $used | Available: $avail | $pct full"
        fi
    done < <(mount | grep -E "type (uapfs|hfsplus)")

    if [ "$count" -eq 0 ]; then
        echo "  No Mac volumes are currently mounted."
    fi
}

function quick_status() {
    echo "=== Quick Status Dashboard ==="
    echo
    
    # Check for running ddrescue processes
    echo -e "${GREEN}DDRescue Jobs:${NC}"
    local ddrescue_count=$(pgrep -f "ddrescue.*-[AMdv]" | wc -l)
    if [ $ddrescue_count -gt 0 ]; then
        echo -e "  ${GREEN}✓${NC} $ddrescue_count active ddrescue process(es)"
        ps aux | grep -E "ddrescue.*-[AMdv]" | grep -v grep | awk '{print "    PID:", $2, "Device:", $13, "Image:", $14}' | head -5
    else
        echo "  No active ddrescue processes"
    fi
    echo
    
    # Check for mounted Mac volumes (APFS + HFS+)
    echo -e "${GREEN}Mac Volumes:${NC}"
    local mac_count=$(mount | grep -cE "type (uapfs|hfsplus)")
    if [ $mac_count -gt 0 ]; then
        echo -e "  ${GREEN}✓${NC} $mac_count mounted Mac volume(s)"
        mount | grep "type uapfs" | awk '{print "     APFS:", $1, "→", $3}'
        mount | grep "type hfsplus" | awk '{print "     HFS+:", $1, "→", $3}'
    else
        echo "  No Mac volumes mounted"
    fi
    echo

    # Check for mounted images
    echo -e "${GREEN}Mounted Images:${NC}"
    local mount_count=$(mount | grep -c "/mnt/recovery-")
    if [ $mount_count -gt 0 ]; then
        echo -e "  ${GREEN}✓${NC} $mount_count mounted image(s)"
        mount | grep "/mnt/recovery-" | awk '{print "    ", $3, "←", $1}'
    else
        echo "  No recovery images currently mounted"
    fi
    echo
    
    # Check for RecuperaBit processes
    echo -e "${GREEN}RecuperaBit Scans:${NC}"
    if pgrep -f "python3.*main.py.*recuperabit" > /dev/null; then
        echo -e "  ${GREEN}✓${NC} RecuperaBit scan in progress"
        local recuperabit_pid=$(pgrep -f "python3.*main.py.*recuperabit")
        local recuperabit_device=$(ps aux | grep -E "python3.*main.py.*recuperabit" | grep -v grep | awk '{print $13}' | head -1)
        echo "    PID: $recuperabit_pid | Device: $recuperabit_device"
        
        # Show latest log activity
        local latest_log=$(ls -t $HOME/recuperabit*.log 2>/dev/null | head -1)
        if [ -n "$latest_log" ]; then
            echo "    Latest activity:"
            tail -3 "$latest_log" | sed 's/^/      /'
        fi
    else
        echo "  No active RecuperaBit scans"
    fi
    echo
    
    # Check loop devices
    echo -e "${GREEN}Loop Devices:${NC}"
    local loop_count=$(losetup -l 2>/dev/null | grep -c "loop[0-9]")
    if [ $loop_count -gt 0 ]; then
        echo -e "  ${GREEN}✓${NC} $loop_count loop device(s) active"
        losetup -l 2>/dev/null | grep "loop[0-9]" | awk '{print "    ", $1, "→", $6}' | head -5
    else
        echo "  No loop devices active"
    fi
}

function manage_sudo() {
    echo "=== DDRescue Sudo Management ==="
    echo
    
    check_sudo_status
    echo
    
    if [ -f "$SUDOERS_FILE" ]; then
        echo "Options:"
        echo "1. Keep current sudo configuration"
        echo "2. Remove passwordless sudo for ddrescue"
        read -p "Select option [1-2]: " sudo_option
        
        if [ "$sudo_option" = "2" ]; then
            # Check if any rules still exist
            local rules_exist=0
            for rule in $UDEV_RULES_DIR/99-ddrescue-*.rules; do
                if [ -f "$rule" ]; then
                    rules_exist=1
                    break
                fi
            done
            
            if [ $rules_exist -eq 1 ]; then
                echo
                echo "Warning: DDRescue rules still exist. Removing sudo permissions may break auto-run."
                read -p "Are you sure you want to remove sudo permissions? [y/N]: " confirm
                if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
                    echo "Sudo permissions kept"
                    return
                fi
            fi
            
            echo "Removing passwordless sudo configuration..."
            sudo rm -f "$SUDOERS_FILE"
            echo "Passwordless sudo removed for ddrescue"
        fi
    else
        echo "Would you like to set up passwordless sudo for ddrescue?"
        read -p "Configure passwordless sudo? [y/N]: " setup_sudo
        
        if [[ "$setup_sudo" =~ ^[Yy]$ ]]; then
            DDRESCUE_PATH=$(which ddrescue)
            if [ -z "$DDRESCUE_PATH" ]; then
                echo "Error: ddrescue not found in PATH"
                return
            fi
            
            echo "# Allow $USER to run ddrescue without password" | sudo tee "$SUDOERS_FILE" > /dev/null
            echo "$USER ALL=(ALL) NOPASSWD: $DDRESCUE_PATH" | sudo tee -a "$SUDOERS_FILE" > /dev/null
            sudo chmod 440 "$SUDOERS_FILE"
            
            if sudo -n ddrescue --version >/dev/null 2>&1; then
                echo "Passwordless sudo configured successfully"
            else
                echo "Warning: Configuration may have failed"
            fi
        fi
    fi
}

# Lightweight check for required external tools; warns in the menu header if any are missing.
check_core_deps() {
    local missing=()
    command -v ddrescue >/dev/null 2>&1 || missing+=("ddrescue")
    command -v icat     >/dev/null 2>&1 || missing+=("sleuthkit(icat)")
    command -v expect   >/dev/null 2>&1 || missing+=("expect")
    if [ ${#missing[@]} -gt 0 ]; then
        echo -e "${YELLOW}⚠ Missing dependencies: ${missing[*]} — press 'D' to install (see DEPENDENCIES.md)${NC}"
        echo
    fi
}

# Main menu loop
while true; do
    # Clear screen for better visibility
    clear
    
    # System info header
    echo "=== Data Recovery Manager v1.7 ==="
    echo -e "User: ${GREEN}$(logname 2>/dev/null || echo $SUDO_USER)${NC} | Date: $(date +"%Y-%m-%d %H:%M") | Free space: ${GREEN}$(df -h / | awk 'NR==2 {print $4}')${NC}"
    echo "─────────────────────────────────────────────────────────────────"
    check_core_deps
    echo
    echo -e "${CYAN}DDRescue Recovery:${NC}"
    echo -e "  ${GREEN}T. Targeted Recovery (MFT/bitmap-based, skips empty space)${NC}"
    echo -e "  ${GREEN}F. Full Drive Clone (sector-by-sector, entire drive)${NC}"
    echo -e "  ${GREEN}R. Resume Previous Recovery${NC}"
    echo
    echo "DDRescue Automation (udev rules):"
    echo "  1. List all ddrescue rules"
    echo "  2. Remove a specific rule"
    echo "  3. Remove ALL ddrescue rules"
    echo "  4. Reload udev rules"
    echo "  5. Manage sudo permissions"
    echo "  6. Monitor ddrescue output (tail log)"
    echo
    echo "Recovery Tools:"
    echo -e "  ${GREEN}A. Mount Mac Volume (APFS / HFS+)${NC}"
    echo "  7. Mount disk image partitions"
    echo "  8. Run RecuperaBit (NTFS file recovery)"
    echo "  9. Check RecuperaBit scan status"
    echo "  10. Interactive RecuperaBit recovery (use existing scan)"
    echo "  11. Automated RecuperaBit recovery (with ticket tracking)"
    echo
    echo "Visualization:"
    echo "  V. Generate Recovery Map (HTML visualization)"
    echo
    echo "System Status:"
    echo "  S. Quick Status (show all active operations)"
    echo
    echo "Setup:"
    echo "  D. Install / update dependencies"
    echo
    echo "  0. Exit"
    echo

    read -p "Select option [0-11,A,D,F,R,S,T,V]: " option

    case $option in
        D|d)
            if [ -f "$SCRIPT_DIR/install-dependencies.sh" ]; then
                bash "$SCRIPT_DIR/install-dependencies.sh"
            else
                echo -e "${RED}install-dependencies.sh not found at $SCRIPT_DIR${NC}"
            fi
            read -p "Press Enter to continue..."
            ;;
        1)
            list_rules
            ;;
        2)
            list_rules
            echo
            read -p "Enter job name to remove (without 99-ddrescue- prefix or .rules suffix): " job_name
            if [ -n "$job_name" ]; then
                if remove_rule "$job_name"; then
                    reload_rules
                fi
            else
                echo "No job name provided"
            fi
            ;;
        3)
            remove_all_rules
            reload_rules
            ;;
        4)
            reload_rules
            ;;
        5)
            manage_sudo
            ;;
        6)
            monitor_ddrescue
            ;;
        7)
            # Mount disk image
            echo "=== Mount Disk Image Tool ==="
            echo
            if [ -f "$SCRIPT_DIR/mount-disk-image.sh" ] && [ -x "$SCRIPT_DIR/mount-disk-image.sh" ]; then
                "$SCRIPT_DIR/mount-disk-image.sh"
            else
                echo -e "${RED}Error: mount-disk-image.sh not found or not executable${NC}"
                echo "Expected location: $SCRIPT_DIR/mount-disk-image.sh"
            fi
            ;;
        8)
            # Run RecuperaBit
            echo "=== RecuperaBit NTFS Recovery ==="
            echo
            echo "Note: RecuperaBit requires sudo access to read disk devices"
            echo
            read -p "Enter image file path or device (e.g., /dev/loop0p2): " device_path
            if [ -n "$device_path" ]; then
                # Validate device/file
                if [ -b "$device_path" ]; then
                    echo -e "${GREEN}✓ Valid block device: $device_path${NC}"
                elif [ -f "$device_path" ]; then
                    echo -e "${GREEN}✓ Valid file: $device_path${NC}"
                else
                    echo -e "${RED}Error: $device_path is not a valid block device or file${NC}"
                    read -p "Press Enter to continue..."
                    continue
                fi
                
                # Use fixed runner if available, otherwise fallback
                if [ -f "$SCRIPT_DIR/recuperabit-runner-fixed.sh" ]; then
                    RUNNER_SCRIPT="$SCRIPT_DIR/recuperabit-runner-fixed.sh"
                elif [ -f "$SCRIPT_DIR/run-recuperabit.sh" ]; then
                    RUNNER_SCRIPT="$SCRIPT_DIR/run-recuperabit.sh"
                else
                    echo -e "${RED}Error: No RecuperaBit runner script found${NC}"
                    read -p "Press Enter to continue..."
                    continue
                fi
                
                # Get output directory
                read -p "Enter output directory [$(get_real_home)/recuperabit-recovery]: " output_dir
                output_dir=${output_dir:-"$(get_real_home)/recuperabit-recovery"}
                
                echo "Starting RecuperaBit on $device_path..."
                echo "Files will be saved to: $output_dir/PartitionXXX/Root/"
                echo
                sudo "$SCRIPT_DIR/sudo-runner.sh" "$RUNNER_SCRIPT" "$device_path" "$output_dir"
            else
                echo -e "${YELLOW}No device specified${NC}"
            fi
            ;;
        9)
            # Check RecuperaBit status
            echo "=== RecuperaBit Scan Status ==="
            echo
            # Find most recent recuperabit log
            latest_log=$(ls -t $HOME/recuperabit*.log 2>/dev/null | head -1)
            if [ -n "$latest_log" ]; then
                echo "Most recent log: $latest_log"
                echo "Last 50 lines:"
                echo
                tail -50 "$latest_log"
                echo
                # Check if still running
                if pgrep -f "python3 main.py.*recuperabit" > /dev/null; then
                    echo "RecuperaBit is still running!"
                    echo "PID: $(pgrep -f "python3 main.py.*recuperabit")"
                else
                    echo "RecuperaBit is not currently running."
                fi
            else
                echo "No RecuperaBit logs found."
            fi
            ;;
        10)
            # Interactive RecuperaBit recovery
            echo "=== Interactive RecuperaBit Recovery ==="
            echo
            
            # Check for existing RecuperaBit installation
            if [ ! -d "$HOME/RecuperaBit" ]; then
                echo -e "${RED}Error: RecuperaBit not found at $HOME/RecuperaBit${NC}"
                read -p "Press Enter to continue..."
                continue
            fi
            
            # Show recent scan results
            echo "Recent RecuperaBit scans:"
            ls -lt $HOME/recuperabit*.log 2>/dev/null | head -5 | awk '{print "  ", $9}'
            echo
            
            # Check for saved scanners
            if [ -d "$HOME/RecuperaBit" ]; then
                cd "$HOME/RecuperaBit"
                if ls *.save 2>/dev/null | grep -q "."; then
                    echo -e "${GREEN}Found saved scanner files:${NC}"
                    ls -la *.save 2>/dev/null
                    echo
                fi
            fi
            
            echo "Available options:"
            echo "1. Use existing scan data (if available)"
            echo "2. Start fresh interactive session"
            echo "3. Show recovery instructions"
            echo "4. Cancel"
            echo
            read -p "Select option [1-4]: " recuperabit_option
            
            case $recuperabit_option in
                1|2)
                    read -p "Enter device or image path (e.g., /dev/loop0p2): " device_path
                    if [ -z "$device_path" ]; then
                        echo -e "${YELLOW}No device specified${NC}"
                        read -p "Press Enter to continue..."
                        continue
                    fi
                    
                    # Validate device/file
                    if [ ! -b "$device_path" ] && [ ! -f "$device_path" ]; then
                        echo -e "${RED}Error: $device_path is not a valid device or file${NC}"
                        read -p "Press Enter to continue..."
                        continue
                    fi
                    
                    read -p "Enter output directory [$HOME/recuperabit-recovery]: " output_dir
                    output_dir=${output_dir:-"$HOME/recuperabit-recovery"}
                    
                    echo
                    echo -e "${YELLOW}Starting interactive RecuperaBit session...${NC}"
                    echo "Commands you can use:"
                    echo "  Press Enter - Start/continue scanning"
                    echo "  info - Show found partitions"
                    echo "  recoverable <N> - List files in partition N"
                    echo "  restore <N> <id> - Restore specific file"
                    echo "  restore <N> all - Restore all files"
                    echo "  quit - Exit RecuperaBit"
                    echo
                    sleep 3
                    
                    cd "$HOME/RecuperaBit"
                    sudo "$SCRIPT_DIR/sudo-runner.sh" "$SCRIPT_DIR/run-recuperabit.sh" "$device_path" "$output_dir"
                    ;;
                3)
                    echo "=== RecuperaBit Recovery Instructions ==="
                    echo
                    echo "After scanning completes, RecuperaBit enters interactive mode."
                    echo
                    echo "Key commands:"
                    echo "1. Press Enter to start scanning (wait for completion)"
                    echo "2. 'info' - Shows found partitions with numbers"
                    echo "3. 'recoverable N' - Lists files in partition N"
                    echo "4. 'restore N all' - Recovers all files from partition N"
                    echo "5. 'restore N 1234' - Recovers specific file ID 1234"
                    echo "6. 'quit' - Exit the program"
                    echo
                    echo "The scan found 2,877 files in a 71.60 GB NTFS partition."
                    echo "Use 'recoverable 0' (or appropriate number) to see the file list."
                    echo
                    ;;
                4)
                    echo "Cancelled"
                    ;;
                *)
                    echo "Invalid option"
                    ;;
            esac
            ;;
        11)
            # Automated RecuperaBit with ticket tracking
            echo "=== Automated RecuperaBit Recovery ==="
            echo
            echo "This will automatically scan/load and recover all files"
            echo
            
            # Get ticket information
            read -p "Enter ticket number: " ticket_number
            if [ -z "$ticket_number" ]; then
                echo -e "${YELLOW}Cancelled - no ticket number${NC}"
                read -p "Press Enter to continue..."
                continue
            fi
            
            read -p "Enter client name: " client_name
            if [ -z "$client_name" ]; then
                echo -e "${YELLOW}Cancelled - no client name${NC}"
                read -p "Press Enter to continue..."
                continue
            fi
            
            # Show mounted images for easy selection
            echo
            echo "Available devices:"
            echo "1. Mounted images:"
            mount | grep "/mnt/recovery-" | awk '{print "   ", $1, "on", $3}'
            losetup -l 2>/dev/null | grep -v "NAME" | awk '{print "   ", $1, "->", $6}'
            echo "2. Physical devices:"
            lsblk -d -o NAME,SIZE,MODEL | grep -E "^(sd|nvme)" | sed 's/^/    /'
            echo
            
            read -p "Enter device path (e.g., /dev/loop0p2): " device_path
            if [ -z "$device_path" ]; then
                echo -e "${YELLOW}Cancelled - no device specified${NC}"
                read -p "Press Enter to continue..."
                continue
            fi
            
            # Validate device
            if [ ! -b "$device_path" ] && [ ! -f "$device_path" ]; then
                echo -e "${RED}Error: $device_path is not a valid device or file${NC}"
                read -p "Press Enter to continue..."
                continue
            fi
            
            read -p "Enter base output directory [$(get_real_home)/recuperabit-recovery]: " output_base
            output_base=${output_base:-"$(get_real_home)/recuperabit-recovery"}
            
            # Check for expect
            if ! command -v expect >/dev/null 2>&1; then
                echo -e "${YELLOW}Installing expect for automation...${NC}"
                sudo apt-get update && sudo apt-get install -y expect
            fi
            
            echo
            echo "Summary:"
            echo "  Ticket: $ticket_number"
            echo "  Client: $client_name" 
            echo "  Device: $device_path"
            echo "  Output: $output_base/${ticket_number}-${client_name}"
            echo
            read -p "Start automated recovery? [y/N]: " confirm
            
            if [[ "$confirm" =~ ^[Yy]$ ]]; then
                echo "Starting automated recovery..."
                echo "This will:"
                echo "  1. Check for existing save file"
                echo "  2. Scan if needed (10-30 min)"
                echo "  3. Save scan state for future use"
                echo "  4. Recover all files automatically"
                echo
                
                # Run automated script with sudo
                sudo "$SCRIPT_DIR/sudo-runner.sh" "$SCRIPT_DIR/recuperabit-automated.sh" \
                    "$ticket_number" "$client_name" "$device_path" "$output_base"
                    
                echo
                echo -e "${GREEN}Recovery process completed!${NC}"
                echo "Check output directory for recovered files."
            else
                echo "Cancelled"
            fi
            ;;
        [Aa])
            # Mac Volume Manager (APFS / HFS+)
            mac_volume_menu
            ;;
        [Ss])
            # Quick Status
            quick_status
            ;;
        [Tt])
            # DDRescue Targeted Recovery
            ddrescue_targeted_recovery
            ;;
        [Ff])
            # DDRescue Full Drive Clone
            ddrescue_full_clone
            ;;
        [Rr])
            # Resume Previous Recovery
            resume_ddrescue_session
            ;;
        [Vv])
            # Visualizer
            run_visualizer
            ;;
        0)
            echo "Exiting..."
            exit 0
            ;;
        *)
            echo "Invalid option"
            ;;
    esac

    case $option in
        1|2|3|4)
            echo
            echo "Current rules:"
            ls $UDEV_RULES_DIR/99-ddrescue-*.rules 2>/dev/null || echo "No ddrescue rules installed"
            echo
            ;;
    esac
    read -p "Press Enter to continue..."
    echo
done