#!/bin/bash
# RecuperaBit Best Practices and Common Issues

cat << 'EOF'
=== RecuperaBit Best Practices ===

1. PATH HANDLING
   Problem: RecuperaBit creates weird paths with quotes
   Solution: 
   - Use absolute paths without quotes
   - Run from RecuperaBit directory
   - Expect output in: output_dir/PartitionXXX/Root/

2. FINDING THE RIGHT PARTITION
   Problem: Multiple partitions, which is the real one?
   Solution:
   - Partition with 10k-50k files is usually main Windows
   - Use 'tree <part#>' to check for /Users folder
   - Avoid partitions with >100k files (deleted files)

3. RESTORE ALL FILES
   Problem: No "restore all" command
   Solution:
   - Use 'tree <part#>' to find root ID (usually 5)
   - Then: restore <part#> <root_id>

4. COMMON COMMANDS
   info                    - Show all partitions
   tree <part#>           - Show directory structure
   restore <part#> <id>   - Restore recursively from ID
   save <name>            - Save scan state
   load <name>            - Load saved scan

5. AUTOMATION TIPS
   - Save scans with ticket numbers: save ticket-12345
   - Use expect scripts for automation
   - Always run from your RecuperaBit checkout directory ($RECUPERABIT_DIR, default $HOME/RecuperaBit)

6. PARTITION SELECTION GUIDE
   Good indicators:
   - Has /Users or /Documents and Settings
   - 10,000-50,000 files
   - Shows Windows, Program Files in tree
   
   Bad indicators:
   - >100,000 files (includes deleted)
   - Mostly cache/temp files
   - No recognizable Windows structure

7. OUTPUT ORGANIZATION
   RecuperaBit creates:
   output_dir/
   └── Partition<number>/
       └── Root/
           ├── Users/
           ├── Windows/
           └── Program Files/

8. MOVING RECOVERED FILES
   After recovery, move files to final location:
   mv "/path/to/output/Partition284/Root/"* "/final/destination/"

9. HANDLING ERRORS
   "Cannot restore $DATA attribute" - Normal for damaged files
   "The index is not valid" - Wrong syntax or partition number

10. QUICK RECOVERY WORKFLOW
    cd "$RECUPERABIT_DIR"   # default $HOME/RecuperaBit
    python3 main.py /dev/loop0p2 -o /recovery/output
    [Enter] to scan
    info
    tree <best_partition>
    restore <best_partition> <root_id>
    quit

EOF

echo
echo "For automated recovery, use:"
echo "  ./recuperabit-restore-all.exp /dev/loop0p2 /output/dir [partition#]"
echo
echo "For manual recovery with fixed paths:"
echo "  ./recuperabit-runner-fixed.sh /dev/loop0p2 /output/dir"