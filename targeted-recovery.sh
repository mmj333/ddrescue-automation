#!/bin/bash
# targeted-recovery.sh - Intelligent Targeted Data Recovery Workflow
# Main entry point for automated ddrescue recovery with MFT-based targeting
#
# Usage: ./targeted-recovery.sh [options]
#   -j, --job-id       Job/ticket number (e.g., 12345)
#   -c, --customer     Customer name (e.g., ClientName)
#   -s, --source       Source device (e.g., /dev/sde)
#   -d, --dest         Destination device or image file
#   -l, --log          DDRescue log file path
#   -r, --resume       Resume existing job
#   -h, --help         Show this help
#
# Workflow:
#   1. Drive health assessment (SMART)
#   2. Filesystem analysis (NTFS parameters)
#   3. MFT region identification and targeted recovery
#   4. MFT parsing to find data cluster locations
#   5. Data domain generation
#   6. Targeted data recovery
#   7. Verification

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$SCRIPT_DIR/scripts"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

# Default values
PARTITION_OFFSET=2048  # sectors (1MB)
CLUSTER_SIZE=4096      # bytes

#######################################
# Logging functions
#######################################
log_info() { echo -e "${CYAN}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[OK]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

log_phase() {
    echo ""
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BLUE}  Phase $1: $2${NC}"
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
}

#######################################
# Show usage
#######################################
show_help() {
    head -25 "$0" | tail -22 | sed 's/^# //' | sed 's/^#//'
    exit 0
}

#######################################
# Parse command line arguments
#######################################
parse_args() {
    while [[ $# -gt 0 ]]; do
        case $1 in
            -j|--job-id) JOB_ID="$2"; shift 2 ;;
            -c|--customer) CUSTOMER="$2"; shift 2 ;;
            -s|--source) SOURCE="$2"; shift 2 ;;
            -d|--dest) DEST="$2"; shift 2 ;;
            -l|--log) LOG_FILE="$2"; shift 2 ;;
            -r|--resume) RESUME=1; shift ;;
            -h|--help) show_help ;;
            *) log_error "Unknown option: $1"; show_help ;;
        esac
    done
}

#######################################
# Interactive setup if args missing
#######################################
interactive_setup() {
    echo ""
    echo -e "${CYAN}╔══════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}║        TARGETED DATA RECOVERY - Job Setup                    ║${NC}"
    echo -e "${CYAN}╚══════════════════════════════════════════════════════════════╝${NC}"
    echo ""

    # Job ID
    if [ -z "$JOB_ID" ]; then
        read -p "Job/Ticket Number: " JOB_ID
    fi

    # Customer name
    if [ -z "$CUSTOMER" ]; then
        read -p "Customer Name: " CUSTOMER
    fi

    # Sanitize customer name for filenames
    CUSTOMER_SAFE=$(echo "$CUSTOMER" | tr ' ' '_' | tr -cd '[:alnum:]_-')
    JOB_NAME="${JOB_ID}${CUSTOMER_SAFE}"

    # Show available drives
    echo ""
    log_info "Scanning available drives..."
    echo ""
    lsblk -d -o NAME,SIZE,MODEL,TRAN,STATE 2>/dev/null | head -20
    echo ""

    # Source drive
    if [ -z "$SOURCE" ]; then
        read -p "Source device (failing drive, e.g., /dev/sde): " SOURCE
    fi

    # Destination
    if [ -z "$DEST" ]; then
        read -p "Destination device or image file: " DEST
    fi

    # Log file
    if [ -z "$LOG_FILE" ]; then
        LOG_FILE="$HOME/Desktop/${JOB_NAME}_DDRescue.log"
        log_info "Log file will be: $LOG_FILE"
    fi

    # Job directory
    JOB_DIR="$SCRIPT_DIR/${JOB_NAME}_analysis"
    mkdir -p "$JOB_DIR"

    # Save job config
    cat > "$JOB_DIR/job.conf" << EOF
# Job configuration - created $(date)
JOB_ID="$JOB_ID"
CUSTOMER="$CUSTOMER"
JOB_NAME="$JOB_NAME"
SOURCE="$SOURCE"
DEST="$DEST"
LOG_FILE="$LOG_FILE"
JOB_DIR="$JOB_DIR"
PARTITION_OFFSET=$PARTITION_OFFSET
CLUSTER_SIZE=$CLUSTER_SIZE
EOF

    log_success "Job config saved to $JOB_DIR/job.conf"
}

