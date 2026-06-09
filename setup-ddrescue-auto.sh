#!/bin/bash

# Setup script for ddrescue auto-run
# This script helps configure automatic ddrescue execution for failing drives

UDEV_RULES_DIR="/etc/udev/rules.d"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== DDRescue Auto-Run Setup ==="
echo

while true; do
    echo "Options:"
    echo "1. Set up new recovery job"
    echo "2. Resume existing recovery (point to existing image/log)"
    echo "3. Refresh drive list"
    echo "4. Recovery Manager (manage rules, mount images, file recovery)"
    echo "5. Exit"
    echo
    read -p "Select option [1-5]: " OPTION

    case $OPTION in
        1|2)
            # List connected drives
            echo
            echo "Available drives:"
            lsblk -d -o NAME,SIZE,MODEL,SERIAL | grep -E "^(sd|nvme)" || lsblk -d -o NAME,SIZE,MODEL | grep -E "^(sd|nvme)"
            echo
            
            # Get drive selection
            read -p "Enter the device name (e.g., sdb, sdc, nvme0n1) or 'r' to refresh: " DEVICE_NAME
            
            if [ "$DEVICE_NAME" = "r" ]; then
                continue
            fi
            
            DEVICE_PATH="/dev/$DEVICE_NAME"
            
            if [ ! -b "$DEVICE_PATH" ]; then
                echo "Error: Device $DEVICE_PATH not found"
                continue
            fi
            
            # Get drive info
            echo "Getting drive information..."
            MODEL=$(udevadm info -a -n "$DEVICE_PATH" 2>/dev/null | grep -m1 'ATTRS{model}' | cut -d'"' -f2)
            SERIAL=$(udevadm info -a -n "$DEVICE_PATH" 2>/dev/null | grep -m1 'ATTRS{serial}' | cut -d'"' -f2 | head -1)
            
            echo "Drive model: $MODEL"
            if [ -z "$SERIAL" ]; then
                echo "Drive serial: (none found - will use model-based matching)"
                echo "Note: Rule will match ANY drive with model '$MODEL'"
            else
                echo "Drive serial: $SERIAL"
            fi
            echo
            
            # Check for existing rules for this device
            echo "Checking for existing rules for this device..."
            local existing_rules=""
            for rule in $UDEV_RULES_DIR/99-ddrescue-*.rules; do
                if [ -f "$rule" ]; then
                    if [ -n "$SERIAL" ] && grep -q "ATTRS{serial}==\"$SERIAL\"" "$rule"; then
                        existing_rules="$existing_rules$(basename "$rule")\n"
                    elif [ -n "$MODEL" ] && grep -q "ATTRS{model}==\"$MODEL\"" "$rule"; then
                        existing_rules="$existing_rules$(basename "$rule")\n"
                    fi
                fi
            done
            
            if [ -n "$existing_rules" ]; then
                echo
                echo "WARNING: Found existing rule(s) for this device:"
                echo -e "$existing_rules"
                echo "Having multiple rules for the same device can cause conflicts!"
                echo
                echo "Options:"
                echo "1. Cancel setup (recommended - use manage script to remove old rule first)"
                echo "2. Continue anyway (not recommended)"
                read -p "Select option [1-2]: " conflict_choice
                
                if [ "$conflict_choice" != "2" ]; then
                    echo "Setup cancelled. Please remove existing rules first using:"
                    echo "./manage-ddrescue-rules.sh"
                    continue
                fi
                echo "Continuing despite existing rules..."
            fi
            echo
            
            if [ "$OPTION" = "1" ]; then
                # New recovery setup
                read -p "Enter destination directory for image/log files: " DEST_DIR
                
                # Trim quotes and spaces from the path
                DEST_DIR=$(echo "$DEST_DIR" | sed -e "s/^['\"]*//" -e "s/['\"]* *$//" -e "s/^ *//" -e "s/ *$//")
                
                # Create directory if it doesn't exist
                if [ ! -d "$DEST_DIR" ]; then
                    read -p "Directory doesn't exist. Create it? [y/N]: " CREATE_DIR
                    if [[ "$CREATE_DIR" =~ ^[Yy]$ ]]; then
                        if ! mkdir -p "$DEST_DIR"; then
                            echo "Error: Failed to create directory"
                            continue
                        fi
                    else
                        echo "Setup cancelled"
                        continue
                    fi
                fi
                
                read -p "Enter a name for this recovery job (e.g., ticket-client): " JOB_NAME
                
                if [ -z "$JOB_NAME" ]; then
                    echo "Error: Job name cannot be empty"
                    continue
                fi
                
                # Sanitize job name for use in filenames
                JOB_NAME=$(echo "$JOB_NAME" | sed 's/[^a-zA-Z0-9._-]/_/g')
                
                DESTINATION_IMAGE="$DEST_DIR/${JOB_NAME}.img"
                DESTINATION_LOG="$DEST_DIR/${JOB_NAME}.log"
            else
                # Resume existing recovery
                read -p "Enter path to existing image file: " DESTINATION_IMAGE
                
                # Trim quotes and spaces from the path
                DESTINATION_IMAGE=$(echo "$DESTINATION_IMAGE" | sed -e "s/^['\"]*//" -e "s/['\"]* *$//" -e "s/^ *//" -e "s/ *$//")
                
                if [ ! -f "$DESTINATION_IMAGE" ]; then
                    echo "Error: Image file not found: $DESTINATION_IMAGE"
                    continue
                fi
                
                read -p "Enter path to existing log file: " DESTINATION_LOG
                
                # Trim quotes and spaces from the log path too
                DESTINATION_LOG=$(echo "$DESTINATION_LOG" | sed -e "s/^['\"]*//" -e "s/['\"]* *$//" -e "s/^ *//" -e "s/ *$//")
                
                if [ ! -f "$DESTINATION_LOG" ]; then
                    echo "Warning: Log file not found. A new one will be created if needed."
                fi
                
                # Extract job name from image filename
                JOB_NAME=$(basename "$DESTINATION_IMAGE" .img)
                # Sanitize job name
                JOB_NAME=$(echo "$JOB_NAME" | sed 's/[^a-zA-Z0-9._-]/_/g')
                DEST_DIR=$(dirname "$DESTINATION_IMAGE")
            fi
            
            # Create job-specific config
            CONFIG_FILE="ddrescue-${JOB_NAME}.conf"
            SCRIPT_FILE="ddrescue-${JOB_NAME}.sh"
            # Get absolute path of script directory
            SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
            FULL_SCRIPT_PATH="$SCRIPT_DIR/$SCRIPT_FILE"
            
            # Check if config already exists
            if [ -f "$CONFIG_FILE" ]; then
                echo
                echo "Warning: Configuration files already exist for job '$JOB_NAME':"
                echo "  - $CONFIG_FILE"
                [ -f "$SCRIPT_FILE" ] && echo "  - $SCRIPT_FILE"
                echo
                read -p "Overwrite these configuration files? [y/N]: " OVERWRITE
                if [[ ! "$OVERWRITE" =~ ^[Yy]$ ]]; then
                    echo "Setup cancelled - existing configuration preserved"
                    continue
                fi
            fi
            
            # Generate configuration
            cat > "$CONFIG_FILE" << EOF
