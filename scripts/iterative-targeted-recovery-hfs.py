#!/usr/bin/env python3
"""
iterative-targeted-recovery-hfs.py - Bootstrapped HFS+ Targeted Recovery

This script implements an iterative recovery workflow for HFS+ filesystems:
1. Recover Volume Header → parse → get block size and allocation file info
2. Recover Allocation File (bitmap)
3. Parse bitmap → get all allocated blocks
4. Recover all allocated data blocks
5. Aggressive retry on failed regions

Stage 0 (critical structures) runs LAST by default to prioritize actual file data,
but ensures destination is immediately mountable when complete.

Use --bootable-first (-b) to run Stage 0 BEFORE data recovery.

Usage: sudo python3 iterative-targeted-recovery-hfs.py [--bootable-first] <source> <dest> <log> [job_dir]
"""

import sys
import os
import subprocess
import struct
import json
import select
import time
import re
import bisect
import argparse
from pathlib import Path

# Default timeout for prompts (in seconds)
DEFAULT_PROMPT_TIMEOUT = 300  # 5 minutes

class HFSTargetedRecovery:
    def __init__(self, source, dest, log_file, job_dir, bootable_first=False):
        self.source = source
        self.dest = dest
        self.log_file = log_file
        self.job_dir = Path(job_dir)
        self.job_dir.mkdir(parents=True, exist_ok=True)
        self.bootable_first = bootable_first

        # Will be populated as we discover them
        self.sector_size = 512
        self.block_size = None
        self.total_blocks = None
        self.free_blocks = None
        self.partition_offset = None  # bytes
        self.hfs_partition = None  # Source HFS+ partition (e.g., /dev/sdb2)
        self.allocated_clusters = []

        # State file to track progress
        self.state_file = self.job_dir / "recovery_state.json"
        self.state = self.load_state()

        # Get drive size
        self.drive_size = self._detect_drive_size()

        # Detect destination size
        self.dest_size = self._detect_dest_size()

    def _detect_drive_size(self):
        """Get the size of the source drive in bytes"""
        try:
            cmd = f"blockdev --getsize64 {self.source}"
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            if result.returncode == 0:
                size = int(result.stdout.strip())
                print(f"Detected source drive size: {size/(1024**3):.2f} GB ({size:,} bytes)")
                return size
        except:
            pass

        try:
            device = os.path.basename(self.source)
            with open(f"/sys/block/{device}/size") as f:
                sectors = int(f.read().strip())
                size = sectors * 512
                print(f"Detected source drive size: {size/(1024**3):.2f} GB ({size:,} bytes)")
                return size
        except:
            pass

        print("WARNING: Could not detect drive size, assuming 1TB")
        return 1000 * 1000 * 1000 * 1000

    def _detect_dest_size(self):
        """Get the size of the destination drive/file in bytes"""
        try:
            cmd = f"blockdev --getsize64 {self.dest}"
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            if result.returncode == 0:
                size = int(result.stdout.strip())
                print(f"Detected destination size: {size/(1024**3):.2f} GB ({size:,} bytes)")
                return size
        except:
            pass

        try:
            if os.path.isfile(self.dest):
                size = os.path.getsize(self.dest)
                print(f"Detected destination file size: {size/(1024**3):.2f} GB")
                return size
        except:
            pass

        print("WARNING: Could not detect destination size")
        return None

    def _get_drive_identity(self, device):
        """Get identifying information for a drive"""
        identity = {
            "path": device,
            "serial": None,
            "model": None,
            "size": None
        }

        try:
            cmd = f"udevadm info --query=property --name={device} 2>/dev/null"
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            for line in result.stdout.split('\n'):
                if line.startswith('ID_SERIAL_SHORT='):
                    identity["serial"] = line.split('=', 1)[1].strip()
                elif line.startswith('ID_SERIAL=') and not identity["serial"]:
                    identity["serial"] = line.split('=', 1)[1].strip()
                elif line.startswith('ID_MODEL='):
                    identity["model"] = line.split('=', 1)[1].strip()
        except:
            pass

        try:
            cmd = f"blockdev --getsize64 {device}"
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            if result.returncode == 0:
                identity["size"] = int(result.stdout.strip())
        except:
            pass

        return identity

    def _validate_drive_identity(self, device, saved_identity, drive_type):
        """Validate that a drive matches saved identity from previous run"""
        current = self._get_drive_identity(device)

        print(f"\nValidating {drive_type} drive identity...")
        print(f"  Current:  {device}")
        if current["model"]:
            print(f"            Model: {current['model']}")
        if current["serial"]:
            print(f"            Serial: {current['serial']}")
        if current["size"]:
            print(f"            Size: {current['size']/(1024**3):.1f} GB")

        print(f"  Expected: {saved_identity.get('path', 'unknown')}")
        if saved_identity.get("model"):
            print(f"            Model: {saved_identity['model']}")
        if saved_identity.get("serial"):
            print(f"            Serial: {saved_identity['serial']}")
        if saved_identity.get("size"):
            print(f"            Size: {saved_identity['size']/(1024**3):.1f} GB")

        mismatches = []

        if saved_identity.get("serial") and current["serial"]:
            if saved_identity["serial"] != current["serial"]:
                mismatches.append(f"Serial mismatch: {current['serial']} vs {saved_identity['serial']}")

        if saved_identity.get("model") and current["model"]:
            if saved_identity["model"] != current["model"]:
                mismatches.append(f"Model mismatch: {current['model']} vs {saved_identity['model']}")

        if saved_identity.get("size") and current["size"]:
            size_diff = abs(saved_identity["size"] - current["size"])
            if size_diff > 1024 * 1024 * 1024:
                mismatches.append(f"Size mismatch: {current['size']/(1024**3):.1f}GB vs {saved_identity['size']/(1024**3):.1f}GB")

        if not mismatches:
            print(f"  ✓ {drive_type} drive identity verified")
            return True

        print(f"\n  ⚠️  WARNING: {drive_type} DRIVE MISMATCH DETECTED!")
        for m in mismatches:
            print(f"      - {m}")

        if drive_type == "DESTINATION":
            print("\n  ⚠️  DANGER: Writing to wrong destination could destroy data!")

        options = {
            'a': "Abort (recommended)",
            'c': f"Continue anyway (I verified this is the correct {drive_type.lower()})"
        }

        choice = self.prompt_with_timeout(
            f"{drive_type} drive appears different from previous run. What would you like to do?",
            options,
            'a',
            timeout=60
        )

        if choice == 'c':
            print(f"  User confirmed: proceeding with current {drive_type.lower()} drive")
            return True
        else:
            print(f"  Aborting due to {drive_type.lower()} drive mismatch")
            return False

    def load_state(self):
        """Load recovery state from previous run"""
        if self.state_file.exists():
            with open(self.state_file) as f:
                return json.load(f)
        return {"stage": 0, "completed": []}

    def save_state(self):
        """Save recovery state"""
        with open(self.state_file, 'w') as f:
            json.dump(self.state, f, indent=2)

    def play_notification_sound(self):
        """Play a notification sound to alert user"""
        sudo_user = os.environ.get('SUDO_USER') or os.environ.get('USER') or os.environ.get('LOGNAME') or ''
        uid = os.environ.get('SUDO_UID', '1000')
        sound_file = "/usr/share/sounds/freedesktop/stereo/alarm-clock-elapsed.oga"

        try:
            cmd = f"su - {sudo_user} -c 'XDG_RUNTIME_DIR=/run/user/{uid} paplay {sound_file}'"
            subprocess.Popen(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except:
            pass

        try:
            cmd = f"su - {sudo_user} -c 'XDG_RUNTIME_DIR=/run/user/{uid} pw-play {sound_file}'"
            subprocess.Popen(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except:
            pass

        for _ in range(3):
            print('\a', end='', flush=True)
            time.sleep(0.2)

    def prompt_with_timeout(self, prompt, options, default, timeout=None):
        """Display a prompt with timeout and sound notification"""
        if timeout is None:
            timeout = DEFAULT_PROMPT_TIMEOUT

        self.play_notification_sound()

        print(f"\n{prompt}")
        for key, desc in options.items():
            default_marker = " (DEFAULT - auto-select in {:.0f}s)".format(timeout) if key == default else ""
            print(f"  [{key}] {desc}{default_marker}")

        start_time = time.time()
        print(f"\nChoice [{'/'.join(options.keys())}]: ", end='', flush=True)

        while True:
            elapsed = time.time() - start_time
            remaining = timeout - elapsed

            if remaining <= 0:
                print(f"\n>> Timeout reached, auto-selecting '{default}'")
                return default

            ready, _, _ = select.select([sys.stdin], [], [], min(1.0, remaining))

            if ready:
                choice = sys.stdin.readline().strip().lower()
                if choice in options or choice == '':
                    return choice if choice else default
                print(f"Invalid choice '{choice}'. Please enter one of: {', '.join(options.keys())}")
                print(f"\nChoice [{'/'.join(options.keys())}]: ", end='', flush=True)
            else:
                if int(remaining) % 30 == 0 and int(remaining) != int(timeout):
                    print(f"\r  (Auto-selecting '{default}' in {int(remaining)}s) Choice [{'/'.join(options.keys())}]: ", end='', flush=True)

    def run_cmd(self, cmd, timeout=300):
        """Run shell command and return output"""
        try:
            result = subprocess.run(cmd, shell=True, capture_output=True,
                                   text=True, timeout=timeout)
            return result.stdout, result.returncode
        except subprocess.TimeoutExpired:
            return "", -1

    def check_device_exists(self, device=None):
        """Check if a device exists and is accessible.

        Args:
            device: Device path to check (defaults to self.source)

        Returns:
            True if device exists, False otherwise
        """
        if device is None:
            device = self.source

        # Check if device file exists
        if not os.path.exists(device):
            return False

        # Try to get size - this verifies it's actually accessible
        try:
            cmd = f"blockdev --getsize64 {device} 2>/dev/null"
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=5)
            return result.returncode == 0
        except:
            return False

    def create_domain_file(self, regions, filename):
        """Create ddrescue domain file for specific regions"""
        domain_path = self.job_dir / filename

        # Sort regions by start position - ddrescue requires ascending order
        sorted_regions = sorted(regions, key=lambda x: x[0])

        # Merge overlapping or adjacent regions - ddrescue doesn't allow overlaps
        merged = []
        for start, size in sorted_regions:
            end = start + size
            if merged and start <= merged[-1][0] + merged[-1][1]:
                # Overlaps or adjacent - extend the previous region
                prev_start, prev_size = merged[-1]
                prev_end = prev_start + prev_size
                new_end = max(end, prev_end)
                merged[-1] = (prev_start, new_end - prev_start)
            else:
                merged.append((start, size))

        sorted_regions = merged

        with open(domain_path, 'w') as f:
            f.write("# Mapfile. Created by GNU ddrescue version 1.23\n")
            f.write(f"# Domain file - {filename}\n")
            f.write("# current_pos  current_status  current_pass\n")

            # Use zero-padded hex format to match ddrescue's expected format
            if sorted_regions:
                f.write(f"0x{sorted_regions[0][0]:08X}     +               1\n")
            else:
                f.write("0x00000000     +               1\n")

            f.write("#      pos        size  status\n")
            for start, size in sorted_regions:
                f.write(f"0x{start:08X}  0x{size:08X}  +\n")

        return domain_path

    def check_region_recovered(self, start, size):
        """Check what percentage of a region is recovered in the log"""
        if not os.path.exists(self.log_file):
            return 0.0

        recovered = 0
        end = start + size

        with open(self.log_file, 'r') as f:
            for line in f:
                line = line.strip()
                if line.startswith('#') or not line:
                    continue
                parts = line.split()
                if len(parts) >= 3:
                    try:
                        pos = int(parts[0], 16)
                        sz = int(parts[1], 16)
                        status = parts[2]
                        if status == '+':
                            overlap_start = max(pos, start)
                            overlap_end = min(pos + sz, end)
                            if overlap_end > overlap_start:
                                recovered += overlap_end - overlap_start
                    except:
                        pass

        return (recovered / size * 100) if size > 0 else 0

    def _parse_log_recovered_regions(self):
        """Parse ddrescue log file ONCE and return sorted list of recovered regions.

        Returns list of (start, end) tuples sorted by start, for efficient overlap checks.
        """
        recovered_regions = []

        if not os.path.exists(self.log_file):
            return recovered_regions

        with open(self.log_file, 'r') as f:
            for line in f:
                line = line.strip()
                if line.startswith('#') or not line:
                    continue
                parts = line.split()
                if len(parts) >= 3:
                    try:
                        pos = int(parts[0], 16)
                        sz = int(parts[1], 16)
                        status = parts[2]
                        if status == '+':
                            recovered_regions.append((pos, pos + sz))
                    except:
                        pass

        # Sort by start position for efficient binary search
        recovered_regions.sort(key=lambda x: x[0])
        return recovered_regions

    def _calculate_bulk_recovery(self, regions):
        """Calculate recovery for multiple regions efficiently.

        Parses the log file ONCE, then uses binary search for each region.
        Returns total bytes recovered and per-region percentages.

        Args:
            regions: list of (start, size) tuples

        Returns:
            tuple: (total_recovered_bytes, list of (start, size, pct) tuples)
        """
        print("  Calculating recovery status...", end='', flush=True)

        # Parse log once
        recovered_log = self._parse_log_recovered_regions()
        if not recovered_log:
            print(" (no log data)")
            return 0, [(r[0], r[1], 0.0) for r in regions]

        # Extract start positions for binary search
        log_starts = [r[0] for r in recovered_log]

        total_recovered = 0
        region_results = []

        for i, (start, size) in enumerate(regions):
            end = start + size
            recovered = 0

            # Find first log entry that might overlap (start before our region ends)
            # Binary search for the first log entry starting <= end
            idx = bisect.bisect_right(log_starts, start)
            if idx > 0:
                idx -= 1  # Back up to check previous entry too

            # Scan forward through potentially overlapping entries
            while idx < len(recovered_log):
                log_start, log_end = recovered_log[idx]

                # If log region starts after our region ends, we're done
                if log_start >= end:
                    break

                # Calculate overlap
                overlap_start = max(log_start, start)
                overlap_end = min(log_end, end)
                if overlap_end > overlap_start:
                    recovered += overlap_end - overlap_start

                idx += 1

            pct = (recovered / size * 100) if size > 0 else 0.0
            total_recovered += recovered
            region_results.append((start, size, pct))

            # Progress indicator every 1000 regions
            if (i + 1) % 1000 == 0:
                print(f"\r  Calculating recovery status... {i+1}/{len(regions)}", end='', flush=True)

        print(f"\r  Calculating recovery status... done ({len(regions)} regions)    ")
        return total_recovered, region_results

    def parse_ddrescue_log_stats(self):
        """Parse ddrescue log file to get actual recovery statistics.

        Returns dict with: rescued, bad_sector, bad_areas, non_tried, non_trimmed, non_scraped
        All values in bytes except bad_areas which is a count.
        """
        stats = {
            'rescued': 0,
            'bad_sector': 0,
            'bad_areas': 0,
            'non_tried': 0,
            'non_trimmed': 0,
            'non_scraped': 0,
            'total': 0
        }

        if not os.path.exists(self.log_file):
            return stats

        # Status codes in ddrescue mapfile:
        # + = rescued (finished)
        # - = bad sector
        # * = non-trimmed
        # / = non-scraped
        # ? = non-tried
        status_map = {
            '+': 'rescued',
            '-': 'bad_sector',
            '*': 'non_trimmed',
            '/': 'non_scraped',
            '?': 'non_tried'
        }

        with open(self.log_file, 'r') as f:
            for line in f:
                line = line.strip()
                if line.startswith('#') or not line:
                    continue
                parts = line.split()
                if len(parts) >= 3:
                    try:
                        size = int(parts[1], 16)
                        status = parts[2]
                        if status in status_map:
                            stats[status_map[status]] += size
                            stats['total'] += size
                            if status == '-':
                                stats['bad_areas'] += 1
                    except:
                        pass

        return stats

    def get_recovery_percentage(self):
        """Get the actual recovery percentage from ddrescue log."""
        stats = self.parse_ddrescue_log_stats()
        if stats['total'] == 0:
            return 0.0, stats
        pct = (stats['rescued'] / stats['total']) * 100
        return pct, stats

    def run_ddrescue(self, domain_file, description, loose_domain=False):
        """Run ddrescue with domain file"""
        print(f"\n{'='*60}")
        print(f"Running ddrescue: {description}")
        print(f"{'='*60}")
        print(f"Domain: {domain_file}")

        L_flag = "-L " if loose_domain else ""
        cmd = f"ddrescue -f -d {L_flag}-m {domain_file} {self.source} {self.dest} {self.log_file}"
        print(f"Command: {cmd}")
        print()

        os.system(cmd)

    def extract_bytes(self, offset, size):
        """Extract bytes from destination device at given offset"""
        cmd = f"dd if={self.dest} bs=1 skip={offset} count={size} 2>/dev/null"
        result = subprocess.run(cmd, shell=True, capture_output=True)
        return result.stdout

    # =========================================================================
    # STAGE 0: Recover Critical Structures (partition table, EFI, boot blocks)
    # =========================================================================
    def stage0_critical_structures(self):
        """Recover critical disk structures needed for the destination to be usable.

        This includes:
        - GPT partition table (first 34 sectors)
        - Backup GPT (last 33 sectors)
        - EFI System Partition (if present)
        - HFS+ boot blocks (first 1024 bytes of partition)
        - Alternate Volume Header (last 1024 bytes of partition)
        """
        print("\n" + "="*60)
        print("STAGE 0: Critical Disk Structures")
        print("="*60)

        regions = []

        # 1. GPT Primary Header and Partition Entries (first 34 sectors = 17KB)
        #    GPT structure: LBA 0 (MBR) + LBA 1 (Header) + LBA 2-33 (Entries)
        #    Using exactly 34 sectors to avoid overlap with EFI (often starts at sector 40)
        gpt_primary_size = 34 * self.sector_size  # 17KB
        regions.append((0, gpt_primary_size, "GPT Primary"))
        print(f"  GPT Primary: sectors 0-33 ({gpt_primary_size} bytes)")

        # 2. Backup GPT (last 33 sectors of disk)
        gpt_backup_size = 33 * self.sector_size  # 16.5KB
        gpt_backup_start = self.drive_size - gpt_backup_size
        regions.append((gpt_backup_start, gpt_backup_size, "GPT Backup"))
        print(f"  GPT Backup: offset {gpt_backup_start} ({gpt_backup_size} bytes)")

        # 3. Parse fdisk to find partitions
        cmd = f"fdisk -l {self.source} 2>/dev/null"
        output, rc = self.run_cmd(cmd)

        efi_partition = None
        hfs_partition = None
        hfs_start_sector = None
        hfs_end_sector = None

        for line in output.split('\n'):
            parts = line.split()
            if len(parts) < 5:
                continue

            # Look for EFI System Partition
            if 'EFI' in line and parts[0].startswith(self.source):
                efi_partition = parts[0]
                try:
                    # Find start and end sectors
                    for i, p in enumerate(parts):
                        if p.isdigit():
                            efi_start = int(p)
                            if i+1 < len(parts) and parts[i+1].isdigit():
                                efi_end = int(parts[i+1])
                                efi_size = (efi_end - efi_start + 1) * self.sector_size
                                regions.append((efi_start * self.sector_size, efi_size, "EFI System Partition"))
                                print(f"  EFI Partition: sectors {efi_start}-{efi_end} ({efi_size/(1024**2):.1f} MB)")
                            break
                except:
                    pass

            # Look for HFS+ partition
            if ('Apple HFS' in line or 'Apple_HFS' in line) and parts[0].startswith(self.source):
                hfs_partition = parts[0]
                try:
                    for i, p in enumerate(parts):
                        if p.isdigit():
                            hfs_start_sector = int(p)
                            if i+1 < len(parts) and parts[i+1].isdigit():
                                hfs_end_sector = int(parts[i+1])
                            break
                except:
                    pass

        # 4. HFS+ Boot Blocks (first 1024 bytes of partition, before Volume Header)
        if hfs_start_sector:
            boot_blocks_start = hfs_start_sector * self.sector_size
            boot_blocks_size = 1024
            regions.append((boot_blocks_start, boot_blocks_size, "HFS+ Boot Blocks"))
            print(f"  HFS+ Boot Blocks: offset {boot_blocks_start} (1024 bytes)")

            # Save for later stages
            self.state["hfs_start_sector"] = hfs_start_sector
            self.state["hfs_end_sector"] = hfs_end_sector

        # 5. Alternate Volume Header (last 1024 bytes of HFS+ partition)
        if hfs_end_sector:
            # End sector is inclusive, so partition ends at (hfs_end_sector + 1) * sector_size
            partition_end = (hfs_end_sector + 1) * self.sector_size
            alt_vh_start = partition_end - 1024
            alt_vh_size = 1024
            regions.append((alt_vh_start, alt_vh_size, "Alternate Volume Header"))
            print(f"  Alternate VH: offset {alt_vh_start} (1024 bytes)")

        if not regions:
            print("  WARNING: Could not detect any critical structures")
            print("  Proceeding without Stage 0 recovery...")
            return True

        # Create domain file for all critical regions
        print(f"\n  Total critical regions: {len(regions)}")
        domain_regions = [(start, size) for start, size, name in regions]
        domain_path = self.create_domain_file(domain_regions, "critical_structures_domain.txt")

        # Check current recovery status
        total_size = sum(r[1] for r in domain_regions)
        recovered = 0
        for start, size in domain_regions:
            pct = self.check_region_recovered(start, size)
            recovered += size * pct / 100

        if recovered >= total_size * 0.99:
            print(f"\n  Critical structures already recovered ({recovered/total_size*100:.1f}%)")
        else:
            print(f"\n  Recovery status: {recovered/total_size*100:.1f}%")
            self.run_ddrescue(domain_path, "Critical Disk Structures", loose_domain=True)

            # Re-check recovery status after ddrescue
            recovered = 0
            for start, size in domain_regions:
                pct = self.check_region_recovered(start, size)
                recovered += size * pct / 100

            final_pct = (recovered / total_size * 100) if total_size > 0 else 0
            print(f"\n  Post-recovery status: {final_pct:.1f}%")

            if final_pct < 1:
                print("  ERROR: Critical structures recovery failed (0%)")
                print("  This may indicate a domain file format error.")
                print("  Not marking Stage 0 as complete - will retry on next run.")
                return False

            if final_pct < 90:
                print(f"  WARNING: Only {final_pct:.1f}% of critical structures recovered")
                print("  The destination may not be bootable/mountable")
                print("  (GPT, EFI, or boot blocks may have bad sectors)")

        self.state["critical_structures_recovered"] = True
        self.save_state()

        # Try to make kernel re-read partition table on destination
        print("\n  Refreshing destination partition table...")
        cmd = f"partprobe {self.dest} 2>/dev/null || blockdev --rereadpt {self.dest} 2>/dev/null"
        os.system(cmd)
        time.sleep(1)

        return True

    # =========================================================================
    # STAGE 1: Find HFS+ partition and recover Volume Header
    # =========================================================================
    def stage1_volume_header(self):
        """Find HFS+ partition and recover Volume Header"""
        print("\n" + "="*60)
        print("STAGE 1: HFS+ Volume Header Recovery")
        print("="*60)

        # First, find HFS+ partition using fdisk/parted
        print("Detecting HFS+ partition...")

        # Try to find partition offset using fdisk
        cmd = f"fdisk -l {self.source} 2>/dev/null"
        output, rc = self.run_cmd(cmd)

        hfs_partition = None
        partition_start_sector = None

        for line in output.split('\n'):
            if 'Apple HFS' in line or 'Apple_HFS' in line or 'af ' in line.lower():
                parts = line.split()
                for i, part in enumerate(parts):
                    if part.startswith(self.source):
                        hfs_partition = part
                    # Look for start sector (usually second or third number)
                    try:
                        if parts[i].isdigit() and int(parts[i]) > 2047:
                            partition_start_sector = int(parts[i])
                            break
                    except:
                        pass
                if hfs_partition:
                    break

        # If fdisk didn't find it, try the common partition (sdb2 for Macs)
        if not hfs_partition:
            # Check if source is a partition or whole disk
            if self.source[-1].isdigit():
                hfs_partition = self.source
                partition_start_sector = 0  # Already a partition
            else:
                # Try sdb2 (common for Mac drives)
                test_part = f"{self.source}2"
                cmd = f"fsstat -f hfs {test_part} 2>&1"
                output, rc = self.run_cmd(cmd)
                if 'HFS+' in output:
                    hfs_partition = test_part
                    # Get partition start
                    cmd = f"fdisk -l {self.source} 2>/dev/null | grep {test_part}"
                    output2, _ = self.run_cmd(cmd)
                    parts = output2.split()
                    for p in parts:
                        try:
                            val = int(p)
                            if val > 2047:
                                partition_start_sector = val
                                break
                        except:
                            pass

        if not hfs_partition:
            print("ERROR: Could not find HFS+ partition")
            return False

        # Get partition start from sysfs if we still don't have it
        if partition_start_sector is None or partition_start_sector == 0:
            try:
                part_name = os.path.basename(hfs_partition)
                with open(f"/sys/class/block/{part_name}/start") as f:
                    partition_start_sector = int(f.read().strip())
            except:
                partition_start_sector = 0

        self.partition_offset = partition_start_sector * self.sector_size
        self.hfs_partition = hfs_partition

        print(f"Found HFS+ partition: {hfs_partition}")
        print(f"Partition start: sector {partition_start_sector} (byte {self.partition_offset})")

        # Volume Header is at offset 1024 bytes into the partition
        vh_offset = self.partition_offset + 1024
        vh_size = 512

        pct = self.check_region_recovered(vh_offset, vh_size)
        print(f"Volume Header at offset {vh_offset} ({vh_offset/(1024**3):.4f} GB)")
        print(f"Recovery status: {pct:.1f}%")

        if pct < 100:
            domain = self.create_domain_file([(vh_offset, vh_size)], "volume_header_domain.txt")
            self.run_ddrescue(domain, "Volume Header")
            pct = self.check_region_recovered(vh_offset, vh_size)

        if pct < 100:
            print(f"WARNING: Volume Header only {pct:.1f}% recovered")
            return False

        # Parse Volume Header from destination
        print("\nParsing Volume Header...")
        vh_data = self.extract_bytes(vh_offset, vh_size)

        # Check HFS+ signature
        if vh_data[0:2] != b'H+' and vh_data[0:2] != b'HX':
            print(f"WARNING: Invalid HFS+ signature: {vh_data[0:2]}")
            print("Trying anyway...")

        # Parse Volume Header fields (big-endian!)
        # Offset 0x28: Block size (4 bytes)
        # Offset 0x40: Total blocks (4 bytes)
        # Offset 0x48: Free blocks (4 bytes)

        self.block_size = struct.unpack('>I', vh_data[0x28:0x2C])[0]
        self.total_blocks = struct.unpack('>I', vh_data[0x40:0x44])[0]
        self.free_blocks = struct.unpack('>I', vh_data[0x48:0x4C])[0]

        print(f"  Block size: {self.block_size} bytes")
        print(f"  Total blocks: {self.total_blocks:,}")
        print(f"  Free blocks: {self.free_blocks:,}")
        print(f"  Used blocks: {self.total_blocks - self.free_blocks:,}")
        print(f"  Utilization: {((self.total_blocks - self.free_blocks) / self.total_blocks * 100):.1f}%")

        self.state["volume_header_parsed"] = True
        self.state["partition_offset"] = self.partition_offset
        self.state["hfs_partition"] = hfs_partition
        self.state["block_size"] = self.block_size
        self.state["total_blocks"] = self.total_blocks
        self.state["free_blocks"] = self.free_blocks
        self.save_state()

        return True

    # =========================================================================
    # STAGE 2: Recover Allocation File
    # =========================================================================
    def _get_dest_partition(self):
        """Get the destination partition path corresponding to the source partition"""
        # If source partition is /dev/sdb2, and dest drive is /dev/sdf,
        # then dest partition is /dev/sdf2
        hfs_partition = self.state.get("hfs_partition", self.hfs_partition)
        if not hfs_partition:
            return None

        # Extract partition number from source partition
        match = re.search(r'(\d+)$', hfs_partition)
        if match:
            part_num = match.group(1)
            # Append to destination drive
            return f"{self.dest}{part_num}"
        return None

    def _parse_alloc_extents_from_vh(self, vh_data):
        """Parse allocation file extents directly from Volume Header fork data.

        This is the authoritative source - same data the filesystem uses.
        The HFSPlusForkData structure at offset 0x70 contains:
          - logicalSize (8 bytes): actual file size
          - clumpSize (4 bytes): allocation clump size
          - totalBlocks (4 bytes): total blocks allocated
          - extents[8] (64 bytes): first 8 extent descriptors

        Each extent descriptor is 8 bytes:
          - startBlock (4 bytes)
          - blockCount (4 bytes)
        """
        extents = []

        try:
            # Allocation file fork data starts at offset 0x70 in Volume Header
            fork_offset = 0x70

            # Parse fork data header
            logical_size = struct.unpack('>Q', vh_data[fork_offset:fork_offset+8])[0]
            # clump_size = struct.unpack('>I', vh_data[fork_offset+8:fork_offset+12])[0]
            total_blocks = struct.unpack('>I', vh_data[fork_offset+12:fork_offset+16])[0]

            print(f"  Allocation File from Volume Header:")
            print(f"    Logical size: {logical_size:,} bytes ({logical_size/(1024**2):.1f} MB)")
            print(f"    Total blocks: {total_blocks:,}")

            # Parse 8 extent descriptors (each 8 bytes: startBlock + blockCount)
            extent_offset = fork_offset + 16
            for i in range(8):
                start_block = struct.unpack('>I', vh_data[extent_offset:extent_offset+4])[0]
                block_count = struct.unpack('>I', vh_data[extent_offset+4:extent_offset+8])[0]

                if block_count > 0:
                    extents.append((start_block, block_count))
                    print(f"    Extent {i}: blocks {start_block:,} - {start_block + block_count - 1:,} ({block_count:,} blocks)")

                extent_offset += 8

            if not extents:
                print("  WARNING: No extents found in Volume Header fork data")
                return [], 0

            # Check if file might have overflow extents
            blocks_in_extents = sum(count for _, count in extents)
            if blocks_in_extents < total_blocks:
                print(f"  WARNING: Volume Header shows {len(extents)} extents covering {blocks_in_extents} blocks,")
                print(f"           but total_blocks is {total_blocks}. File may have overflow extents.")
                print(f"           Proceeding with available extents.")

            return extents, logical_size

        except Exception as e:
            print(f"  ERROR parsing Volume Header fork data: {e}")
            return [], 0

    def _parse_istat_extents(self, istat_output):
        """Parse istat output to extract extent/block locations for HFS+ file"""
        extents = []

        # Look for extent information in istat output
        # HFS+ istat output typically shows:
        #   Type: Regular File
        #   Size: 123456
        #   Direct Blocks:
        #   1234 1235 1236 ...
        # Or it shows extents like:
        #   Extents:
        #   0: Start block: 1234  Block count: 100

        lines = istat_output.split('\n')
        in_extents_section = False
        in_direct_blocks = False

        for line in lines:
            line = line.strip()

            # Check for extent section markers
            if 'Extent' in line and ':' in line:
                in_extents_section = True
                in_direct_blocks = False
                continue

            if 'Direct Block' in line:
                in_direct_blocks = True
                in_extents_section = False
                continue

            # Parse extent entries like "0: Start block: 1234  Block count: 100"
            if in_extents_section:
                if 'Start block:' in line and 'Block count:' in line:
                    try:
                        start_match = re.search(r'Start block:\s*(\d+)', line)
                        count_match = re.search(r'Block count:\s*(\d+)', line)
                        if start_match and count_match:
                            start_block = int(start_match.group(1))
                            block_count = int(count_match.group(1))
                            if block_count > 0:
                                extents.append((start_block, block_count))
                    except:
                        pass

            # Parse direct block numbers (space-separated list)
            if in_direct_blocks and line and not any(c.isalpha() for c in line.replace(' ', '')):
                try:
                    blocks = [int(b) for b in line.split() if b.isdigit()]
                    # Group consecutive blocks into ranges
                    if blocks:
                        current_start = blocks[0]
                        current_count = 1
                        for i in range(1, len(blocks)):
                            if blocks[i] == blocks[i-1] + 1:
                                current_count += 1
                            else:
                                extents.append((current_start, current_count))
                                current_start = blocks[i]
                                current_count = 1
                        extents.append((current_start, current_count))
                except:
                    pass

        return extents

    def stage2_allocation_file(self):
        """Recover the Allocation File (bitmap)"""
        print("\n" + "="*60)
        print("STAGE 2: Allocation File Recovery")
        print("="*60)

        hfs_partition = self.state.get("hfs_partition", self.hfs_partition)
        partition_offset = self.state.get("partition_offset", self.partition_offset)
        block_size = self.state.get("block_size", self.block_size)

        # Step 2a: Get Allocation File extent locations from Volume Header
        # This is the authoritative source - same approach as NTFS parsing MFT
        print("\nStep 2a: Parsing Allocation File extents from Volume Header...")

        # Re-read Volume Header from destination (already recovered in Stage 1)
        vh_offset = partition_offset + 1024
        vh_data = self.extract_bytes(vh_offset, 512)

        if len(vh_data) < 512:
            print(f"ERROR: Could not read Volume Header from destination")
            return False

        # Parse allocation file extents from Volume Header fork data
        extents, alloc_size = self._parse_alloc_extents_from_vh(vh_data)

        # Fallback to istat if Volume Header parsing failed
        if not extents:
            print("\n  Volume Header parsing failed, trying istat as fallback...")
            cmd = f"istat -f hfs {hfs_partition} 6"
            output, rc = self.run_cmd(cmd)

            if rc == 0:
                print(f"  istat output:\n{output}")
                extents = self._parse_istat_extents(output)

                # Parse size from istat if we don't have it
                if not alloc_size:
                    for line in output.split('\n'):
                        if line.startswith('Size:'):
                            try:
                                alloc_size = int(line.split(':')[1].strip())
                            except:
                                pass

        # If still no extents, we cannot proceed safely
        if not extents:
            print("\nERROR: Could not determine Allocation File location")
            print("  - Volume Header fork data parsing failed")
            print("  - istat fallback failed")
            print("\nCannot safely proceed without knowing allocation file location.")
            print("Manual investigation required.")
            return False

        # Estimate size if we still don't have it
        if not alloc_size:
            total_blocks = self.state.get("total_blocks", self.total_blocks)
            alloc_size = (total_blocks + 7) // 8
            print(f"  Estimated Allocation File size: {alloc_size:,} bytes")

        self.alloc_file_size = alloc_size

        # Step 2b: Convert block extents to byte offsets for domain file
        print("\nStep 2b: Creating domain file for Allocation File blocks...")
        alloc_regions = []
        for start_block, block_count in extents:
            start_byte = partition_offset + (start_block * block_size)
            size_bytes = block_count * block_size
            alloc_regions.append((start_byte, size_bytes))
            print(f"  Blocks {start_block}-{start_block + block_count - 1}: {size_bytes/(1024**2):.2f} MB at offset 0x{start_byte:X}")

        domain_path = self.create_domain_file(alloc_regions, "alloc_file_domain.txt")
        print(f"  Created domain file: {domain_path}")

        # Step 2c: Run ddrescue to recover allocation file blocks to destination
        print("\nStep 2c: Recovering Allocation File blocks to destination...")
        self.run_ddrescue(domain_path, "Allocation File Blocks")

        # Check recovery status
        total_alloc_size = sum(r[1] for r in alloc_regions)
        recovered = 0
        for start, size in alloc_regions:
            pct = self.check_region_recovered(start, size)
            recovered += size * pct / 100

        recovery_pct = (recovered / total_alloc_size * 100) if total_alloc_size > 0 else 0
        print(f"\n  Allocation File recovery: {recovery_pct:.1f}%")

        if recovery_pct < 90:
            print(f"  WARNING: Only {recovery_pct:.1f}% of Allocation File recovered")
            print("  Bitmap parsing may be incomplete, but continuing...")

        # Step 2d: Extract Allocation File from DESTINATION partition
        print("\nStep 2d: Extracting Allocation File from DESTINATION...")

        dest_partition = self._get_dest_partition()
        if not dest_partition:
            print("  ERROR: Could not determine destination partition path")
            print(f"  Source partition: {hfs_partition}")
            print(f"  Destination drive: {self.dest}")
            return False

        print(f"  Destination partition: {dest_partition}")

        alloc_path = self.job_dir / "allocation_file.raw"
        print(f"  Extracting to: {alloc_path}")

        # Extract from DESTINATION (which now has the recovered data)
        cmd = f"icat -f hfs {dest_partition} 6 > {alloc_path}"
        result = os.system(cmd)

        if result != 0:
            print(f"  WARNING: icat returned non-zero: {result}")
            # Try direct extraction from destination using dd at the extent locations
            print("  Attempting direct extraction from destination extents...")
            with open(alloc_path, 'wb') as outf:
                for start_byte, size_bytes in alloc_regions:
                    cmd = f"dd if={self.dest} bs=1 skip={start_byte} count={size_bytes} 2>/dev/null"
                    result = subprocess.run(cmd, shell=True, capture_output=True)
                    outf.write(result.stdout)

        if not alloc_path.exists() or alloc_path.stat().st_size == 0:
            print("ERROR: Failed to extract Allocation File from destination")
            print("The allocation file blocks may need more recovery.")
            return False

        actual_size = alloc_path.stat().st_size
        print(f"  Extracted {actual_size} bytes ({actual_size/(1024**2):.1f} MB)")

        # Verify we got reasonable data (not all zeros)
        with open(alloc_path, 'rb') as f:
            sample = f.read(4096)
            if sample == b'\x00' * len(sample):
                print("  WARNING: Extracted data appears to be all zeros")
                print("  Allocation file blocks may not have been recovered")

        self.state["alloc_file_extracted"] = True
        self.state["alloc_file_size"] = actual_size
        self.state["alloc_file_extents"] = extents
        self.state["dest_partition"] = dest_partition
        self.save_state()

        return True

    # =========================================================================
    # STAGE 3: Parse Allocation File for allocated blocks
    # =========================================================================
    def stage3_parse_allocation(self):
        """Parse Allocation File to find all allocated blocks"""
        print("\n" + "="*60)
        print("STAGE 3: Parse Allocation File")
        print("="*60)

        alloc_path = self.job_dir / "allocation_file.raw"

        if not alloc_path.exists():
            print("ERROR: Allocation File not found")
            return False

        print(f"Reading {alloc_path}...")
        with open(alloc_path, 'rb') as f:
            bitmap_data = f.read()

        print(f"  Read {len(bitmap_data)} bytes")

        # Calculate max valid block based on drive size
        partition_offset = self.state.get("partition_offset", self.partition_offset)
        block_size = self.state.get("block_size", self.block_size)
        max_block = (self.drive_size - partition_offset) // block_size

        print(f"  Max valid block: {max_block:,}")

        # Parse bitmap - HFS+ uses 1 = allocated, 0 = free (same as NTFS)
        allocated_ranges = []
        current_start = None
        prev_block = None
        total_allocated = 0

        for byte_idx, byte in enumerate(bitmap_data):
            for bit_idx in range(8):
                block_num = byte_idx * 8 + bit_idx

                if block_num >= max_block:
                    if current_start is not None:
                        start_byte = partition_offset + (current_start * block_size)
                        size_bytes = (prev_block - current_start + 1) * block_size
                        allocated_ranges.append((start_byte, size_bytes, current_start, prev_block))
                        current_start = None
                    break

                # HFS+ bitmap: bit set (1) = allocated
                is_allocated = bool(byte & (1 << (7 - bit_idx)))  # HFS+ is big-endian bit order

                if is_allocated:
                    total_allocated += 1
                    if current_start is None:
                        current_start = block_num
                    prev_block = block_num
                else:
                    if current_start is not None:
                        start_byte = partition_offset + (current_start * block_size)
                        size_bytes = (prev_block - current_start + 1) * block_size
                        allocated_ranges.append((start_byte, size_bytes, current_start, prev_block))
                        current_start = None

            if block_num >= max_block:
                break

        # Don't forget last range
        if current_start is not None:
            start_byte = partition_offset + (current_start * block_size)
            size_bytes = (prev_block - current_start + 1) * block_size
            allocated_ranges.append((start_byte, size_bytes, current_start, prev_block))

        # Sort by start position
        allocated_ranges.sort(key=lambda x: x[0])

        self.allocated_clusters = allocated_ranges
        total_data_bytes = sum(r[1] for r in allocated_ranges)

        print(f"  Total allocated blocks: {total_allocated:,}")
        print(f"  Total allocated data: {total_data_bytes/(1024**3):.2f} GB")
        print(f"  Contiguous ranges: {len(allocated_ranges):,}")

        # Save summary
        summary_path = self.job_dir / "block_analysis.txt"
        with open(summary_path, 'w') as f:
            f.write(f"HFS+ Block Analysis\n")
            f.write(f"===================\n")
            f.write(f"Total allocated blocks: {total_allocated:,}\n")
            f.write(f"Total allocated data: {total_data_bytes/(1024**3):.2f} GB\n")
            f.write(f"Contiguous ranges: {len(allocated_ranges):,}\n\n")
            f.write(f"Top 20 largest ranges:\n")
            sorted_ranges = sorted(allocated_ranges, key=lambda x: x[1], reverse=True)[:20]
            for start, size, bstart, bend in sorted_ranges:
                f.write(f"  Blocks {bstart:,}-{bend:,}: {size/(1024**2):.1f} MB at 0x{start:X}\n")

        print(f"  Analysis saved to {summary_path}")

        self.state["allocated_ranges_count"] = len(allocated_ranges)
        self.state["total_allocated_bytes"] = total_data_bytes
        self.save_state()

        return True

    # =========================================================================
    # STAGE 4: Create data recovery domain
    # =========================================================================
    def stage4_create_data_domain(self):
        """Create domain file for all allocated data"""
        print("\n" + "="*60)
        print("STAGE 4: Create Data Recovery Domain")
        print("="*60)

        if not self.allocated_clusters:
            print("ERROR: No allocated blocks found")
            return False

        regions = [(r[0], r[1]) for r in self.allocated_clusters]
        domain_path = self.create_domain_file(regions, "all_data_domain.txt")

        total_size = sum(r[1] for r in regions)
        print(f"Created domain file: {domain_path}")
        print(f"  Regions: {len(regions):,}")
        print(f"  Total size: {total_size/(1024**3):.2f} GB")

        # Check current recovery status (efficient bulk calculation)
        total_recovered, _ = self._calculate_bulk_recovery(regions)
        overall_pct = (total_recovered / total_size * 100) if total_size > 0 else 0
        remaining = total_size - total_recovered

        print(f"\nCurrent recovery status:")
        print(f"  Recovered: {total_recovered/(1024**3):.2f} GB ({overall_pct:.1f}%)")
        print(f"  Remaining: {remaining/(1024**3):.2f} GB")

        self.state["data_domain_created"] = True
        self.state["data_recovery_pct"] = overall_pct
        self.save_state()

        return True

    # =========================================================================
    # STAGE 5: Recover all data
    # =========================================================================
    def stage5_recover_data(self):
        """Run ddrescue to recover all allocated data"""
        print("\n" + "="*60)
        print("STAGE 5: Recover All Allocated Data")
        print("="*60)

        domain_path = self.job_dir / "all_data_domain.txt"
        if not domain_path.exists():
            print("ERROR: Data domain file not found")
            return False

        self.run_ddrescue(domain_path, "All Allocated Data", loose_domain=True)

        # Get domain-specific recovery stats (not whole drive)
        bad_bytes, bad_areas, total_bytes, pct = self._get_domain_bad_sectors(domain_path)
        rescued_bytes = total_bytes - bad_bytes  # Approximation

        print(f"\nFinal recovery status (allocated data only):")
        print(f"  Domain size: {total_bytes/(1024**3):.2f} GB")
        if bad_bytes == 0:
            print(f"  Status: ✓ 100% COMPLETE - No bad sectors!")
        else:
            # Never say 100% if there are bad sectors
            display_pct = min(pct, 99.99) if bad_bytes > 0 else pct
            print(f"  Recovered: {display_pct:.2f}%")
            print(f"  Bad sectors: {bad_bytes:,} bytes ({bad_areas} areas) - NOT complete")

        self.state["final_recovery_pct"] = pct
        self.state["bad_sectors"] = bad_bytes
        self.state["bad_areas"] = bad_areas
        self.save_state()

        return True

    # =========================================================================
    # STAGE 6: Aggressive retry for bad sectors
    # =========================================================================
    def _check_domain_recovery(self, domain_path, name):
        """Check recovery status for a domain file. Returns (pct, remaining_bytes) or None."""
        if not domain_path.exists():
            return None
        regions = self._load_domain_regions(domain_path)
        if not regions:
            return None
        total = sum(r[1] for r in regions)
        recovered, _ = self._calculate_bulk_recovery(regions)
        pct = (recovered / total * 100) if total > 0 else 100
        return (pct, total - recovered)

    def _get_domain_bad_sectors(self, domain_path):
        """Check how many bad sectors fall within a domain's regions.

        Uses efficient binary search instead of O(n×m) nested loop.
        Returns (bad_bytes, bad_areas, total_bytes, pct_recovered)
        """
        if not domain_path.exists():
            return 0, 0, 0, 100.0

        regions = self._load_domain_regions(domain_path)
        if not regions:
            return 0, 0, 0, 100.0

        total_bytes = sum(r[1] for r in regions)

        if not os.path.exists(self.log_file):
            return 0, 0, total_bytes, 0.0

        # Parse log entries once, separate by status
        bad_entries = []  # (start, end) for bad sectors
        rescued_entries = []  # (start, end) for rescued

        with open(self.log_file, 'r') as f:
            for line in f:
                line = line.strip()
                if line.startswith('#') or not line:
                    continue
                parts = line.split()
                if len(parts) >= 3:
                    try:
                        pos = int(parts[0], 16)
                        size = int(parts[1], 16)
                        status = parts[2]
                        if status == '-':
                            bad_entries.append((pos, pos + size))
                        elif status == '+':
                            rescued_entries.append((pos, pos + size))
                    except:
                        pass

        # Sort for binary search
        bad_entries.sort()
        rescued_entries.sort()

        bad_starts = [e[0] for e in bad_entries]
        rescued_starts = [e[0] for e in rescued_entries]

        bad_bytes = 0
        bad_areas = 0
        rescued_bytes = 0

        # For each domain region, find overlapping log entries using binary search
        for reg_start, reg_size in regions:
            reg_end = reg_start + reg_size

            # Check bad sectors
            if bad_entries:
                idx = bisect.bisect_right(bad_starts, reg_start)
                if idx > 0:
                    idx -= 1
                while idx < len(bad_entries):
                    log_start, log_end = bad_entries[idx]
                    if log_start >= reg_end:
                        break
                    overlap_start = max(log_start, reg_start)
                    overlap_end = min(log_end, reg_end)
                    if overlap_end > overlap_start:
                        bad_bytes += overlap_end - overlap_start
                        bad_areas += 1
                    idx += 1

            # Check rescued
            if rescued_entries:
                idx = bisect.bisect_right(rescued_starts, reg_start)
                if idx > 0:
                    idx -= 1
                while idx < len(rescued_entries):
                    log_start, log_end = rescued_entries[idx]
                    if log_start >= reg_end:
                        break
                    overlap_start = max(log_start, reg_start)
                    overlap_end = min(log_end, reg_end)
                    if overlap_end > overlap_start:
                        rescued_bytes += overlap_end - overlap_start
                    idx += 1

        pct = (rescued_bytes / total_bytes * 100) if total_bytes > 0 else 100.0
        return bad_bytes, bad_areas, total_bytes, pct

    def stage6_aggressive_retry(self):
        """Retry recovery with aggressive settings, prioritized by importance"""
        print("\n" + "="*60)
        print("STAGE 6: Prioritized Aggressive Retry")
        print("="*60)

        # Define domains in priority order (most critical first)
        domains = [
            ("1. Critical Structures (GPT, boot, Volume Header)",
             self.job_dir / "critical_structures_domain.txt", True),
            ("2. Allocation File (bitmap - needed to find files)",
             self.job_dir / "alloc_file_domain.txt", False),
            ("3. User Data Files",
             self.job_dir / "all_data_domain.txt", True),
        ]

        print("\nChecking recovery status by priority...\n")

        incomplete_domains = []
        for name, path, use_loose in domains:
            if not path.exists():
                continue
            bad_bytes, bad_areas, total_bytes, pct = self._get_domain_bad_sectors(path)

            # Show status - don't say 100% if there are bad sectors
            if bad_bytes == 0:
                status = "✓ COMPLETE"
            elif pct >= 99.99:
                status = f">99.99% ({bad_bytes:,} bytes bad)"
            else:
                status = f"{pct:.2f}%"

            print(f"  {name}")
            print(f"    Status: {status}")
            print()

            if bad_bytes > 0:
                incomplete_domains.append((name, path, use_loose, bad_bytes, bad_areas, pct))

        if not incomplete_domains:
            print("All areas at 100% with no bad sectors - nothing to retry!")
            return True

        # Calculate combined domain stats (not whole drive)
        total_bad = sum(d[3] for d in incomplete_domains)
        total_areas = sum(d[4] for d in incomplete_domains)
        print(f"Total bad sectors in domains: {total_bad:,} bytes ({total_areas} areas)")

        print("\n" + "-"*60)
        print("Aggressive retry uses: -A (all blocks) -r3 (3 retries) -M (max effort)")
        print("This stresses the drive but may recover more data.")
        print("-"*60)

        options = {
            'a': "Retry ALL incomplete areas (in priority order)",
            'c': "CHOOSE which areas to retry",
            's': "Skip aggressive retry"
        }
        # No timeout for final stage - wait for user decision
        self.play_notification_sound()
        print("\nHow would you like to proceed?")
        for key, desc in options.items():
            print(f"  [{key}] {desc}")
        while True:
            choice = input(f"\nChoice [a/c/s]: ").strip().lower()
            if choice in options:
                break
            if choice == '':
                choice = 's'
                break
            print(f"Invalid choice. Please enter: a, c, or s")

        if choice == 's':
            print("Skipping aggressive retry.")
            return True

        domains_to_retry = incomplete_domains[:]

        if choice == 'c':
            # Let user choose which to retry
            domains_to_retry = []
            for name, path, use_loose, bad_bytes, bad_areas, pct in incomplete_domains:
                short_name = name.split('.')[1].strip().split('(')[0].strip()
                opt = {'y': 'Yes', 'n': 'No'}
                c = self.prompt_with_timeout(
                    f"Retry {short_name}? ({bad_bytes:,} bytes bad)",
                    opt, 'y', timeout=30
                )
                if c == 'y':
                    domains_to_retry.append((name, path, use_loose, bad_bytes, bad_areas, pct))

        if not domains_to_retry:
            print("No areas selected for retry.")
            return True

        # Run aggressive retry on each selected domain - multiple passes
        for name, domain_path, use_loose, bad_bytes, bad_areas, old_pct in domains_to_retry:
            print(f"\n{'='*60}")
            short_name = name.split('.')[1].strip().split('(')[0].strip()
            print(f"Aggressive retry: {short_name}")
            print(f"{'='*60}")
            # Never show 100% if there are bad sectors
            display_pct = min(old_pct, 99.99) if bad_bytes > 0 else old_pct
            print(f"  Before: {display_pct:.2f}%, {bad_bytes:,} bytes in {bad_areas} bad areas")

            L_flag = "-L " if use_loose else ""
            current_bad = bad_bytes

            # Pass 1: Standard aggressive (-r3)
            print(f"\n  --- Pass 1: Standard aggressive (-r3) ---")
            cmd = f"ddrescue -f -d -A -r3 -M {L_flag}-m {domain_path} {self.source} {self.dest} {self.log_file}"
            print(f"  Command: {cmd}\n")
            os.system(cmd)

            # Check after pass 1
            new_bad, new_areas, total, new_pct = self._get_domain_bad_sectors(domain_path)
            if new_bad < current_bad:
                print(f"\n  Pass 1 recovered: +{current_bad - new_bad:,} bytes")
                current_bad = new_bad

            # Pass 2: Extra aggressive (-r5) if still have bad sectors
            if new_bad > 0:
                print(f"\n  --- Pass 2: Extra aggressive (-r5) ---")
                cmd = f"ddrescue -f -d -A -r5 -M {L_flag}-m {domain_path} {self.source} {self.dest} {self.log_file}"
                print(f"  Command: {cmd}\n")
                os.system(cmd)

                # Check after pass 2
                new_bad, new_areas, total, new_pct = self._get_domain_bad_sectors(domain_path)
                if new_bad < current_bad:
                    print(f"\n  Pass 2 recovered: +{current_bad - new_bad:,} bytes")

            # Final status for this domain - never show 100% with bad sectors
            final_display_pct = min(new_pct, 99.99) if new_bad > 0 else 100.0
            print(f"\n  After: {final_display_pct:.2f}%, {new_bad:,} bytes in {new_areas} bad areas")

            improvement = bad_bytes - new_bad
            if improvement > 0:
                print(f"  Total improvement: +{improvement:,} bytes recovered!")
            elif new_bad == bad_bytes:
                print(f"  No change - these sectors may be permanently unreadable")

        # Final summary - recalculate domain stats
        print(f"\n{'='*60}")
        print("AGGRESSIVE RETRY COMPLETE")
        print(f"{'='*60}")

        # Recalculate stats for all domains
        final_bad_total = 0
        final_areas_total = 0
        total_domain_bytes = 0
        total_rescued_bytes = 0

        for name, path, use_loose in domains:
            if path.exists():
                bad, areas, total, pct = self._get_domain_bad_sectors(path)
                final_bad_total += bad
                final_areas_total += areas
                total_domain_bytes += total
                total_rescued_bytes += total - bad

        # Calculate both percentages
        allocated_pct = (total_rescued_bytes / total_domain_bytes * 100) if total_domain_bytes > 0 else 0
        whole_drive_pct = (total_rescued_bytes / self.drive_size * 100) if self.drive_size else 0

        print(f"\nRecovery Summary:")
        print(f"  Allocated data recovered: {total_rescued_bytes/(1024**3):.2f} GB of {total_domain_bytes/(1024**3):.2f} GB")

        # Never say 100% if there are bad sectors
        if final_bad_total == 0:
            print(f"  Allocated data status:    ✓ 100% COMPLETE")
        else:
            display_pct = min(allocated_pct, 99.99)
            print(f"  Allocated data status:    {display_pct:.2f}% ({final_bad_total:,} bytes unrecovered)")

        print(f"  Whole drive cloned:       {whole_drive_pct:.1f}% (includes free space we skipped)")
        print(f"  Remaining bad sectors:    {final_bad_total:,} bytes in {final_areas_total} areas")

        if final_bad_total == 0:
            print("\n✓ All file data fully recovered!")
        else:
            print(f"\nThese {final_areas_total} bad areas may be permanently unreadable.")

        return True

    def _load_domain_regions(self, domain_path):
        """Load regions from a domain file"""
        regions = []
        with open(domain_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                parts = line.split()
                if len(parts) >= 2 and parts[0].startswith('0x') and parts[1].startswith('0x'):
                    try:
                        start = int(parts[0], 16)
                        size = int(parts[1], 16)
                        regions.append((start, size))
                    except ValueError:
                        continue
        return regions

    # =========================================================================
    # Visualization Support: Extract Catalog File
    # =========================================================================
    def extract_catalog_for_visualization(self):
        """Extract the HFS+ Catalog file (File ID 4) for visualization.

        The Catalog B-tree contains file/folder names, parent references,
        and extent information needed to map blocks to files in the visualizer.

        This should be called after Stage 5 when the catalog data has been recovered.
        """
        print("\n" + "="*60)
        print("EXTRACTING CATALOG FOR VISUALIZATION")
        print("="*60)

        dest_partition = self.state.get("dest_partition")
        if not dest_partition:
            dest_partition = self._get_dest_partition()

        if not dest_partition:
            print("ERROR: Could not determine destination partition")
            return False

        catalog_path = self.job_dir / "catalog.raw"
        print(f"Destination partition: {dest_partition}")
        print(f"Output: {catalog_path}")

        # Extract Catalog file (File ID 4) from destination
        cmd = f"icat -f hfs {dest_partition} 4 > {catalog_path}"
        print(f"Command: {cmd}")
        result = os.system(cmd)

        if result != 0:
            print(f"WARNING: icat returned non-zero: {result}")
            # Try to verify if file was created anyway
            if not catalog_path.exists() or catalog_path.stat().st_size == 0:
                print("ERROR: Failed to extract Catalog file")
                print("The catalog blocks may need more recovery.")
                return False

        if catalog_path.exists():
            size = catalog_path.stat().st_size
            print(f"Extracted: {size:,} bytes ({size/(1024**2):.1f} MB)")

            # Verify we got valid B-tree data (check for node descriptor)
            with open(catalog_path, 'rb') as f:
                header = f.read(32)
                if len(header) >= 14:
                    # B-tree header node has specific structure
                    # First node is header node (kind = 1)
                    node_kind = header[8] if len(header) > 8 else 0
                    if node_kind == 1:
                        print("Verified: Valid B-tree header node found")
                    else:
                        print(f"Note: First node kind = {node_kind} (expected 1 for header)")

            self.state["catalog_extracted"] = True
            self.state["catalog_path"] = str(catalog_path)
            self.save_state()
            return True
        else:
            print("ERROR: Catalog file not created")
            return False

    def validate_destination_size(self, required_bytes):
        """Check if destination has enough space"""
        if self.dest_size is None:
            print("WARNING: Destination size unknown, cannot validate")
            return True

        if self.dest_size < required_bytes:
            shortfall = required_bytes - self.dest_size
            print(f"\nERROR: Destination too small!")
            print(f"  Required: {required_bytes/(1024**3):.2f} GB")
            print(f"  Available: {self.dest_size/(1024**3):.2f} GB")
            print(f"  Shortfall: {shortfall/(1024**3):.2f} GB")
            return False

        headroom = self.dest_size - required_bytes
        print(f"Destination size OK: {self.dest_size/(1024**3):.2f} GB available, {required_bytes/(1024**3):.2f} GB needed ({headroom/(1024**3):.2f} GB headroom)")
        return True

    # =========================================================================
    # Main workflow
    # =========================================================================
    def run(self):
        """Run the full iterative recovery workflow"""
        print("="*60)
        print("HFS+ ITERATIVE TARGETED RECOVERY")
        print("="*60)
        print(f"Source: {self.source}")
        print(f"Destination: {self.dest}")
        print(f"Log: {self.log_file}")
        print(f"Job directory: {self.job_dir}")
        print()

        # Check if this is a resume
        is_resume = "source_identity" in self.state or "dest_identity" in self.state

        if is_resume:
            print("Resuming previous recovery job - validating drives...")

            if "source_identity" in self.state:
                if not self._validate_drive_identity(self.source, self.state["source_identity"], "SOURCE"):
                    return False

            if "dest_identity" in self.state:
                if not self._validate_drive_identity(self.dest, self.state["dest_identity"], "DESTINATION"):
                    return False

            # Restore state
            if "block_size" in self.state:
                self.block_size = self.state["block_size"]
            if "partition_offset" in self.state:
                self.partition_offset = self.state["partition_offset"]
            if "hfs_partition" in self.state:
                self.hfs_partition = self.state["hfs_partition"]

            print()
        else:
            print("New recovery job - recording drive identities...")
            self.state["source_identity"] = self._get_drive_identity(self.source)
            self.state["dest_identity"] = self._get_drive_identity(self.dest)

            src_id = self.state["source_identity"]
            dst_id = self.state["dest_identity"]

            print(f"  Source:  {src_id.get('model', 'unknown')} / {src_id.get('serial', 'unknown')}")
            print(f"  Dest:    {dst_id.get('model', 'unknown')} / {dst_id.get('serial', 'unknown')}")

            self.save_state()
            print()

        # Stage 0 (bootable-first mode): Critical structures BEFORE data recovery
        if self.bootable_first and not self.state.get("critical_structures_recovered"):
            print("\n" + "="*60)
            print("Stage 0 (bootable-first): Critical Disk Structures")
            print("="*60)
            print("Recovering GPT, EFI, boot blocks BEFORE data recovery...")
            if not self.stage0_critical_structures():
                print("WARNING: Critical structures incomplete - continuing with data recovery anyway")
            else:
                print("Critical structures recovered successfully")

        # Stage 1: Volume Header
        if not self.state.get("volume_header_parsed"):
            if not self.stage1_volume_header():
                print("FAILED at Stage 1: Volume Header")
                return False
        else:
            print("Stage 1 already complete, loading saved state...")
            self.block_size = self.state["block_size"]
            self.partition_offset = self.state["partition_offset"]
            self.hfs_partition = self.state["hfs_partition"]

        # Stage 2: Allocation File
        if not self.state.get("alloc_file_extracted"):
            if not self.stage2_allocation_file():
                print("FAILED at Stage 2: Allocation File")
                return False
        else:
            print("Stage 2 already complete, allocation file extracted")

        # Stage 3: Parse Allocation File
        if not self.state.get("allocated_ranges_count"):
            if not self.stage3_parse_allocation():
                print("FAILED at Stage 3: Parse Allocation")
                return False
        else:
            print(f"Stage 3 already complete, {self.state['allocated_ranges_count']} ranges found")
            # Reload allocated clusters from domain file if needed
            if not self.allocated_clusters:
                domain_path = self.job_dir / "all_data_domain.txt"
                if domain_path.exists():
                    self.allocated_clusters = []
                    with open(domain_path) as f:
                        for line in f:
                            line = line.strip()
                            if not line or line.startswith('#'):
                                continue
                            parts = line.split()
                            # Domain file format: 0xPOS  0xSIZE  STATUS
                            # Skip header line and only parse data lines
                            if len(parts) >= 3 and parts[0].startswith('0x') and parts[1].startswith('0x'):
                                try:
                                    start = int(parts[0], 16)
                                    size = int(parts[1], 16)
                                    self.allocated_clusters.append((start, size, 0, 0))
                                except ValueError:
                                    continue

        # Stage 4: Create data domain
        if not self.state.get("data_domain_created"):
            if not self.stage4_create_data_domain():
                print("FAILED at Stage 4: Create data domain")
                return False

        # Validate destination size
        total_allocated = self.state.get("total_allocated_bytes", 0)
        if total_allocated > 0:
            if not self.validate_destination_size(total_allocated):
                print("\nERROR: Destination is too small for the allocated data.")
                return False

        # Stage 5: Recover all data
        print("\n" + "="*60)
        print("Ready for Stage 5: Full Data Recovery")
        print("="*60)

        options = {
            'y': "Yes, proceed with full data recovery",
            'n': "No, skip data recovery"
        }
        choice = self.prompt_with_timeout(
            "Proceed with full data recovery?",
            options,
            'y'
        )

        if choice != 'n':
            self.stage5_recover_data()

            # Offer Stage 6: Aggressive retry
            self.stage6_aggressive_retry()

        # Stage 0: Critical disk structures (GPT, EFI, boot blocks, alt VH)
        # Normally done LAST to prioritize actual data recovery, but --bootable-first runs it earlier
        if not self.state.get("critical_structures_recovered"):
            # Check device exists before attempting critical structure recovery
            if not self.check_device_exists(self.source):
                print("\nWARNING: Source device no longer accessible - skipping critical structure recovery")
                print("The destination may need manual partition table repair.")
            else:
                print("\nRecovering critical disk structures for destination mountability...")
                if not self.stage0_critical_structures():
                    print("WARNING: Critical structures incomplete - destination may need partition repair")
        elif not self.bootable_first:
            # Only print "already recovered" if we didn't just do it in bootable-first mode
            print("\nCritical disk structures already recovered")

        # Extract catalog for visualization support
        if not self.state.get("catalog_extracted"):
            print("\nExtracting catalog for visualization support...")
            self.extract_catalog_for_visualization()
        else:
            print(f"\nCatalog already extracted: {self.state.get('catalog_path')}")

        print("\n" + "="*60)
        print("WORKFLOW COMPLETE")
        print("="*60)
        print("Destination should now be mountable when connected to another system.")

        # Show visualization hint
        catalog_path = self.job_dir / "catalog.raw"
        if catalog_path.exists():
            print(f"\nVisualization support files available:")
            print(f"  Catalog: {catalog_path}")
            print(f"\nTo generate visualization:")
            print(f"  python3 visualizer/visualize.py --job {self.job_dir}")

        # Offer to continue cloning remaining areas
        self.offer_remaining_clone()

        return True

    def offer_remaining_clone(self):
        """Offer to clone areas not covered by targeted recovery."""
        if not self.check_device_exists(self.source):
            print("\nSource device no longer accessible - cannot continue cloning.")
            return

        # Calculate what's been recovered vs total drive size
        try:
            drive_size = int(subprocess.check_output(
                ['blockdev', '--getsize64', self.source],
                stderr=subprocess.DEVNULL
            ).strip())
        except (subprocess.CalledProcessError, ValueError):
            print("\nCould not determine drive size - skipping continuation offer.")
            return

        # Parse the log to see what's been recovered
        recovered = 0
        if os.path.exists(str(self.log_file)):
            try:
                with open(str(self.log_file), 'r') as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith('#') or not line:
                            continue
                        parts = line.split()
                        if len(parts) == 3 and parts[2] == '+':
                            recovered += int(parts[1], 16)
            except Exception:
                pass

        remaining = drive_size - recovered
        if remaining <= 0:
            print("\nEntire drive has been cloned - nothing remaining.")
            return

        pct_done = (recovered / drive_size) * 100
        remaining_gb = remaining / (1024**3)

        print(f"\n{'='*60}")
        print(f"CONTINUE CLONING?")
        print(f"{'='*60}")
        print(f"Targeted recovery covered {pct_done:.1f}% of the drive.")
        print(f"{remaining_gb:.1f} GB remains uncloned.")
        print(f"")
        print(f"Options:")
        print(f"  1. Clone remaining areas (other partitions + free space)")
        print(f"     Useful for: deleted file recovery, other partitions,")
        print(f"     corrupt partition table, or just getting everything")
        print(f"  2. Skip - done with this recovery")
        print(f"")

        try:
            choice = input("Continue cloning remaining areas? [1/2, default=2]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nSkipped.")
            return

        if choice != '1':
            print("Skipped.")
            return

        print(f"\nCloning remaining {remaining_gb:.1f} GB...")
        print(f"Using existing log file - ddrescue will skip already-recovered areas.")
        print(f"Command: ddrescue -d -f {self.source} {self.dest} {self.log_file}")
        print()

        try:
            result = subprocess.run(
                ['ddrescue', '-d', '-f', self.source, self.dest, str(self.log_file)],
                check=False
            )
            if result.returncode == 0:
                print(f"\nFull clone completed successfully!")
            else:
                print(f"\nClone finished with exit code {result.returncode}")
        except KeyboardInterrupt:
            print(f"\nClone interrupted - progress saved in log file.")
            print(f"Resume with: ddrescue -d -f {self.source} {self.dest} {self.log_file}")


def main():
    parser = argparse.ArgumentParser(
        description='Iterative targeted HFS+ recovery with bootstrapped workflow',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  # Standard recovery (data-first, critical structures last)
  iterative-targeted-recovery-hfs.py /dev/sdb /dev/sdf recovery.log ./job_mac

  # Bootable-first (recover GPT/EFI/boot blocks BEFORE data recovery)
  iterative-targeted-recovery-hfs.py --bootable-first /dev/sdb /dev/sdf recovery.log ./job_mac
'''
    )

    parser.add_argument('source', help='Source device (e.g., /dev/sdb)')
    parser.add_argument('dest', help='Destination device (e.g., /dev/sdf)')
    parser.add_argument('log', help='DDRescue log file path')
    parser.add_argument('job_dir', nargs='?', default='./recovery_job_hfs',
                        help='Job directory for state files (default: ./recovery_job_hfs)')
    parser.add_argument('--bootable-first', '-b', action='store_true',
                        help='Recover critical disk structures (GPT, EFI, boot blocks) BEFORE data recovery')

    args = parser.parse_args()

    recovery = HFSTargetedRecovery(
        args.source,
        args.dest,
        args.log,
        args.job_dir,
        bootable_first=args.bootable_first
    )
    success = recovery.run()

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