#######################################
# Load existing job
#######################################
load_job() {
    if [ -n "$RESUME" ]; then
        # Find most recent job or let user pick
        JOBS=$(find "$SCRIPT_DIR" -maxdepth 1 -name "*_analysis" -type d 2>/dev/null | head -10)
        if [ -z "$JOBS" ]; then
            log_error "No existing jobs found to resume"
            exit 1
        fi
        echo "Available jobs:"
        echo "$JOBS" | nl
        read -p "Select job number to resume: " JOB_NUM
        JOB_DIR=$(echo "$JOBS" | sed -n "${JOB_NUM}p")
        source "$JOB_DIR/job.conf"
        log_info "Resuming job: $JOB_NAME"
    fi
}

#######################################
# Phase 1: Drive Health Assessment
#######################################
phase_health_check() {
    log_phase "1" "Drive Health Assessment"

    if command -v smartctl &> /dev/null; then
        log_info "Running SMART health check on $SOURCE..."
        smartctl -H "$SOURCE" > "$JOB_DIR/smart_health.txt" 2>&1 || true
        smartctl -A "$SOURCE" > "$JOB_DIR/smart_attributes.txt" 2>&1 || true

        # Check for failures
        if grep -q "PASSED" "$JOB_DIR/smart_health.txt"; then
            log_success "SMART health: PASSED"
        else
            log_warn "SMART health: FAILING - drive has problems"
        fi

        # Show critical attributes
        echo ""
        log_info "Critical SMART attributes:"
        grep -E "(Reallocated|Pending|Uncorrectable|Current_Pending)" "$JOB_DIR/smart_attributes.txt" 2>/dev/null || echo "  (none found)"
    else
        log_warn "smartctl not installed - skipping SMART check"
    fi

    echo ""
    read -p "Continue with recovery? [Y/n]: " CONTINUE
    if [[ "$CONTINUE" =~ ^[Nn] ]]; then
        log_info "Aborted by user"
        exit 0
    fi
}

#######################################
# Phase 2: Filesystem Analysis
#######################################
phase_filesystem_analysis() {
    log_phase "2" "Filesystem Analysis"

    log_info "Analyzing NTFS filesystem structure..."

    # Try to get partition info
    if command -v fdisk &> /dev/null; then
        fdisk -l "$SOURCE" > "$JOB_DIR/partition_table.txt" 2>&1 || true
        log_info "Partition table saved to partition_table.txt"
    fi

    # Get NTFS info if partition is accessible
    if command -v ntfsinfo &> /dev/null; then
        # Try partition 1 first
        ntfsinfo -m "${SOURCE}1" > "$JOB_DIR/ntfs_info.txt" 2>&1 || \
        ntfsinfo -m "$SOURCE" > "$JOB_DIR/ntfs_info.txt" 2>&1 || true

        if [ -s "$JOB_DIR/ntfs_info.txt" ]; then
            # Extract MFT location
            MFT_LCN=$(grep "MFT Cluster Location" "$JOB_DIR/ntfs_info.txt" | awk '{print $NF}')
            CLUSTER_SIZE=$(grep "Cluster Size" "$JOB_DIR/ntfs_info.txt" | awk '{print $NF}')

            if [ -n "$MFT_LCN" ] && [ -n "$CLUSTER_SIZE" ]; then
                MFT_BYTE_OFFSET=$((MFT_LCN * CLUSTER_SIZE + PARTITION_OFFSET * 512))
                MFT_GB=$(echo "scale=2; $MFT_BYTE_OFFSET / 1073741824" | bc)

                log_success "NTFS parameters detected:"
                echo "  MFT Cluster:  $MFT_LCN"
                echo "  Cluster Size: $CLUSTER_SIZE bytes"
                echo "  MFT Offset:   $MFT_BYTE_OFFSET bytes (~${MFT_GB} GB)"

                # Save for later phases
                echo "MFT_LCN=$MFT_LCN" >> "$JOB_DIR/job.conf"
                echo "MFT_BYTE_OFFSET=$MFT_BYTE_OFFSET" >> "$JOB_DIR/job.conf"
            fi
        fi
    fi

    # Use fsstat as backup
    if command -v fsstat &> /dev/null; then
        fsstat -o "$PARTITION_OFFSET" "$SOURCE" > "$JOB_DIR/fsstat.txt" 2>&1 || true
        log_info "Filesystem stats saved to fsstat.txt"
    fi
}