# DDRescue Configuration for $JOB_NAME
# Generated on $(date)
# Drive: $MODEL (Serial: $SERIAL)

# Source device
SOURCE_DEVICE="$DEVICE_PATH"

# Destination files
DESTINATION_IMAGE="$DESTINATION_IMAGE"
DESTINATION_LOG="$DESTINATION_LOG"

# DDRescue options
# -A: Disable read-ahead
# -M: Retrim on errors
# -d: Direct disk access
# -v: Verbose
# -r -1: Infinite retries
DDRESCUE_OPTIONS="-AMdv -r -1"

# Log directory
LOG_DIR="$DEST_DIR"

# Optional: Add custom options below
# DDRESCUE_OPTIONS="-AMdv -r 3 -c 256"  # Example with 3 retries and 256 sector clusters
EOF

            # Generate job-specific script
            cat > "$SCRIPT_FILE" << EOF
#!/bin/bash
# Auto-generated ddrescue runner script
SCRIPT_DIR="\$(cd "\$(dirname "\$0")" && pwd)"
CONFIG_FILE="\$SCRIPT_DIR/$CONFIG_FILE"

if [ -f "\$CONFIG_FILE" ]; then
    source "\$CONFIG_FILE"
else
    echo "Configuration file not found: \$CONFIG_FILE"
    exit 1
