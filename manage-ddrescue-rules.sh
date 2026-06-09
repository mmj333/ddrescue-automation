#!/bin/bash

# Management script for ddrescue udev rules

UDEV_RULES_DIR="/etc/udev/rules.d"
SUDOERS_FILE="/etc/sudoers.d/ddrescue-auto"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

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
        echo "Found rule: $rule_file"
        cat "$rule_file"
        echo
        read -p "Remove this rule? [y/N]: " confirm
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

# Main menu loop
while true; do
    echo "=== DDRescue Rules Manager ==="
    echo
    echo "1. List all ddrescue rules"
    echo "2. Remove a specific rule"
    echo "3. Remove ALL ddrescue rules"
    echo "4. Reload udev rules"
    echo "5. Manage sudo permissions"
    echo "6. Monitor ddrescue output (tail log)"
    echo "7. Exit"
    echo

    read -p "Select option [1-7]: " option

    case $option in
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
            echo "Exiting..."
            exit 0
            ;;
        *)
            echo "Invalid option"
            ;;
    esac

    echo
    echo "Current rules:"
    ls $UDEV_RULES_DIR/99-ddrescue-*.rules 2>/dev/null || echo "No ddrescue rules installed"
    echo
    read -p "Press Enter to continue..."
    echo
done