#######################################
# Phase 3: MFT Recovery
#######################################
phase_mft_recovery() {
    log_phase "3" "MFT Region Recovery"

    # Load MFT location from config
    source "$JOB_DIR/job.conf"

    if [ -z "$MFT_BYTE_OFFSET" ]; then
        log_warn "MFT location not detected automatically"
        read -p "Enter MFT byte offset (or press Enter for default 3GB): " MFT_BYTE_OFFSET
        MFT_BYTE_OFFSET=${MFT_BYTE_OFFSET:-3221225472}
    fi

    # Estimate MFT size (usually 128MB for typical drives, up to 256MB for large)
    MFT_SIZE=${MFT_SIZE:-134217728}  # 128MB default

    log_info "Creating MFT domain file..."
    log_info "  MFT Start: $(printf '0x%X' $MFT_BYTE_OFFSET) ($MFT_BYTE_OFFSET bytes)"
    log_info "  MFT Size:  $(printf '0x%X' $MFT_SIZE) ($((MFT_SIZE / 1048576)) MB)"

    # Create domain file
    cat > "$JOB_DIR/mft_domain.txt" << EOF
# Mapfile. Created by GNU ddrescue version 1.23
# Domain file for MFT recovery - $JOB_NAME
# MFT region: $(printf '0x%X' $MFT_BYTE_OFFSET), size $(printf '0x%X' $MFT_SIZE)
# current_pos  current_status  current_pass
$(printf '0x%X' $MFT_BYTE_OFFSET)     +               1
#      pos        size  status
$(printf '0x%X' $MFT_BYTE_OFFSET)  $(printf '0x%X' $MFT_SIZE)  +
EOF

    log_success "MFT domain file created: $JOB_DIR/mft_domain.txt"
    echo ""

    log_info "Running ddrescue for MFT region..."
    echo ""
    echo "Command: ddrescue -f -d -m $JOB_DIR/mft_domain.txt $SOURCE $DEST $LOG_FILE"
    echo ""
    read -p "Start MFT recovery? [Y/n]: " START
    if [[ ! "$START" =~ ^[Nn] ]]; then
        ddrescue -f -d -m "$JOB_DIR/mft_domain.txt" "$SOURCE" "$DEST" "$LOG_FILE"
        log_success "MFT recovery pass complete"
    fi
}

#######################################
# Phase 4: MFT Analysis
#######################################
phase_mft_analysis() {
    log_phase "4" "MFT Analysis - Finding Data Clusters"

    source "$JOB_DIR/job.conf"

    # Determine which device to analyze (dest drive or image)
    ANALYZE_DEV="$DEST"

    log_info "Analyzing MFT on $ANALYZE_DEV..."

    # Run the cluster analysis script
    if [ -x "$SCRIPTS_DIR/analyze-mft-clusters.sh" ]; then
        "$SCRIPTS_DIR/analyze-mft-clusters.sh" "$ANALYZE_DEV" "$PARTITION_OFFSET" "$JOB_DIR/mft_parsed"
    else
        # Inline analysis if script not available
        log_info "Running inline MFT analysis..."

        mkdir -p "$JOB_DIR/mft_parsed"

        # List all files
        fls -r -p -o "$PARTITION_OFFSET" "$ANALYZE_DEV" > "$JOB_DIR/mft_parsed/file_list.txt" 2>&1
        FILE_COUNT=$(wc -l < "$JOB_DIR/mft_parsed/file_list.txt")
        log_info "Found $FILE_COUNT MFT entries"

        # Filter active files
        grep -E "^r/r" "$JOB_DIR/mft_parsed/file_list.txt" > "$JOB_DIR/mft_parsed/active_files.txt" || true
        ACTIVE_COUNT=$(wc -l < "$JOB_DIR/mft_parsed/active_files.txt")
        log_success "Active files: $ACTIVE_COUNT"
    fi
}