fi

if [ ! -b "\$SOURCE_DEVICE" ]; then
    echo "Device \$SOURCE_DEVICE not found"
    exit 1
fi

mkdir -p "\$LOG_DIR"

# Note: This script may need passwordless sudo or to be run as root
# when triggered by udev/systemd-run

echo "\$(date): Starting ddrescue for \$SOURCE_DEVICE" >> "\$LOG_DIR/ddrescue-auto.log"
echo "\$(date): Running as user: \$(whoami) (UID: \$UID)" >> "\$LOG_DIR/ddrescue-auto.log"

# Log the full command being executed
echo "\$(date): Executing command: ddrescue \$DDRESCUE_OPTIONS \$SOURCE_DEVICE \$DESTINATION_IMAGE \$DESTINATION_LOG" >> "\$LOG_DIR/ddrescue-auto.log"

# Try to run ddrescue, use sudo if needed and available
if [ "\$UID" -ne 0 ]; then
    if command -v sudo >/dev/null 2>&1; then
        sudo ddrescue \$DDRESCUE_OPTIONS "\$SOURCE_DEVICE" "\$DESTINATION_IMAGE" "\$DESTINATION_LOG" 2>&1 | tee -a "\$LOG_DIR/ddrescue-auto.log"
        EXIT_CODE=\${PIPESTATUS[0]}
    else
        echo "Error: Not running as root and sudo not available" | tee -a "\$LOG_DIR/ddrescue-auto.log"
        exit 1
    fi
else
    ddrescue \$DDRESCUE_OPTIONS "\$SOURCE_DEVICE" "\$DESTINATION_IMAGE" "\$DESTINATION_LOG" 2>&1 | tee -a "\$LOG_DIR/ddrescue-auto.log"
    EXIT_CODE=\${PIPESTATUS[0]}
fi

echo "\$(date): ddrescue completed with exit code \$EXIT_CODE" >> "\$LOG_DIR/ddrescue-auto.log"
EOF

            chmod +x "$SCRIPT_FILE"
            
            # Generate udev rule
            RULE_FILE="$UDEV_RULES_DIR/99-ddrescue-${JOB_NAME}.rules"
            
            # Check if udev rule already exists
            if [ -f "$RULE_FILE" ]; then
                echo
                echo "Warning: Udev rule already exists for job '$JOB_NAME'"
                echo "Location: $RULE_FILE"
                echo
                read -p "Replace existing udev rule? [y/N]: " REPLACE_RULE
                if [[ ! "$REPLACE_RULE" =~ ^[Yy]$ ]]; then
                    echo "Keeping existing udev rule"
                    echo
                    echo "=== Setup Complete ==="
                    echo "1. Configuration: Edit $CONFIG_FILE to modify ddrescue options"
                    echo "2. Test manually: ./$SCRIPT_FILE"
                    echo "3. The script will run automatically when the drive is connected"
                    echo "4. Monitor progress: tail -f \"$DEST_DIR/ddrescue-auto.log\""
                    echo
                    read -p "Press Enter to continue..."
                    echo
                    continue
                fi
            fi
            
            RULE_CONTENT=""
            
            if [ -n "$SERIAL" ]; then
                RULE_CONTENT="# Auto-run ddrescue for $JOB_NAME
KERNEL==\"sd?\", ATTRS{serial}==\"$SERIAL\", ACTION==\"add\", RUN+=\"/usr/bin/systemd-run --uid=$USER --property=SuccessExitStatus='1 2 3' '$FULL_SCRIPT_PATH'\"
KERNEL==\"nvme?n?\", ATTRS{serial}==\"$SERIAL\", ACTION==\"add\", RUN+=\"/usr/bin/systemd-run --uid=$USER --property=SuccessExitStatus='1 2 3' '$FULL_SCRIPT_PATH'\""
            else
                RULE_CONTENT="# Auto-run ddrescue for $JOB_NAME  
KERNEL==\"sd?\", ATTRS{model}==\"$MODEL\", ACTION==\"add\", RUN+=\"/usr/bin/systemd-run --uid=$USER --property=SuccessExitStatus='1 2 3' '$FULL_SCRIPT_PATH'\"
KERNEL==\"nvme?n?\", ATTRS{model}==\"$MODEL\", ACTION==\"add\", RUN+=\"/usr/bin/systemd-run --uid=$USER --property=SuccessExitStatus='1 2 3' '$FULL_SCRIPT_PATH'\""
            fi
            
            echo "=== Summary ==="
            echo "Configuration file: $CONFIG_FILE"
            echo "Script file: $SCRIPT_FILE"
            echo "Udev rule file: $RULE_FILE"
            echo
            echo "=== Udev Rule Content ==="
            echo "$RULE_CONTENT"
            echo
            
            read -p "Install udev rule? (requires sudo) [y/N]: " INSTALL
            if [[ "$INSTALL" =~ ^[Yy]$ ]]; then
                echo "$RULE_CONTENT" | sudo tee "$RULE_FILE" > /dev/null
                sudo udevadm control --reload-rules
                echo "Udev rule installed and reloaded"
                
                # Check if we need to set up passwordless sudo
                echo
                echo "Checking sudo configuration..."
                if ! sudo -n ddrescue --version >/dev/null 2>&1; then
                    echo "ddrescue requires sudo password. Would you like to configure passwordless sudo?"
                    echo "This will add: $USER ALL=(ALL) NOPASSWD: /usr/bin/ddrescue"
                    read -p "Configure passwordless sudo for ddrescue? [y/N]: " SETUP_SUDO
                    
                    if [[ "$SETUP_SUDO" =~ ^[Yy]$ ]]; then
                        SUDOERS_FILE="/etc/sudoers.d/ddrescue-auto"
                        DDRESCUE_PATH=$(which ddrescue)
                        if [ -z "$DDRESCUE_PATH" ]; then
                            echo "Error: ddrescue not found in PATH"
                            continue
                        fi
                        echo "# Allow $USER to run ddrescue without password" | sudo tee "$SUDOERS_FILE" > /dev/null
                        echo "$USER ALL=(ALL) NOPASSWD: $DDRESCUE_PATH" | sudo tee -a "$SUDOERS_FILE" > /dev/null
                        sudo chmod 440 "$SUDOERS_FILE"
                        
                        # Verify it works
                        if sudo -n ddrescue --version >/dev/null 2>&1; then
                            echo "Passwordless sudo configured successfully for ddrescue"
                        else
                            echo "Warning: Passwordless sudo configuration may have failed"
                        fi
                    fi
                else
                    echo "Passwordless sudo already configured for ddrescue"
                fi
            else
                echo "Udev rule not installed. You can manually create it at: $RULE_FILE"
            fi
            
            echo
            echo "=== Setup Complete ==="
            echo "1. Configuration: Edit $CONFIG_FILE to modify ddrescue options"
            echo "2. Test manually: ./$SCRIPT_FILE"
            echo "3. The script will run automatically when the drive is connected"
            echo "4. Monitor progress: tail -f \"$DEST_DIR/ddrescue-auto.log\""
            echo
            
            read -p "Press Enter to continue..."
            echo
            ;;
            
        3)
            # Refresh - loop will show the menu again
            echo "Refreshing..."
            echo
            continue
            ;;
            
        4)
            # Launch recovery manager
            MANAGE_SCRIPT="$SCRIPT_DIR/recovery-manager.sh"
            if [ -f "$MANAGE_SCRIPT" ]; then
                echo "Launching Recovery Manager..."
                "$MANAGE_SCRIPT"
                echo
                echo "Returned to setup script"
                echo
            else
                # Try old name for backward compatibility
                MANAGE_SCRIPT="$SCRIPT_DIR/manage-ddrescue-rules.sh"
                if [ -f "$MANAGE_SCRIPT" ]; then
                    echo "Launching management script..."
                    "$MANAGE_SCRIPT"
                    echo
                else
                    echo "Error: Recovery manager not found"
                    echo
                fi
            fi
            ;;
            
        5)
            echo "Exiting..."
            exit 0
            ;;
            
        *)
            echo "Invalid option"
            echo
            ;;
    esac
done