#######################################
# Phase 5: Generate Data Domain
#######################################
phase_generate_domain() {
    log_phase "5" "Generating Data Recovery Domain"

    source "$JOB_DIR/job.conf"

    log_info "Creating ddrescue domain for actual data clusters..."

    # This would process the cluster list from phase 4 and create a domain file
    # For now, placeholder
    log_warn "Data domain generation not yet implemented"
    log_info "Use the cluster list from $JOB_DIR/mft_parsed/ to create domain"
}

#######################################
# Phase 6: Data Recovery
#######################################
phase_data_recovery() {
    log_phase "6" "Targeted Data Recovery"

    source "$JOB_DIR/job.conf"

    if [ -f "$JOB_DIR/data_domain.txt" ]; then
        log_info "Running ddrescue with data domain..."
        ddrescue -f -d -m "$JOB_DIR/data_domain.txt" "$SOURCE" "$DEST" "$LOG_FILE"
        log_success "Data recovery pass complete"
    else
        log_warn "No data domain file found - run phase 5 first"
    fi
}

#######################################
# Show status
#######################################
show_status() {
    source "$JOB_DIR/job.conf"

    echo ""
    echo -e "${CYAN}╔══════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}║  Job Status: $JOB_NAME${NC}"
    echo -e "${CYAN}╚══════════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo "Source:      $SOURCE"
    echo "Destination: $DEST"
    echo "Log:         $LOG_FILE"
    echo "Job Dir:     $JOB_DIR"
    echo ""

    # Check which phases have been completed
    [ -f "$JOB_DIR/smart_health.txt" ] && echo "✓ Phase 1: Health check complete"
    [ -f "$JOB_DIR/ntfs_info.txt" ] && echo "✓ Phase 2: Filesystem analysis complete"
    [ -f "$JOB_DIR/mft_domain.txt" ] && echo "✓ Phase 3: MFT domain created"
    [ -d "$JOB_DIR/mft_parsed" ] && echo "✓ Phase 4: MFT analysis complete"
    [ -f "$JOB_DIR/data_domain.txt" ] && echo "✓ Phase 5: Data domain generated"
    echo ""
}

#######################################
# Main menu
#######################################
main_menu() {
    while true; do
        show_status

        echo "Select action:"
        echo "  1) Run all phases (full workflow)"
        echo "  2) Phase 1: Health check"
        echo "  3) Phase 2: Filesystem analysis"
        echo "  4) Phase 3: MFT recovery"
        echo "  5) Phase 4: MFT analysis"
        echo "  6) Phase 5: Generate data domain"
        echo "  7) Phase 6: Data recovery"
        echo "  8) Analyze ddrescue log"
        echo "  q) Quit"
        echo ""
        read -p "Choice: " CHOICE

        case $CHOICE in
            1)
                phase_health_check
                phase_filesystem_analysis
                phase_mft_recovery
                phase_mft_analysis
                phase_generate_domain
                phase_data_recovery
                ;;
            2) phase_health_check ;;
            3) phase_filesystem_analysis ;;
            4) phase_mft_recovery ;;
            5) phase_mft_analysis ;;
            6) phase_generate_domain ;;
            7) phase_data_recovery ;;
            8)
                if [ -f "$LOG_FILE" ]; then
                    python3 "$SCRIPT_DIR/analyze_recovery.py" "$LOG_FILE"
                else
                    log_warn "No log file found at $LOG_FILE"
                fi
                ;;
            q|Q) exit 0 ;;
            *) log_warn "Invalid choice" ;;
        esac

        echo ""
        read -p "Press Enter to continue..."
    done
}

#######################################
# Main
#######################################
main() {
    echo ""
    echo -e "${GREEN}╔══════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║         TARGETED DATA RECOVERY SYSTEM v1.0                   ║${NC}"
    echo -e "${GREEN}║         MFT-Based Intelligent Recovery                       ║${NC}"
    echo -e "${GREEN}╚══════════════════════════════════════════════════════════════╝${NC}"
    echo ""

    # Check for root
    if [ "$EUID" -ne 0 ]; then
        log_warn "Not running as root - some features may not work"
        log_info "Consider: sudo $0 $*"
        echo ""
    fi

    parse_args "$@"
    load_job

    if [ -z "$JOB_DIR" ]; then
        interactive_setup
    fi

    main_menu
}

main "$@"
