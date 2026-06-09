#!/usr/bin/env python3
"""
iterative-targeted-recovery.py - Bootstrapped NTFS Targeted Recovery

This script implements an iterative recovery workflow optimized for data-first recovery:

1. Recover boot sector → parse → get MFT location
2. Recover MFT header + $MFTMirr → parse → get $Bitmap data runs
3. Recover $Bitmap → parse → get all allocated clusters
4. Create data recovery domain
5. Recover all data clusters (THE MAIN EVENT)
6. Aggressive retry on failed regions
7. (Optional) Recover unknown regions
0. Recover critical disk structures (GPT, EFI, backup boot sector) → mountable destination

Stage 0 runs LAST by default to prioritize actual file data, but ensures destination is
immediately mountable when plugged into another system.

Use --bootable-first (-b) to run Stage 0 BEFORE data recovery (useful for aggressive
retry on critical structures or when mountability is more important than data).

Each step verifies the previous data is complete before proceeding.

Usage: sudo python3 iterative-targeted-recovery.py [--bootable-first] <source> <dest> <log> [job_dir]
"""

import sys
import os
import subprocess
import struct
import json
import select
import time
import argparse
import bisect
import signal
import re
from pathlib import Path

# Default timeout for prompts (in seconds)
DEFAULT_PROMPT_TIMEOUT = 300  # 5 minutes

# Priority recovery presets — defines which user folders to recover first.
# Inspired by rsync-recovery/recovery_presets.sh but expressed as Python dicts
# so the MFT scanner can consume them directly.
PRIORITY_PRESETS = {
    'family': {
        'name': 'Family/Personal (Photos, Videos, Documents)',
        'tier1_dirs': ['Desktop', 'Documents', 'Pictures', 'Videos', 'Music', 'Downloads'],
        'tier2_dirs': ['OneDrive', 'Google Drive', 'iCloudDrive', 'Dropbox', 'Box'],
        'skip_dirs': ['AppData', 'Application Data', 'Local Settings'],
    },
    'photographer': {
        'name': 'Photographer (Exports/Finals, then RAW)',
        'tier1_dirs': ['Desktop', 'Documents', 'Pictures', 'Exports', 'Finals', 'Delivered', 'Clients', 'Lightroom', 'Capture One', 'Selects'],
        'tier2_dirs': ['OneDrive', 'Google Drive', 'Dropbox'],
        'skip_dirs': ['AppData', 'Application Data', 'Cache', 'Previews', 'Thumbnails'],
    },
    'business': {
        'name': 'Business/Office (Documents, Financials)',
        'tier1_dirs': ['Desktop', 'Documents', 'QuickBooks', 'Financial', 'Contracts', 'Invoices'],
        'tier2_dirs': ['OneDrive', 'SharePoint', 'Dropbox', 'Google Drive'],
        'skip_dirs': ['AppData', 'Application Data', 'Downloads'],
    },
    'gamer': {
        'name': 'Gamer (Saves, Recordings)',
        'tier1_dirs': ['Saved Games', 'Desktop', 'Documents', 'Videos', 'Recordings'],
        'tier2_dirs': ['OneDrive', 'Dropbox'],
        'skip_dirs': ['AppData', 'Application Data'],
    },
    'default': {
        'name': 'Default (Desktop, Documents, Pictures)',
        'tier1_dirs': ['Desktop', 'Documents', 'Pictures', 'Videos', 'Downloads'],
        'tier2_dirs': ['OneDrive', 'Google Drive', 'Dropbox', 'iCloudDrive'],
        'skip_dirs': ['AppData', 'Application Data'],
    },
}

class TargetedRecovery:
    def __init__(self, source, dest, log_file, job_dir, bootable_first=False):
        self.source = source
        self.dest = dest
        self.log_file = log_file
        self.job_dir = Path(job_dir)
        self.job_dir.mkdir(parents=True, exist_ok=True)
        self.bootable_first = bootable_first

        # Will be populated as we discover them
        self.sector_size = 512
        self.cluster_size = None
        self.mft_cluster = None
        self.mft_byte_offset = None
        self.mft_mirr_cluster = None
        self.mft_mirr_byte_offset = None
        self.bitmap_data_runs = []
        self.allocated_clusters = []
        self.unknown_cluster_ranges = []

        # State file to track progress
        self.state_file = self.job_dir / "recovery_state.json"
        self.state = self.load_state()

        # Restore cached values from state
        if "mft_mirr_byte_offset" in self.state:
            self.mft_mirr_byte_offset = self.state["mft_mirr_byte_offset"]
            self.mft_mirr_cluster = self.state.get("mft_mirr_cluster")
        if "mft_byte_offset" in self.state:
            self.mft_byte_offset = self.state["mft_byte_offset"]
            self.mft_cluster = self.state.get("mft_cluster")
        if "cluster_size" in self.state:
            self.cluster_size = self.state["cluster_size"]

        # Get drive size (must be after other init)
        self.drive_size = self._detect_drive_size()

        # Detect partition offset (or use saved value from state)
        if "partition_offset" in self.state:
            self.partition_offset = self.state["partition_offset"]
            print(f"Using saved partition offset: {self.partition_offset} sectors")
        else:
            self.partition_offset = self._detect_partition_offset()

        # Detect destination size for validation
        self.dest_size = self._detect_dest_size()

    def _detect_drive_size(self):
        """Get the size of the source drive in bytes"""
        try:
            # Try blockdev first
            cmd = f"blockdev --getsize64 {self.source}"
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            if result.returncode == 0:
                size = int(result.stdout.strip())
                print(f"Detected source drive size: {size/(1024**4):.2f} TB ({size:,} bytes)")
                return size
        except:
            pass

        try:
            # Fall back to reading /sys/block
            device = os.path.basename(self.source)
            with open(f"/sys/block/{device}/size") as f:
                sectors = int(f.read().strip())
                size = sectors * 512
                print(f"Detected source drive size: {size/(1024**4):.2f} TB ({size:,} bytes)")
                return size
        except:
            pass

        # Default to 2TB if we can't detect
        print("WARNING: Could not detect drive size, assuming 2TB")
        return 2 * 1000 * 1000 * 1000 * 1000  # 2TB in bytes

    def _detect_partition_offset(self):
        """
        Detect NTFS partition offset by parsing MBR or GPT.
        Returns offset in sectors.
        """
        print("Detecting partition offset...")

        # Try to read first two sectors from source (for MBR + GPT header check)
        try:
            cmd = f"dd if={self.source} bs=512 count=2 2>/dev/null"
            result = subprocess.run(cmd, shell=True, capture_output=True)
            if len(result.stdout) < 512:
                print("WARNING: Could not read MBR from source, using default offset 2048")
                return 2048
            mbr = result.stdout[:512]
        except Exception as e:
            print(f"WARNING: Error reading MBR: {e}, using default offset 2048")
            return 2048

        # If the source device IS already a partition, its first sector is the NTFS VBR
        # (not an MBR/GPT). Detect this by checking for the NTFS OEM ID at bytes 3-10.
        # The NTFS VBR also ends with 0x55 0xAA, so without this check the code below
        # would read partition table entries out of NTFS BPB data and default to 2048.
        if mbr[3:11] == b'NTFS    ':
            print("  Source is an NTFS partition device — NTFS VBR at offset 0, using offset 0")
            return 0

        # Check MBR signature (last 2 bytes should be 0x55 0xAA)
        if mbr[510:512] != b'\x55\xaa':
            print("WARNING: Invalid MBR signature, using default offset 2048")
            return 2048

        # Check if this is a protective MBR (GPT)
        # Partition type 0xEE in first partition entry indicates GPT
        first_partition_type = mbr[450]  # Offset 446 + 4 (type is at offset 4 in entry)

        if first_partition_type == 0xEE:
            # GPT disk - parse GPT partition table
            return self._parse_gpt_for_ntfs()
        else:
            # MBR disk - parse MBR partition table
            return self._parse_mbr_for_ntfs(mbr)

    def _parse_mbr_for_ntfs(self, mbr):
        """Parse MBR partition table to find NTFS partition (type 0x07)"""
        print("  Parsing MBR partition table...")

        # MBR partition table starts at offset 446, each entry is 16 bytes
        # Entry format: boot(1) + CHS_start(3) + type(1) + CHS_end(3) + LBA_start(4) + sectors(4)
        ntfs_partitions = []

        for i in range(4):
            entry_offset = 446 + (i * 16)
            entry = mbr[entry_offset:entry_offset + 16]

            if len(entry) < 16:
                continue

            partition_type = entry[4]
            lba_start = struct.unpack('<I', entry[8:12])[0]
            sector_count = struct.unpack('<I', entry[12:16])[0]

            if partition_type == 0x00:
                continue  # Empty entry

            type_name = {
                0x07: "NTFS/HPFS/exFAT",
                0x0B: "FAT32 (CHS)",
                0x0C: "FAT32 (LBA)",
                0x0E: "FAT16 (LBA)",
                0x83: "Linux",
                0x82: "Linux swap",
                0xEE: "GPT Protective",
            }.get(partition_type, f"0x{partition_type:02X}")

            size_gb = (sector_count * 512) / (1024**3)
            print(f"    Partition {i+1}: type={type_name}, start={lba_start}, size={size_gb:.1f}GB")

            if partition_type == 0x07:  # NTFS/HPFS/exFAT
                ntfs_partitions.append((lba_start, sector_count, i+1))

        if not ntfs_partitions:
            print("  WARNING: No NTFS partition found in MBR, using default offset 2048")
            return 2048

        if len(ntfs_partitions) == 1:
            offset = ntfs_partitions[0][0]
            print(f"  Found NTFS partition at sector {offset}")
            return offset

        # Multiple NTFS partitions - pick the largest (most likely to be data)
        largest = max(ntfs_partitions, key=lambda x: x[1])
        print(f"  Multiple NTFS partitions found, using largest (partition {largest[2]}) at sector {largest[0]}")
        return largest[0]

    def _parse_gpt_for_ntfs(self):
        """Parse GPT partition table to find NTFS partition"""
        print("  Parsing GPT partition table...")

        # GPT header is at LBA 1, partition entries typically start at LBA 2
        # Read GPT header and partition entries
        try:
            # Read more sectors to get partition entries (usually 128 entries * 128 bytes = 32 sectors)
            cmd = f"dd if={self.source} bs=512 skip=1 count=33 2>/dev/null"
            result = subprocess.run(cmd, shell=True, capture_output=True)
            gpt_data = result.stdout
        except Exception as e:
            print(f"  WARNING: Error reading GPT: {e}, using default offset 2048")
            return 2048

        if len(gpt_data) < 512:
            print("  WARNING: Could not read GPT header, using default offset 2048")
            return 2048

        # GPT header
        gpt_header = gpt_data[:512]

        # Verify GPT signature "EFI PART"
        if gpt_header[:8] != b'EFI PART':
            print("  WARNING: Invalid GPT signature, using default offset 2048")
            return 2048

        # Get partition entry info from header
        partition_entry_lba = struct.unpack('<Q', gpt_header[72:80])[0]
        num_entries = struct.unpack('<I', gpt_header[80:84])[0]
        entry_size = struct.unpack('<I', gpt_header[84:88])[0]

        print(f"    GPT: {num_entries} partition entries, entry size {entry_size} bytes")

        # GUID for Microsoft Basic Data (NTFS, FAT, etc.)
        # {EBD0A0A2-B9E5-4433-87C0-68B6B72699C7}
        MS_BASIC_DATA_GUID = bytes([
            0xA2, 0xA0, 0xD0, 0xEB, 0xE5, 0xB9, 0x33, 0x44,
            0x87, 0xC0, 0x68, 0xB6, 0xB7, 0x26, 0x99, 0xC7
        ])

        # Partition entries start after header (typically at LBA 2, which is offset 512 in our read)
        entries_start = 512  # We skipped LBA 0, so LBA 2 is at offset 512 in gpt_data
        ntfs_partitions = []

        for i in range(min(num_entries, 128)):  # Limit to 128 for safety
            entry_offset = entries_start + (i * entry_size)
            if entry_offset + entry_size > len(gpt_data):
                break

            entry = gpt_data[entry_offset:entry_offset + entry_size]

            # Partition type GUID is first 16 bytes
            type_guid = entry[:16]

            if type_guid == b'\x00' * 16:
                continue  # Empty entry

            # Starting LBA at offset 32 (8 bytes)
            start_lba = struct.unpack('<Q', entry[32:40])[0]
            # Ending LBA at offset 40 (8 bytes)
            end_lba = struct.unpack('<Q', entry[40:48])[0]

            size_gb = ((end_lba - start_lba + 1) * 512) / (1024**3)

            # Check if it's Microsoft Basic Data
            if type_guid == MS_BASIC_DATA_GUID:
                print(f"    Partition {i+1}: Microsoft Basic Data, start={start_lba}, size={size_gb:.1f}GB")
                ntfs_partitions.append((start_lba, end_lba - start_lba + 1, i+1))
            else:
                # Print other partitions for reference
                guid_str = type_guid.hex()
                print(f"    Partition {i+1}: GUID={guid_str[:8]}..., start={start_lba}, size={size_gb:.1f}GB")

        if not ntfs_partitions:
            print("  WARNING: No Microsoft Basic Data partition found in GPT, using default offset 2048")
            return 2048

        if len(ntfs_partitions) == 1:
            offset = ntfs_partitions[0][0]
            print(f"  Found NTFS-compatible partition at sector {offset}")
            return offset

        # Multiple partitions - pick the largest
        largest = max(ntfs_partitions, key=lambda x: x[1])
        print(f"  Multiple data partitions found, using largest (partition {largest[2]}) at sector {largest[0]}")
        return largest[0]

    def _get_drive_identity(self, device):
        """
        Get identifying information for a drive to verify on resume.
        Returns dict with serial, model, size, and path.
        """
        identity = {
            "path": device,
            "serial": None,
            "model": None,
            "size": None
        }

        # Get serial and model from udevadm
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

        # Get size
        try:
            cmd = f"blockdev --getsize64 {device}"
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            if result.returncode == 0:
                identity["size"] = int(result.stdout.strip())
        except:
            pass

        # Fallback: try /sys/block
        if not identity["serial"] or not identity["model"]:
            try:
                dev_name = os.path.basename(device)
                sys_path = f"/sys/block/{dev_name}/device"
                if os.path.exists(f"{sys_path}/serial"):
                    with open(f"{sys_path}/serial") as f:
                        identity["serial"] = f.read().strip()
                if os.path.exists(f"{sys_path}/model"):
                    with open(f"{sys_path}/model") as f:
                        identity["model"] = f.read().strip()
            except:
                pass

        return identity

    def _validate_drive_identity(self, device, saved_identity, drive_type):
        """
        Validate that a drive matches saved identity from previous run.
        Returns True if OK to proceed, False to abort.
        """
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

        # Check for mismatches
        mismatches = []

        if saved_identity.get("serial") and current["serial"]:
            if saved_identity["serial"] != current["serial"]:
                mismatches.append(f"Serial mismatch: {current['serial']} vs {saved_identity['serial']}")

        if saved_identity.get("model") and current["model"]:
            if saved_identity["model"] != current["model"]:
                mismatches.append(f"Model mismatch: {current['model']} vs {saved_identity['model']}")

        if saved_identity.get("size") and current["size"]:
            # Allow small size differences (partition table variations)
            size_diff = abs(saved_identity["size"] - current["size"])
            if size_diff > 1024 * 1024 * 1024:  # >1GB difference
                mismatches.append(f"Size mismatch: {current['size']/(1024**3):.1f}GB vs {saved_identity['size']/(1024**3):.1f}GB")

        if not mismatches:
            print(f"  ✓ {drive_type} drive identity verified")
            return True

        # Mismatches found - warn and ask
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
            'a',  # Default to abort for safety
            timeout=60  # Shorter timeout, default to safe option
        )

        if choice == 'c':
            print(f"  User confirmed: proceeding with current {drive_type.lower()} drive")
            return True
        else:
            print(f"  Aborting due to {drive_type.lower()} drive mismatch")
            return False

    def _detect_dest_size(self):
        """Get the size of the destination drive/file in bytes"""
        try:
            # Try blockdev first (works for block devices)
            cmd = f"blockdev --getsize64 {self.dest}"
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            if result.returncode == 0:
                size = int(result.stdout.strip())
                print(f"Detected destination size: {size/(1024**4):.2f} TB ({size:,} bytes)")
                return size
        except:
            pass

        try:
            # For image files, report free space on parent (not sparse logical size)
            import os, stat as _stat
            if os.path.isfile(self.dest) and not _stat.S_ISBLK(os.stat(self.dest).st_mode):
                parent = os.path.dirname(self.dest) or '.'
                sv = os.statvfs(parent)
                free = sv.f_bavail * sv.f_frsize
                actual = os.stat(self.dest).st_blocks * 512
                available = free + actual
                print(f"Detected destination capacity: {available/(1024**3):.2f} GB (image file on {parent})")
                return available
        except:
            pass

        try:
            # Fall back to reading /sys/block
            device = os.path.basename(self.dest)
            with open(f"/sys/block/{device}/size") as f:
                sectors = int(f.read().strip())
                size = sectors * 512
                print(f"Detected destination size: {size/(1024**4):.2f} TB ({size:,} bytes)")
                return size
        except:
            pass

        print("WARNING: Could not detect destination size")
        return None

    def validate_destination_size(self, required_bytes):
        """
        Check if destination has enough space for the recovery.
        Returns True if OK, False if not enough space.
        """
        import stat as _stat

        # For image file destinations, capacity = free space on parent filesystem
        # plus actual disk blocks already allocated by the partial file.
        # (os.path.getsize returns sparse logical size, not actual disk usage.)
        try:
            dest_is_file = False
            if os.path.exists(self.dest):
                dest_is_file = not _stat.S_ISBLK(os.stat(self.dest).st_mode)
            elif os.path.isdir(os.path.dirname(self.dest)):
                dest_is_file = True

            if dest_is_file:
                parent = os.path.dirname(self.dest) or '.'
                sv = os.statvfs(parent)
                free = sv.f_bavail * sv.f_frsize
                actual_used = os.stat(self.dest).st_blocks * 512 if os.path.exists(self.dest) else 0
                available = free + actual_used
                if available < required_bytes:
                    shortfall = required_bytes - available
                    print(f"\nERROR: Destination too small!")
                    print(f"  Required: {required_bytes/(1024**3):.2f} GB")
                    print(f"  Available: {available/(1024**3):.2f} GB (free on {parent})")
                    print(f"  Shortfall: {shortfall/(1024**3):.2f} GB")
                    return False
                headroom = available - required_bytes
                print(f"Destination size OK: {available/(1024**3):.2f} GB available, {required_bytes/(1024**3):.2f} GB needed ({headroom/(1024**3):.2f} GB headroom)")
                return True
        except OSError:
            pass

        # Block device path
        if self.dest_size is None:
            print("WARNING: Destination size unknown, cannot validate")
            return True  # Proceed anyway

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

    def load_state(self):
        """Load recovery state from previous run"""
        if self.state_file.exists():
            with open(self.state_file) as f:
                return json.load(f)
        return {"stage": 0, "completed": []}

    def play_notification_sound(self):
        """Play a notification sound to alert user"""
        # When running under sudo, we need to run as the original user
        # with their XDG_RUNTIME_DIR to access PulseAudio/PipeWire
        sudo_user = os.environ.get('SUDO_USER') or os.environ.get('USER') or os.environ.get('LOGNAME') or ''
        uid = os.environ.get('SUDO_UID', '1000')

        sound_file = "/usr/share/sounds/freedesktop/stereo/alarm-clock-elapsed.oga"

        # Method 1: Run paplay as the original user via su (works when already root)
        try:
            cmd = f"su - {sudo_user} -c 'XDG_RUNTIME_DIR=/run/user/{uid} paplay {sound_file}'"
            subprocess.Popen(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except:
            pass

        # Method 2: Try pw-play (PipeWire) as original user
        try:
            cmd = f"su - {sudo_user} -c 'XDG_RUNTIME_DIR=/run/user/{uid} pw-play {sound_file}'"
            subprocess.Popen(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except:
            pass

        # Method 3: Desktop notification (visual fallback)
        try:
            cmd = f"su - {sudo_user} -c 'DISPLAY=:0 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus notify-send -u critical DDRescue \"Input needed!\"'"
            subprocess.run(cmd, shell=True, capture_output=True, timeout=5)
        except:
            pass

        # Fallback: terminal bell (multiple times to be noticeable)
        for _ in range(3):
            print('\a', end='', flush=True)
            time.sleep(0.2)

    def prompt_with_timeout(self, prompt, options, default, timeout=None):
        """
        Display a prompt with timeout and sound notification.

        Args:
            prompt: The prompt message to display
            options: Dict of {key: description} for valid options
            default: Default option to select on timeout
            timeout: Timeout in seconds (uses DEFAULT_PROMPT_TIMEOUT if None)

        Returns:
            The selected option key
        """
        if timeout is None:
            timeout = DEFAULT_PROMPT_TIMEOUT

        # Play notification sound
        self.play_notification_sound()

        # Display prompt with options
        print(f"\n{prompt}")
        for key, desc in options.items():
            default_marker = " (DEFAULT - auto-select in {:.0f}s)".format(timeout) if key == default else ""
            print(f"  [{key}] {desc}{default_marker}")

        start_time = time.time()

        # Use select for timeout on stdin
        choice_display = '/'.join(k.upper() if k == default else k for k in options.keys())
        print(f"\nChoice [{choice_display}]: ", end='', flush=True)

        while True:
            elapsed = time.time() - start_time
            remaining = timeout - elapsed

            if remaining <= 0:
                print(f"\n>> Timeout reached, auto-selecting '{default}'")
                return default

            # Check if input is available (with 1 second intervals for timeout updates)
            ready, _, _ = select.select([sys.stdin], [], [], min(1.0, remaining))

            if ready:
                choice = sys.stdin.readline().strip().lower()
                if choice in options or choice == '':
                    return choice if choice else default
                print(f"Invalid choice '{choice}'. Please enter one of: {', '.join(options.keys())}")
                print(f"\nChoice [{choice_display}]: ", end='', flush=True)
            else:
                # Update countdown display every 30 seconds
                if int(remaining) % 30 == 0 and int(remaining) != int(timeout):
                    print(f"\r  (Auto-selecting '{default}' in {int(remaining)}s) Choice [{'/'.join(options.keys())}]: ", end='', flush=True)

    def save_state(self):
        """Save recovery state"""
        with open(self.state_file, 'w') as f:
            json.dump(self.state, f, indent=2)

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

    def _total_recovered_bytes(self, regions):
        """Single log pass to count total recovered bytes across all regions.
        O((N+M)log(N+M)) vs calling check_region_recovered N times (O(N*M)).
        """
        if not os.path.exists(self.log_file):
            return 0

        log_entries = []
        with open(self.log_file, 'r') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                parts = line.split()
                if len(parts) >= 3 and parts[2] == '+':
                    try:
                        pos = int(parts[0], 16)
                        sz  = int(parts[1], 16)
                        if sz > 0:
                            log_entries.append((pos, pos + sz))
                    except ValueError:
                        pass

        if not log_entries:
            return 0

        log_entries.sort()
        domain = sorted((r[0], r[0] + r[1]) for r in regions)

        total = li = di = 0
        while li < len(log_entries) and di < len(domain):
            ls, le = log_entries[li]
            ds, de = domain[di]
            overlap = min(le, de) - max(ls, ds)
            if overlap > 0:
                total += overlap
            if le <= de:
                li += 1
            else:
                di += 1

        return total

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

    # =========================================================================
    # Drive-disconnect resilience: if the source or destination drops out during
    # ANY ddrescue run, stop ddrescue cleanly (mapfile preserved), wait for the
    # SAME drive (verified by serial) to return — even at a new /dev node — then
    # resume from the mapfile. Routes every ddrescue invocation through
    # _exec_ddrescue().
    # =========================================================================
    def _split_disk_partition(self, name):
        """Split a block-device basename into (disk, partition_suffix).
        sdd3 -> ('sdd','3'); sda -> ('sda',''); nvme0n1p3 -> ('nvme0n1','3')."""
        m = re.match(r'^(nvme\d+n\d+|mmcblk\d+)p(\d+)$', name)
        if m:
            return m.group(1), m.group(2)
        if re.match(r'^(nvme\d+n\d+|mmcblk\d+)$', name):
            return name, ''
        m = re.match(r'^([a-z]+)(\d*)$', name)
        if m:
            return m.group(1), m.group(2)
        return name, ''

    def _partition_path(self, disk_name, part_suffix):
        """Reattach a partition suffix to a disk name (nvme/mmc use a 'p' separator)."""
        if re.match(r'^(nvme\d+n\d+|mmcblk\d+)$', disk_name):
            return f"/dev/{disk_name}p{part_suffix}"
        return f"/dev/{disk_name}{part_suffix}"

    def _find_disk_by_serial(self, serial):
        """Scan whole-disk block devices for one whose serial matches. Returns name or None.
        This is how we confirm a reappeared drive is the SAME one, not a different drive
        that grabbed the old /dev node."""
        if not serial:
            return None
        try:
            for name in sorted(os.listdir('/sys/block')):
                if name.startswith(('loop', 'ram', 'dm-', 'sr', 'zram', 'md')):
                    continue
                ident = self._get_drive_identity(f"/dev/{name}")
                if ident.get('serial') and ident['serial'] == serial:
                    return name
        except OSError:
            pass
        return None

    def _resolve_device(self, original_path, saved_identity):
        """Return the CURRENT /dev path for the drive matching saved_identity, preserving
        any partition suffix from original_path. Returns the path, or None if it's not
        present. Never returns a node whose serial doesn't match the saved identity.
        For non-/dev paths (image files), returns the path iff it currently exists."""
        if not str(original_path).startswith('/dev/'):
            return original_path if os.path.exists(original_path) else None

        serial = (saved_identity or {}).get('serial')
        name = os.path.basename(original_path)
        disk, part = self._split_disk_partition(name)

        if not serial:
            # No serial recorded to verify against — only trust the original path if live.
            return original_path if self.check_device_exists(original_path) else None

        found_disk = self._find_disk_by_serial(serial)
        if not found_disk:
            return None
        path = self._partition_path(found_disk, part) if part else f"/dev/{found_disk}"
        return path if os.path.exists(path) else None

    def _dest_present(self):
        """Whether the destination is currently reachable (block device OR image file)."""
        if str(self.dest).startswith('/dev/'):
            return self.check_device_exists(self.dest)
        return os.path.exists(self.dest)

    def _relocate_devices_on_resume(self):
        """On resume, if the source/dest block device re-enumerated to a different /dev
        node, find it by its saved serial and update self.source / self.dest so the run
        attaches to the SAME physical drive at its new address. Leaves image-file dests and
        already-correct paths untouched; never repoints to a serial that doesn't match."""
        for attr, key, label in (("source", "source_identity", "SOURCE"),
                                  ("dest", "dest_identity", "DESTINATION")):
            ident = self.state.get(key)
            current = getattr(self, attr)
            serial = (ident or {}).get('serial')
            if not serial or not str(current).startswith('/dev/'):
                continue
            # Already the right drive at the current path? Leave it.
            if self.check_device_exists(current):
                here = self._get_drive_identity(current)
                if here.get('serial') == serial:
                    continue
            # Otherwise hunt for the saved serial on another node.
            resolved = self._resolve_device(current, ident)
            if resolved and resolved != current:
                print(f"  {label} moved {current} → {resolved} (matched by serial) — using new node")
                setattr(self, attr, resolved)

    def _wait_for_drives(self):
        """Block until BOTH source and dest are present AND identity-verified, updating
        self.source / self.dest if a node moved on re-enumeration. Waits indefinitely;
        Ctrl-C aborts. Returns True when ready, False if the user aborted."""
        print("\n  ⏳ Waiting for drive(s) to reconnect — will verify by serial before resuming.")
        print("     (Ctrl-C to abort; the mapfile is already saved and you can resume later.)")
        waited = 0
        try:
            while True:
                src = self._resolve_device(self.source, self.state.get('source_identity'))
                if src and src != self.source:
                    print(f"  ↳ SOURCE is back at {src} (was {self.source}) — serial verified, updating path")
                    self.source = src

                dst = self._resolve_device(self.dest, self.state.get('dest_identity'))
                if dst and dst != self.dest:
                    print(f"  ↳ DESTINATION is back at {dst} (was {self.dest}) — serial verified, updating path")
                    self.dest = dst

                missing = []
                if not src:
                    missing.append('source')
                if not dst:
                    missing.append('destination')

                if not missing:
                    time.sleep(2)  # let the kernel finish enumerating partitions
                    print("  ✓ Drive(s) verified present — resuming ddrescue from mapfile.\n")
                    return True

                time.sleep(3)
                waited += 3
                if waited % 15 == 0:
                    print(f"  ...still waiting for {', '.join(missing)} ({waited}s elapsed)")
        except KeyboardInterrupt:
            print("\n  Aborted by user. Mapfile is saved — re-run and resume to continue.")
            self.state["device_disappeared"] = True
            self.save_state()
            return False

    def _stop_ddrescue_clean(self, proc):
        """Stop a running ddrescue so it flushes its mapfile, using only SIGINT/SIGTERM.
        Never SIGKILL — a hard kill mid-mapfile-write is the one thing that could corrupt
        the log. If the process is stuck on dead-drive I/O it will exit and save once the
        kernel times out that I/O; we keep waiting rather than force-killing."""
        for sig in (signal.SIGINT, signal.SIGINT, signal.SIGTERM):
            try:
                proc.send_signal(sig)
            except ProcessLookupError:
                return
            try:
                proc.wait(timeout=30)
                return
            except subprocess.TimeoutExpired:
                print("    (ddrescue still finishing its current I/O — waiting for it to save the mapfile...)")
        # Still alive (likely blocked in uninterruptible I/O on the dead drive).
        # Wait it out; it will save and exit when the kernel errors the I/O. No SIGKILL.
        try:
            proc.wait()
        except KeyboardInterrupt:
            # Don't leave the user staring at a silent hang. ddrescue already has its
            # stop signal and is flushing the mapfile; force-killing now risks a partial
            # write, so we keep waiting (mapfile integrity > responsiveness).
            print("\n  ddrescue is still flushing its mapfile after the disconnect — this can")
            print("  take up to a minute or two while the kernel times out the dead drive's I/O.")
            print("  Waiting for it to finish so the mapfile is never left partial...")
            proc.wait()

    def _exec_ddrescue(self, flags, domain=None, quiet=False):
        """Run ddrescue, surviving mid-run disconnects of either drive.

        flags  : list of ddrescue option flags, e.g. ['-f','-d','-A','-r3','-M','-L']
        domain : optional mapfile/domain path passed via -m
        Builds : ddrescue <flags> [-m <domain>] <source> <dest> <log_file>

        On a source/dest drop: stop ddrescue cleanly (mapfile saved), wait for the SAME
        drives to return (verified by serial; paths auto-updated), then resume. Returns
        ddrescue's exit code (0/1 = ok, 2 = fatal), or 130 if the user aborted.
        """
        if not self.check_device_exists(self.source):
            print(f"\nERROR: Source device {self.source} is not accessible!")
            print("The drive may have disconnected or failed.")
            return 2

        # Pre-flight dest check so a missing destination at startup is reported as a real
        # error, not misread as a hot-disconnect. Block devices must be present now; image
        # files may not exist yet (ddrescue creates them) but their parent dir must exist.
        if str(self.dest).startswith('/dev/'):
            dest_ok = self.check_device_exists(self.dest)
        else:
            dest_ok = os.path.exists(self.dest) or os.path.isdir(os.path.dirname(self.dest) or '.')
        if not dest_ok:
            print(f"\nERROR: Destination {self.dest} is not accessible!")
            print("The drive may have disconnected, or the image's folder is not mounted.")
            return 2

        while True:
            cmd = ['ddrescue'] + list(flags)
            if domain is not None:
                cmd += ['-m', str(domain)]
            cmd += [self.source, self.dest, str(self.log_file)]

            if not quiet:
                print(f"Command: {' '.join(cmd)}\n")

            proc = subprocess.Popen(cmd)

            disconnected = False
            try:
                while True:
                    try:
                        proc.wait(timeout=2)
                        break  # ddrescue exited on its own
                    except subprocess.TimeoutExpired:
                        src_ok = self.check_device_exists(self.source)
                        dst_ok = self._dest_present()
                        if not (src_ok and dst_ok):
                            disconnected = True
                            if not src_ok and not dst_ok:
                                which = 'Source and destination drives'
                            elif not src_ok:
                                which = 'Source drive'
                            else:
                                which = 'Destination drive'
                            print(f"\n  ⚠ {which} disconnected mid-run — stopping "
                                  "ddrescue cleanly (mapfile preserved)...")
                            self._stop_ddrescue_clean(proc)
                            break
            except KeyboardInterrupt:
                # User asked to stop; ddrescue got the same SIGINT from the tty — let it save.
                print("\n  Interrupted — letting ddrescue save its mapfile...")
                self._stop_ddrescue_clean(proc)
                return proc.returncode if proc.returncode is not None else 130

            if not disconnected:
                rc = proc.returncode if proc.returncode is not None else 0
                # ddrescue can exit on its own the instant a drive vanishes — often faster
                # than the 2s watchdog poll. If it errored out AND a drive is now missing,
                # treat it as a disconnect (pause + wait to resume) rather than bailing.
                if rc != 0 and (not self.check_device_exists(self.source) or not self._dest_present()):
                    print("\n  ⚠ ddrescue exited and a drive is no longer present — "
                          "treating as a disconnect; mapfile preserved.")
                    disconnected = True
                else:
                    return rc

            # Disconnect path: record it, wait for the verified drive(s), then relaunch
            # with refreshed self.source / self.dest (ddrescue skips already-rescued areas).
            self.state["device_disappeared"] = True
            self.save_state()
            if not self._wait_for_drives():
                return 130
            self.state["device_disappeared"] = False
            self.save_state()

    def run_ddrescue(self, domain_file, description, loose_domain=False):
        """Run ddrescue with a domain file, resilient to drive disconnects.

        Returns:
            True if ddrescue completed (with or without bad sectors), False on fatal
            error, device gone, or user abort.
        """
        print(f"\n{'='*60}")
        print(f"Running ddrescue: {description}")
        print(f"{'='*60}")
        print(f"Domain: {domain_file}")
        print()

        flags = ['-f', '-d']
        if loose_domain:
            flags.append('-L')

        rc = self._exec_ddrescue(flags, domain=domain_file)

        # ddrescue exit codes: 0=success, 1=completed with bad sectors (still usable),
        # 2=fatal. 130 = user aborted a reconnect wait (device_disappeared already set).
        if rc in (0, 1):
            return True
        if rc != 130:
            print("\nWARNING: ddrescue reported fatal errors")
        return False

    def run_ddrescue_regions(self, regions, description):
        """Run ddrescue for multiple regions, one at a time (ddrescue doesn't allow gaps)"""
        print(f"\n{'='*60}")
        print(f"Running ddrescue: {description}")
        print(f"{'='*60}")
        print(f"Total regions: {len(regions)}")

        total_size = sum(r[1] for r in regions)
        recovered_so_far = 0

        for i, (start, size) in enumerate(regions, 1):
            # Check if already recovered
            pct = self.check_region_recovered(start, size)
            if pct >= 99.9:
                recovered_so_far += size
                continue  # Skip already recovered regions

            # Create single-region domain file
            domain_path = self.job_dir / "current_region.txt"
            with open(domain_path, 'w') as f:
                f.write("# Mapfile. Created by GNU ddrescue version 1.23\n")
                f.write(f"# Region {i}/{len(regions)}\n")
                f.write("# current_pos  current_status  current_pass\n")
                f.write(f"0x{start:X}     +               1\n")
                f.write("#      pos        size  status\n")
                f.write(f"0x{start:X}  0x{size:X}  +\n")

            # Progress display
            progress_pct = (recovered_so_far / total_size * 100) if total_size > 0 else 0
            print(f"\r[{i}/{len(regions)}] {progress_pct:.1f}% - Region at {start/(1024**3):.2f}GB ({size/(1024**2):.1f}MB)    ", end='', flush=True)

            # Run ddrescue quietly (disconnect-resilient)
            rc = self._exec_ddrescue(['-f', '-d', '-q'], domain=domain_path, quiet=True)
            if rc == 130:
                print("\n  Aborted — stopping region recovery.")
                return

            recovered_so_far += size

        print(f"\nCompleted {len(regions)} regions")

    # =========================================================================
    # STAGE 0: Critical Disk Structures (GPT, EFI, backup boot sector)
    # =========================================================================
    def stage0_critical_structures(self):
        """Recover critical disk structures needed for destination to be mountable.

        This includes:
        - GPT partition table (first 34 sectors)
        - Backup GPT (last 33 sectors)
        - EFI System Partition (if present)
        - NTFS Backup Boot Sector (last sector of NTFS partition)
        """
        print("\n" + "="*60)
        print("STAGE 0: Critical Disk Structures")
        print("="*60)

        regions = []

        # 1. GPT Primary Header and Partition Entries (first 34 sectors = 17KB)
        gpt_primary_size = 34 * self.sector_size
        regions.append((0, gpt_primary_size, "GPT Primary"))
        print(f"  GPT Primary: sectors 0-33 ({gpt_primary_size} bytes)")

        # 2. Backup GPT (last 33 sectors of disk)
        gpt_backup_size = 33 * self.sector_size
        gpt_backup_start = self.drive_size - gpt_backup_size
        regions.append((gpt_backup_start, gpt_backup_size, "GPT Backup"))
        print(f"  GPT Backup: offset {gpt_backup_start} ({gpt_backup_size} bytes)")

        # 3. Find EFI System Partition and NTFS partition info
        efi_partition = None
        ntfs_start_sector = None
        ntfs_end_sector = None

        cmd = f"fdisk -l {self.source} 2>/dev/null"
        output, rc = self.run_cmd(cmd)

        for line in output.split('\n'):
            parts = line.split()
            if len(parts) < 5:
                continue

            # Look for EFI System Partition
            if 'EFI' in line and parts[0].startswith(self.source):
                try:
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

            # Look for NTFS partition (type 0x07 or "Microsoft basic data")
            if parts[0].startswith(self.source) and ('NTFS' in line or 'Microsoft' in line or 'Basic data' in line):
                try:
                    for i, p in enumerate(parts):
                        if p.isdigit():
                            ntfs_start_sector = int(p)
                            if i+1 < len(parts) and parts[i+1].isdigit():
                                ntfs_end_sector = int(parts[i+1])
                            break
                except:
                    pass

        # 4. NTFS Backup Boot Sector (last sector of NTFS partition)
        if ntfs_end_sector:
            backup_boot_start = ntfs_end_sector * self.sector_size
            backup_boot_size = self.sector_size
            regions.append((backup_boot_start, backup_boot_size, "NTFS Backup Boot Sector"))
            print(f"  NTFS Backup Boot: sector {ntfs_end_sector} ({backup_boot_size} bytes)")
            self.state["ntfs_end_sector"] = ntfs_end_sector
        elif self.partition_offset:
            # Estimate from partition offset - assume partition goes to near end of disk
            # This is less accurate but better than nothing
            print("  NTFS Backup Boot: Could not determine, will skip")

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

            # Check device exists before attempting recovery
            if not self.check_device_exists(self.source):
                print(f"\n  ERROR: Source device {self.source} is not accessible!")
                print("  Cannot recover critical structures - device may have disconnected.")
                return False

            success = self.run_ddrescue(domain_path, "Critical Disk Structures", loose_domain=True)

            if not success:
                print("\n  ERROR: ddrescue failed or device disappeared during critical structure recovery")
                return False

            # Re-check recovery status
            recovered = 0
            for start, size in domain_regions:
                pct = self.check_region_recovered(start, size)
                recovered += size * pct / 100

            final_pct = (recovered / total_size * 100) if total_size > 0 else 0
            print(f"\n  Post-recovery status: {final_pct:.1f}%")

            if final_pct < 90:
                print(f"  WARNING: Only {final_pct:.1f}% of critical structures recovered")
                print("  The destination may not be bootable/mountable")

        self.state["critical_structures_recovered"] = True
        self.save_state()

        # Refresh destination partition table
        print("\n  Refreshing destination partition table...")
        cmd = f"partprobe {self.dest} 2>/dev/null || blockdev --rereadpt {self.dest} 2>/dev/null"
        os.system(cmd)
        time.sleep(1)

        return True

    # =========================================================================
    # STAGE 1: Boot Sector
    # =========================================================================
    def stage1_boot_sector(self):
        """Recover and parse boot sector"""
        print("\n" + "="*60)
        print("STAGE 1: Boot Sector Recovery")
        print("="*60)

        # Boot sector is at partition start
        partition_byte_offset = self.partition_offset * self.sector_size
        boot_size = 512

        # Check if already recovered
        pct = self.check_region_recovered(partition_byte_offset, boot_size)
        print(f"Boot sector at offset {partition_byte_offset} ({partition_byte_offset/(1024**3):.4f} GB)")
        print(f"Recovery status: {pct:.1f}%")

        if pct < 100:
            # Create domain and recover
            domain = self.create_domain_file(
                [(partition_byte_offset, boot_size)],
                "boot_domain.txt"
            )
            self.run_ddrescue(domain, "Boot Sector")
            pct = self.check_region_recovered(partition_byte_offset, boot_size)

        if pct < 100:
            print(f"WARNING: Boot sector only {pct:.1f}% recovered")
            return False

        # Parse boot sector from destination
        print("\nParsing boot sector...")
        boot_data = self.extract_bytes(partition_byte_offset, boot_size)

        if boot_data[:4] != b'\xeb\x52\x90N' and boot_data[:3] != b'\xeb\x52\x90':
            # Check for NTFS signature at offset 3
            if boot_data[3:7] != b'NTFS':
                print("WARNING: NTFS signature not found, trying anyway...")

        # Parse NTFS boot sector
        # Bytes per sector: offset 0x0B (2 bytes)
        # Sectors per cluster: offset 0x0D (1 byte)
        # MFT cluster: offset 0x30 (8 bytes)

        bytes_per_sector = struct.unpack('<H', boot_data[0x0B:0x0D])[0]
        sectors_per_cluster = boot_data[0x0D]
        mft_cluster = struct.unpack('<Q', boot_data[0x30:0x38])[0]
        mft_mirr_cluster = struct.unpack('<Q', boot_data[0x38:0x40])[0]

        self.sector_size = bytes_per_sector
        self.cluster_size = bytes_per_sector * sectors_per_cluster
        self.mft_cluster = mft_cluster
        self.mft_mirr_cluster = mft_mirr_cluster
        self.mft_byte_offset = partition_byte_offset + (mft_cluster * self.cluster_size)
        self.mft_mirr_byte_offset = partition_byte_offset + (mft_mirr_cluster * self.cluster_size)

        print(f"  Bytes per sector: {bytes_per_sector}")
        print(f"  Sectors per cluster: {sectors_per_cluster}")
        print(f"  Cluster size: {self.cluster_size} bytes")
        print(f"  MFT cluster: {mft_cluster}")
        print(f"  MFT byte offset: {self.mft_byte_offset} ({self.mft_byte_offset/(1024**3):.3f} GB)")
        print(f"  MFTMirr cluster: {mft_mirr_cluster}")
        print(f"  MFTMirr byte offset: {self.mft_mirr_byte_offset} ({self.mft_mirr_byte_offset/(1024**3):.3f} GB)")

        self.state["boot_parsed"] = True
        self.state["partition_offset"] = self.partition_offset
        self.state["cluster_size"] = self.cluster_size
        self.state["mft_cluster"] = self.mft_cluster
        self.state["mft_byte_offset"] = self.mft_byte_offset
        self.state["mft_mirr_cluster"] = mft_mirr_cluster
        self.state["mft_mirr_byte_offset"] = self.mft_mirr_byte_offset
        self.save_state()

        return True

    def extract_bytes(self, offset, size):
        """Extract bytes from destination device at given offset"""
        cmd = f"dd if={self.dest} bs=1 skip={offset} count={size} 2>/dev/null"
        result = subprocess.run(cmd, shell=True, capture_output=True)
        return result.stdout

    # =========================================================================
    # MFT parsing helpers (used by stage5b priority recovery)
    # =========================================================================

    @staticmethod
    def _apply_usa_fixup(data, entry_size=1024):
        """Apply NTFS Update Sequence Array fixup in-place on a bytearray."""
        if len(data) < 8 or data[:4] != b'FILE':
            return
        usa_offset = struct.unpack_from('<H', data, 4)[0]
        usa_count = struct.unpack_from('<H', data, 6)[0]
        if usa_offset + usa_count * 2 > entry_size:
            return
        check = (data[usa_offset] | (data[usa_offset + 1] << 8))
        for i in range(1, usa_count):
            sector_tail = i * 512 - 2
            if sector_tail + 1 >= entry_size:
                break
            if (data[sector_tail] | (data[sector_tail + 1] << 8)) == check:
                repl_pos = usa_offset + i * 2
                data[sector_tail] = data[repl_pos]
                data[sector_tail + 1] = data[repl_pos + 1]

    def _parse_data_runs(self, run_list):
        """Parse NTFS data run list → [(byte_offset, byte_size)] pairs."""
        runs = []
        pos = 0
        current_lcn = 0
        while pos < len(run_list):
            header = run_list[pos]
            if header == 0:
                break
            pos += 1
            len_bytes = header & 0x0F
            off_bytes = (header >> 4) & 0x0F
            if pos + len_bytes + off_bytes > len(run_list):
                break
            run_len = int.from_bytes(run_list[pos:pos + len_bytes], 'little')
            pos += len_bytes
            if off_bytes > 0:
                delta_raw = int.from_bytes(run_list[pos:pos + off_bytes], 'little')
                if run_list[pos + off_bytes - 1] & 0x80:
                    delta_raw -= (1 << (off_bytes * 8))
                current_lcn += delta_raw
                pos += off_bytes
                if current_lcn >= 0 and run_len > 0:
                    runs.append((current_lcn * self.cluster_size, run_len * self.cluster_size))
            else:
                pos += off_bytes  # sparse run — no LCN
        return runs

    def _parse_mft_entry(self, data, entry_size=1024):
        """Parse a single MFT FILE record. Returns (flags, parent_inode, name, data_runs) or None."""
        if len(data) < entry_size or data[:4] != b'FILE':
            return None
        attrs_offset = struct.unpack_from('<H', data, 20)[0]
        flags = struct.unpack_from('<H', data, 22)[0]
        name = None
        parent_inode = None
        data_runs = []
        offset = attrs_offset
        while offset + 8 <= entry_size:
            attr_type = struct.unpack_from('<I', data, offset)[0]
            if attr_type == 0xFFFFFFFF:
                break
            attr_len = struct.unpack_from('<I', data, offset + 4)[0]
            if attr_len < 8 or offset + attr_len > entry_size:
                break
            non_resident = data[offset + 8]
            attr_name_len = data[offset + 9]
            if attr_type == 0x30 and not non_resident:  # $FILE_NAME (resident only)
                val_offset = struct.unpack_from('<H', data, offset + 20)[0]
                attr_data = data[offset + val_offset:]
                if len(attr_data) >= 66:
                    parent_ref = struct.unpack_from('<Q', attr_data, 0)[0]
                    fn_namespace = attr_data[65]
                    fn_len = attr_data[64]
                    # Prefer Win32 (1) or Win32&DOS (3) names over POSIX/DOS
                    if name is None or fn_namespace in (1, 3):
                        try:
                            name = attr_data[66:66 + fn_len * 2].decode('utf-16-le')
                            parent_inode = parent_ref & 0x0000FFFFFFFFFFFF
                        except UnicodeDecodeError:
                            pass
            elif attr_type == 0x80 and non_resident and attr_name_len == 0:  # unnamed $DATA
                run_list_offset = struct.unpack_from('<H', data, offset + 32)[0]
                runs = self._parse_data_runs(data[offset + run_list_offset:offset + attr_len])
                data_runs.extend(runs)
            offset += attr_len
        return (flags, parent_inode, name, data_runs)

    def _get_mft_data_runs(self):
        """Read MFT entry 0 ($MFT) from dest to get the MFT's own fragmented extents.

        Returns [(byte_offset, byte_size), ...] — the physical layout of the MFT on disk.
        Must be called after mft_byte_offset and cluster_size are set.
        """
        entry_size = 1024
        try:
            with open(self.dest, 'rb') as f:
                f.seek(self.mft_byte_offset)
                data = bytearray(f.read(entry_size))
        except OSError as e:
            print(f"  ERROR reading MFT entry 0: {e}")
            return []
        if len(data) < entry_size or data[:4] != b'FILE':
            print("  WARNING: MFT entry 0 invalid — falling back to single-fragment scan")
            return [(self.mft_byte_offset, 200 * 1024 * 1024)]  # 200MB fallback
        self._apply_usa_fixup(data, entry_size)
        runs = self._parse_data_runs_from_entry(bytes(data), entry_size)
        return runs

    def _parse_data_runs_from_entry(self, data, entry_size=1024):
        """Extract unnamed $DATA data runs from an MFT FILE record."""
        ao = struct.unpack_from('<H', data, 20)[0]
        offset = ao
        while offset + 8 <= entry_size:
            attr_type = struct.unpack_from('<I', data, offset)[0]
            if attr_type == 0xFFFFFFFF:
                break
            attr_len = struct.unpack_from('<I', data, offset + 4)[0]
            if attr_len < 8 or offset + attr_len > entry_size:
                break
            non_resident = data[offset + 8]
            attr_name_len = data[offset + 9]
            if attr_type == 0x80 and non_resident and attr_name_len == 0:
                rlo = struct.unpack_from('<H', data, offset + 32)[0]
                return self._parse_data_runs(data[offset + rlo:offset + attr_len])
            offset += attr_len
        return []

    def _extract_mft_for_visualizer(self):
        """Assemble the full $MFT from the destination into job_dir/mft.raw so the
        recovery-map visualizer can build its file-level view.

        Reuses the MFT's own data runs (every fragment), so a fragmented MFT is
        captured in full, not just its first fragment, and the visualizer's
        parse_mft() then reads every entry across all fragments. Best-effort: a
        failure here never fails the recovery.
        """
        mft_path = self.job_dir / "mft.raw"
        try:
            if mft_path.exists() and mft_path.stat().st_size > 0:
                return  # already assembled
            if not self.mft_byte_offset:
                return
            if not (self.check_device_exists(self.dest) or os.path.exists(str(self.dest))):
                return
            runs = self._get_mft_data_runs()
            if not runs:
                return
            total = sum(s for _, s in runs)
            print(f"\nAssembling $MFT for the file-level recovery map "
                  f"({len(runs)} fragment(s), {total/(1024**2):.1f} MB)...")
            written = 0
            with open(self.dest, 'rb') as src, open(mft_path, 'wb') as out:
                for off, size in runs:
                    src.seek(off)
                    remaining = size
                    while remaining > 0:
                        chunk = src.read(min(remaining, 8 * 1024 * 1024))
                        if not chunk:
                            break
                        out.write(chunk)
                        written += len(chunk)
                        remaining -= len(chunk)
            print(f"  Saved {mft_path.name} ({written/(1024**2):.1f} MB across "
                  f"{len(runs)} fragment(s)) - the visualizer's file map will now populate.")
        except OSError as e:
            print(f"  Note: could not assemble $MFT ({e}); file-level map will be unavailable.")

    def _parse_mft_for_priority_domains(self, preset_key):
        """Two-pass MFT scan → (tier1_runs, tier2_runs) sorted [(byte_offset, byte_size)] lists.

        Pass 1: scan all MFT fragments for directories only (quick flags check, skip files).
                Users may be in any fragment so we must scan all before resolving.
        Resolve: Users → profiles → tier1/tier2 dirs → BFS-expand to all subdirs.
        Pass 2: scan all MFT fragments, bucket file runs into tier1 or tier2.

        Tier 1 = local user folders (Desktop, Documents, Pictures, etc.)
        Tier 2 = cloud sync folders (OneDrive, Dropbox, etc.) — recovered after tier 1,
                 before Windows/Program Files/AppData which stage 6 handles.

        Reads entirely from self.dest (healthy destination). No subprocess calls.
        """
        preset = PRIORITY_PRESETS.get(preset_key, PRIORITY_PRESETS['default'])
        t1_lower = {d.lower() for d in preset['tier1_dirs']}
        t2_lower = {d.lower() for d in preset['tier2_dirs']}
        priority_lower = t1_lower | t2_lower
        skip_lower = {d.lower() for d in preset['skip_dirs']}

        entry_size = 1024
        chunk_size = 4 * 1024 * 1024

        mft_runs = self._get_mft_data_runs()
        if not mft_runs:
            print("  ERROR: Could not determine MFT layout")
            return []
        total_mft = sum(s for _, s in mft_runs)
        print(f"  MFT: {len(mft_runs)} fragment(s), {total_mft / (1024**2):.1f} MB total")

        def scan_frags_dirs():
            """Pass 1: collect all in-use directory entries across all fragments."""
            dir_map = {}
            try:
                with open(self.dest, 'rb') as f:
                    inode_base = 0
                    for frag_idx, (frag_offset, frag_size) in enumerate(mft_runs):
                        f.seek(frag_offset)
                        buf = b''
                        frag_end = frag_offset + frag_size
                        frag_pos = frag_offset
                        local = 0
                        while frag_pos < frag_end:
                            if len(buf) < entry_size:
                                to_read = min(chunk_size, frag_end - frag_pos - len(buf))
                                if to_read > 0:
                                    buf += f.read(to_read)
                            if len(buf) < entry_size:
                                break
                            raw = bytearray(buf[:entry_size])
                            buf = buf[entry_size:]
                            frag_pos += entry_size
                            # Flags at offset 22 — not affected by USA fixup (which touches
                            # sector-tail bytes 510-511 and 1022-1023), so safe to check first.
                            if raw[:4] == b'FILE' and (raw[22] & 0x03) == 0x03:
                                self._apply_usa_fixup(raw, entry_size)
                                result = self._parse_mft_entry(bytes(raw), entry_size)
                                if result:
                                    _, parent, name, _ = result
                                    if parent is not None and name:
                                        dir_map[inode_base + local] = (parent, name)
                            local += 1
                        print(f"  Pass 1 frag {frag_idx+1}/{len(mft_runs)}: "
                              f"{frag_offset/(1024**3):.2f} GB  {local:,} entries  "
                              f"{len(dir_map):,} dirs")
                        inode_base += local
            except OSError as e:
                print(f"  ERROR (pass 1): {e}")
            return dir_map

        def resolve_priority_inodes(dir_map):
            """Find Users → profiles → tier1/tier2 dirs, BFS-expand each tier separately.
            Returns (t1_inodes, t2_inodes) — two sets for bucketing file runs in pass 2.
            """
            users_ino = next((ino for ino, (p, n) in dir_map.items()
                              if p == 5 and n.lower() == 'users'), None)
            if users_ino is None:
                print("  WARNING: Users directory not found in MFT")
                return set(), set()
            skip_profiles = {'public', 'default', 'default user', 'all users'}
            profiles = {ino: name for ino, (p, name) in dir_map.items()
                        if p == users_ino and name.lower() not in skip_profiles}
            print(f"  User profiles: {list(profiles.values())}")
            t1_inodes, t2_inodes = set(), set()
            for user_ino, username in profiles.items():
                for ino, (p, name) in dir_map.items():
                    if p == user_ino:
                        nl = name.lower()
                        if nl in t1_lower:
                            t1_inodes.add(ino)
                            print(f"    Tier 1: {username}/{name}")
                        elif nl in t2_lower:
                            t2_inodes.add(ino)
                            print(f"    Tier 2: {username}/{name}")
            if not t1_inodes and not t2_inodes:
                print("  No priority dirs found")
                return set(), set()
            # BFS: expand each tier to include all subdirectories
            for tier_set in (t1_inodes, t2_inodes):
                changed = True
                while changed:
                    changed = False
                    for ino, (p, name) in dir_map.items():
                        if ino not in tier_set and p in tier_set \
                                and name.lower() not in skip_lower:
                            tier_set.add(ino)
                            changed = True
            print(f"  Tier 1: {len(t1_inodes):,} dirs  |  Tier 2: {len(t2_inodes):,} dirs")
            return t1_inodes, t2_inodes

        def scan_frags_files(t1_inodes, t2_inodes):
            """Pass 2: collect data runs bucketed into tier1 and tier2 lists."""
            t1_runs, t2_runs = [], []
            all_priority = t1_inodes | t2_inodes
            try:
                with open(self.dest, 'rb') as f:
                    inode_base = 0
                    for frag_idx, (frag_offset, frag_size) in enumerate(mft_runs):
                        f.seek(frag_offset)
                        buf = b''
                        frag_end = frag_offset + frag_size
                        frag_pos = frag_offset
                        local = 0
                        while frag_pos < frag_end:
                            if len(buf) < entry_size:
                                to_read = min(chunk_size, frag_end - frag_pos - len(buf))
                                if to_read > 0:
                                    buf += f.read(to_read)
                            if len(buf) < entry_size:
                                break
                            raw = bytearray(buf[:entry_size])
                            buf = buf[entry_size:]
                            frag_pos += entry_size
                            if raw[:4] == b'FILE' and (raw[22] & 0x03) == 0x01:
                                self._apply_usa_fixup(raw, entry_size)
                                result = self._parse_mft_entry(bytes(raw), entry_size)
                                if result:
                                    _, parent, _, data_runs = result
                                    if data_runs and parent in all_priority:
                                        if parent in t1_inodes:
                                            t1_runs.extend(data_runs)
                                        else:
                                            t2_runs.extend(data_runs)
                            local += 1
                        print(f"  Pass 2 frag {frag_idx+1}/{len(mft_runs)}: "
                              f"{frag_offset/(1024**3):.2f} GB  "
                              f"t1={len(t1_runs):,}  t2={len(t2_runs):,} segments")
                        inode_base += local
            except OSError as e:
                print(f"  ERROR (pass 2): {e}")
            return t1_runs, t2_runs

        def merge_runs(runs):
            if not runs:
                return []
            runs.sort()
            merged = [list(runs[0])]
            for offset, size in runs[1:]:
                prev = merged[-1]
                if offset <= prev[0] + prev[1]:
                    prev[1] = max(prev[0] + prev[1], offset + size) - prev[0]
                else:
                    merged.append([offset, size])
            return [(o, s) for o, s in merged]

        dir_map = scan_frags_dirs()
        t1_inodes, t2_inodes = resolve_priority_inodes(dir_map)
        if not t1_inodes and not t2_inodes:
            return [], []

        t1_runs, t2_runs = scan_frags_files(t1_inodes, t2_inodes)
        t1_merged = merge_runs(t1_runs)
        t2_merged = merge_runs(t2_runs)
        t1_gb = sum(s for _, s in t1_merged) / (1024**3)
        t2_gb = sum(s for _, s in t2_merged) / (1024**3)
        print(f"  Tier 1: {t1_gb:.2f} GB across {len(t1_merged):,} regions")
        print(f"  Tier 2: {t2_gb:.2f} GB across {len(t2_merged):,} regions")
        return t1_merged, t2_merged

    # =========================================================================
    # STAGE 2: MFT Header (first 10 entries) + $MFTMirr
    # =========================================================================
    def stage2_mft_header(self):
        """Recover and parse MFT header to find $Bitmap location.

        Also recovers $MFTMirr (mirror of first 4 MFT records) for redundancy.
        """
        print("\n" + "="*60)
        print("STAGE 2: MFT Header + MFTMirr Recovery")
        print("="*60)

        # MFT entry size is typically 1024 bytes
        # We need entries 0-9 (10 entries) to get $Bitmap (entry 6)
        mft_entry_size = 1024
        mft_header_size = mft_entry_size * 10  # 10KB

        # $MFTMirr contains copies of first 4 records (4KB)
        mft_mirr_size = mft_entry_size * 4  # 4KB

        print(f"MFT location: {self.mft_byte_offset} ({self.mft_byte_offset/(1024**3):.3f} GB)")
        print(f"MFTMirr location: {self.mft_mirr_byte_offset} ({self.mft_mirr_byte_offset/(1024**3):.3f} GB)")
        print(f"Recovering MFT entries 0-9 ({mft_header_size} bytes) + MFTMirr ({mft_mirr_size} bytes)")

        # Create regions for both MFT and MFTMirr
        regions = [
            (self.mft_byte_offset, mft_header_size),
            (self.mft_mirr_byte_offset, mft_mirr_size),
        ]

        # Check recovery status
        mft_pct = self.check_region_recovered(self.mft_byte_offset, mft_header_size)
        mirr_pct = self.check_region_recovered(self.mft_mirr_byte_offset, mft_mirr_size)
        print(f"MFT recovery status: {mft_pct:.1f}%")
        print(f"MFTMirr recovery status: {mirr_pct:.1f}%")

        if mft_pct < 100 or mirr_pct < 100:
            # MFTMirr (~8KB) and MFT header (~3GB) are far apart — a single domain
            # file with both regions triggers "Blocks are not contiguous" in ddrescue
            # 1.27+.  run_ddrescue_regions() issues one ddrescue call per region,
            # each with a single-region domain file, avoiding the problem.
            self.run_ddrescue_regions(
                sorted(regions, key=lambda x: x[0]),
                "MFT Header + MFTMirr"
            )
            mft_pct = self.check_region_recovered(self.mft_byte_offset, mft_header_size)
            mirr_pct = self.check_region_recovered(self.mft_mirr_byte_offset, mft_mirr_size)

        pct = mft_pct  # Use MFT pct for decision making

        print(f"Post-run MFT recovery: {mft_pct:.1f}%  MFTMirr: {mirr_pct:.1f}%")
        if pct < 95:  # Allow some tolerance
            print(f"WARNING: MFT header only {pct:.1f}% recovered")

        # Parse MFT entry 6 ($Bitmap) to get its data runs
        print("\nParsing MFT entry 6 ($Bitmap)...")

        # Use istat to get $Bitmap data runs.
        # -f ntfs forces filesystem detection — needed when the destination is a raw
        # whole-disk device (no partition table at offset 0).
        partition_offset_sectors = self.partition_offset
        result = subprocess.run(
            f"istat -f ntfs -o {partition_offset_sectors} {self.dest} 6",
            shell=True, capture_output=True, text=True
        )
        output, rc = result.stdout, result.returncode
        if rc != 0:
            err = result.stderr.strip()
            print(f"ERROR: Failed to parse MFT entry 6")
            if err:
                print(f"  istat: {err}")
            return False

        # Parse istat output to find data runs
        # Look for cluster numbers after $DATA attribute
        self.bitmap_data_runs = []
        in_data_section = False
        clusters = []

        for line in output.split('\n'):
            if ('$DATA' in line or '(128-' in line) and 'Non-Resident' in line:
                in_data_section = True
                continue
            if in_data_section:
                # Cluster numbers are listed as space-separated integers
                parts = line.split()
                for part in parts:
                    try:
                        cluster = int(part)
                        clusters.append(cluster)
                    except ValueError:
                        pass

        if not clusters:
            print("ERROR: Could not find $Bitmap data runs")
            return False

        # Convert clusters to byte ranges
        # Group contiguous clusters
        clusters.sort()
        partition_byte_offset = self.partition_offset * self.sector_size

        ranges = []
        start_cluster = clusters[0]
        prev_cluster = clusters[0]

        for cluster in clusters[1:]:
            if cluster == prev_cluster + 1:
                prev_cluster = cluster
            else:
                # End of contiguous range
                start_byte = partition_byte_offset + (start_cluster * self.cluster_size)
                size_bytes = (prev_cluster - start_cluster + 1) * self.cluster_size
                ranges.append((start_byte, size_bytes))
                start_cluster = cluster
                prev_cluster = cluster

        # Don't forget last range
        start_byte = partition_byte_offset + (start_cluster * self.cluster_size)
        size_bytes = (prev_cluster - start_cluster + 1) * self.cluster_size
        ranges.append((start_byte, size_bytes))

        self.bitmap_data_runs = ranges
        total_size = sum(r[1] for r in ranges)

        print(f"  Found {len(ranges)} data run(s) for $Bitmap")
        print(f"  Total size: {total_size/(1024**2):.1f} MB")
        for i, (start, size) in enumerate(ranges):
            print(f"    Run {i+1}: offset 0x{start:X} ({start/(1024**3):.3f} GB), size {size/(1024**2):.1f} MB")

        self.state["bitmap_data_runs"] = [(s, sz) for s, sz in ranges]
        self.save_state()

        return True

    # =========================================================================
    # STAGE 3: $Bitmap Recovery
    # =========================================================================
    def stage3_bitmap_recovery(self):
        """Recover $Bitmap data runs"""
        print("\n" + "="*60)
        print("STAGE 3: $Bitmap Recovery")
        print("="*60)

        if not self.bitmap_data_runs:
            print("ERROR: No $Bitmap data runs found")
            return False

        # Check recovery status of each run
        total_size = sum(r[1] for r in self.bitmap_data_runs)
        total_recovered = 0

        for start, size in self.bitmap_data_runs:
            pct = self.check_region_recovered(start, size)
            recovered = size * pct / 100
            total_recovered += recovered
            print(f"  Region 0x{start:X}: {pct:.1f}% recovered")

        overall_pct = (total_recovered / total_size * 100) if total_size > 0 else 0
        print(f"\nOverall $Bitmap recovery: {overall_pct:.1f}%")

        # Loop until user is satisfied or we hit 100%
        retry_level = 0

        while overall_pct < 100:
            if overall_pct >= 99.9:
                print(f"\n$Bitmap is {overall_pct:.2f}% recovered - close enough to proceed.")
                break

            missing_bytes = total_size - total_recovered
            missing_clusters = int(missing_bytes / self.cluster_size * 8) if self.cluster_size else 0

            print(f"\n$Bitmap is {overall_pct:.1f}% recovered")
            print(f"  Missing: {missing_bytes/(1024**2):.2f} MB ({missing_clusters:,} potential cluster entries)")

            # Determine best default: if high recovery %, continue; otherwise try more
            if overall_pct >= 95:
                default_choice = '3'  # Good enough, continue
            else:
                default_choice = '1'  # Try to recover more

            options = {
                '1': "Try to recover more (standard pass)",
                '2': "Try harder (with retries: -A -r3 -M)",
                '3': "Continue anyway (may miss some data)",
                'q': "Quit"
            }

            choice = self.prompt_with_timeout(
                f"$Bitmap is {overall_pct:.1f}% recovered. What would you like to do?",
                options,
                default_choice
            )

            if choice == 'q':
                print("Aborted by user.")
                return False
            elif choice == '3':
                print(f"Continuing with {overall_pct:.1f}% $Bitmap recovery...")
                break
            elif choice == '2':
                extra_flags = "-A -r3 -M"
                print(f"\nRunning aggressive recovery with {extra_flags}...")
            else:
                extra_flags = "-A" if retry_level > 0 else ""

            domain = self.create_domain_file(
                self.bitmap_data_runs,
                "bitmap_data_domain.txt"
            )

            # Run ddrescue with optional extra flags
            # -L (loose-domain) is required when $Bitmap spans non-contiguous clusters
            print(f"\n{'='*60}")
            print(f"Running ddrescue: $Bitmap Data Runs")
            print(f"{'='*60}")
            flags = ['-f', '-d', '-L'] + (extra_flags.split() if extra_flags else [])
            rc = self._exec_ddrescue(flags, domain=domain)
            if rc == 130:
                print("\n  Aborted — stopping $Bitmap recovery.")
                self.state["bitmap_recovered_pct"] = overall_pct
                self.save_state()
                return False

            # Recheck
            total_recovered = 0
            for start, size in self.bitmap_data_runs:
                pct = self.check_region_recovered(start, size)
                total_recovered += size * pct / 100
            overall_pct = (total_recovered / total_size * 100) if total_size > 0 else 0
            print(f"\nAfter recovery: {overall_pct:.1f}%")

            retry_level += 1

        self.state["bitmap_recovered_pct"] = overall_pct
        self.save_state()

        return True

    # =========================================================================
    # STAGE 4: Parse $Bitmap for all allocated clusters
    # =========================================================================
    def stage4_parse_bitmap(self):
        """Extract and parse $Bitmap to find all allocated clusters"""
        print("\n" + "="*60)
        print("STAGE 4: Parse $Bitmap")
        print("="*60)

        import struct
        ranges_cache = self.job_dir / "allocated_ranges.bin"

        # On resume, reload from binary cache — unless $Bitmap coverage improved
        if self.state.get("bitmap_parsed") and ranges_cache.exists():
            cached_pct = self.state.get("bitmap_pct_when_cached", 0)
            current_pct = sum(
                self.check_region_recovered(s, z) for s, z in self.bitmap_data_runs
            ) / max(len(self.bitmap_data_runs), 1) if self.bitmap_data_runs else 100.0
            if current_pct <= cached_pct + 0.5:
                with open(ranges_cache, 'rb') as f:
                    count = struct.unpack('<I', f.read(4))[0]
                    ranges = []
                    for _ in range(count):
                        start, size = struct.unpack('<QQ', f.read(16))
                        ranges.append((start, size, 0, 0))
                self.allocated_clusters = ranges
                total = sum(r[1] for r in ranges)
                print(f"  Loaded {count:,} ranges from cache ({total/(1024**3):.2f} GB, $Bitmap {current_pct:.1f}% recovered)")
                return True
            else:
                print(f"  $Bitmap improved {cached_pct:.1f}% → {current_pct:.1f}% — re-parsing...")

        # Extract $Bitmap from destination
        bitmap_path = self.job_dir / "bitmap.raw"
        partition_offset_sectors = self.partition_offset

        print(f"Extracting $Bitmap to {bitmap_path}...")
        # -f ntfs forces filesystem detection — needed when dest is a raw whole-disk device
        cmd = f"icat -f ntfs -o {partition_offset_sectors} {self.dest} 6 > {bitmap_path}"
        os.system(cmd)

        if not bitmap_path.exists() or bitmap_path.stat().st_size == 0:
            print("ERROR: Failed to extract $Bitmap")
            return False

        bitmap_size = bitmap_path.stat().st_size
        print(f"  Extracted {bitmap_size/(1024**2):.1f} MB")

        # Parse bitmap
        print("Parsing bitmap for allocated clusters...")

        with open(bitmap_path, 'rb') as f:
            bitmap_data = f.read()

        partition_byte_offset = self.partition_offset * self.sector_size

        # Calculate maximum valid cluster number based on drive size
        # This prevents garbage bytes in $Bitmap from creating invalid entries
        max_cluster = (self.drive_size - partition_byte_offset) // self.cluster_size
        print(f"  Drive size: {self.drive_size/(1024**4):.2f} TB")
        print(f"  Max valid cluster: {max_cluster:,}")

        # Find allocated cluster ranges
        allocated_ranges = []
        current_start = None
        prev_cluster = None
        total_allocated = 0
        skipped_beyond_drive = 0

        for byte_idx, byte in enumerate(bitmap_data):
            for bit_idx in range(8):
                cluster_num = byte_idx * 8 + bit_idx

                # Skip clusters beyond drive capacity (handles $Bitmap corruption/garbage)
                if cluster_num >= max_cluster:
                    if byte & (1 << bit_idx):
                        skipped_beyond_drive += 1
                    # Finalize any pending range and stop
                    if current_start is not None:
                        start_byte = partition_byte_offset + (current_start * self.cluster_size)
                        size_bytes = (prev_cluster - current_start + 1) * self.cluster_size
                        allocated_ranges.append((start_byte, size_bytes, current_start, prev_cluster))
                        current_start = None
                    continue

                is_allocated = bool(byte & (1 << bit_idx))

                if is_allocated:
                    total_allocated += 1
                    if current_start is None:
                        current_start = cluster_num
                    prev_cluster = cluster_num
                else:
                    if current_start is not None:
                        # End of allocated range
                        start_byte = partition_byte_offset + (current_start * self.cluster_size)
                        size_bytes = (prev_cluster - current_start + 1) * self.cluster_size
                        allocated_ranges.append((start_byte, size_bytes, current_start, prev_cluster))
                        current_start = None

        # Don't forget last range
        if current_start is not None:
            start_byte = partition_byte_offset + (current_start * self.cluster_size)
            size_bytes = (prev_cluster - current_start + 1) * self.cluster_size
            allocated_ranges.append((start_byte, size_bytes, current_start, prev_cluster))

        if skipped_beyond_drive > 0:
            print(f"  WARNING: Skipped {skipped_beyond_drive:,} 'allocated' clusters beyond drive capacity")
            print(f"           (This is normal - likely garbage/uninitialized bytes in $Bitmap)")

        # Sort ranges by start position (should already be sorted, but be safe)
        allocated_ranges.sort(key=lambda x: x[0])

        # Check which parts of $Bitmap were actually recovered
        # For unrecovered portions, conservatively assume those clusters ARE allocated
        unknown_cluster_ranges = []
        bitmap_start_byte = self.bitmap_data_runs[0][0] if self.bitmap_data_runs else 0

        for bm_start, bm_size in self.bitmap_data_runs:
            pct = self.check_region_recovered(bm_start, bm_size)
            if pct < 99:
                # Calculate which clusters this unrecovered $Bitmap portion represents
                # Each byte of $Bitmap = 8 clusters
                # bm_start is absolute byte position; need offset within $Bitmap
                bitmap_byte_offset = bm_start - bitmap_start_byte
                start_cluster = (bitmap_byte_offset * 8)
                end_cluster = start_cluster + (bm_size * 8)

                # Limit to valid cluster range
                start_cluster = max(0, start_cluster)
                end_cluster = min(end_cluster, max_cluster)

                if end_cluster > start_cluster:
                    # These clusters' allocation status is UNKNOWN - assume allocated
                    start_byte = partition_byte_offset + (start_cluster * self.cluster_size)
                    size_bytes = (end_cluster - start_cluster) * self.cluster_size
                    unknown_cluster_ranges.append((start_byte, size_bytes, start_cluster, end_cluster - 1))

                    print(f"  WARNING: $Bitmap region at 0x{bm_start:X} is only {pct:.0f}% recovered")
                    print(f"           Adding clusters {start_cluster:,}-{end_cluster-1:,} as 'unknown but recover anyway'")
                    print(f"           ({size_bytes/(1024**3):.2f} GB)")

        # Keep unknown ranges SEPARATE - recover them last (bird in hand...)
        self.unknown_cluster_ranges = unknown_cluster_ranges
        if unknown_cluster_ranges:
            unknown_total = sum(r[1] for r in unknown_cluster_ranges)
            print(f"\n  Identified {len(unknown_cluster_ranges)} 'unknown' ranges ({unknown_total/(1024**3):.2f} GB)")
            print(f"  These will be offered as optional recovery AFTER known data")
            self.state["unknown_ranges"] = [(r[0], r[1]) for r in unknown_cluster_ranges]
            self.save_state()

        self.allocated_clusters = allocated_ranges
        total_data_bytes = sum(r[1] for r in allocated_ranges)

        print(f"  Total allocated clusters: {total_allocated:,}")
        print(f"  Total allocated data: {total_data_bytes/(1024**3):.2f} GB")
        print(f"  Contiguous ranges: {len(allocated_ranges):,}")

        # Save summary
        summary_path = self.job_dir / "cluster_analysis.txt"
        with open(summary_path, 'w') as f:
            f.write(f"Cluster Analysis\n")
            f.write(f"================\n")
            f.write(f"Total allocated clusters: {total_allocated:,}\n")
            f.write(f"Total allocated data: {total_data_bytes/(1024**3):.2f} GB\n")
            f.write(f"Contiguous ranges: {len(allocated_ranges):,}\n\n")
            f.write(f"Top 20 largest ranges:\n")
            sorted_ranges = sorted(allocated_ranges, key=lambda x: x[1], reverse=True)[:20]
            for start, size, cstart, cend in sorted_ranges:
                f.write(f"  Clusters {cstart:,}-{cend:,}: {size/(1024**2):.1f} MB at 0x{start:X}\n")

        print(f"  Analysis saved to {summary_path}")

        self.state["allocated_ranges_count"] = len(allocated_ranges)
        self.state["total_allocated_bytes"] = total_data_bytes

        # Save binary cache for fast resume
        with open(ranges_cache, 'wb') as f:
            f.write(struct.pack('<I', len(allocated_ranges)))
            for start, size, _, _ in allocated_ranges:
                f.write(struct.pack('<QQ', start, size))

        # Record $Bitmap coverage so cache can be invalidated if it improves
        bitmap_pct = sum(
            self.check_region_recovered(s, z) for s, z in self.bitmap_data_runs
        ) / max(len(self.bitmap_data_runs), 1) if self.bitmap_data_runs else 100.0
        self.state["bitmap_pct_when_cached"] = bitmap_pct
        self.state["bitmap_parsed"] = True
        self.state.pop("data_domain_created", None)  # force stage5 to regenerate if bitmap improved
        self.save_state()

        return True

    # =========================================================================
    # STAGE 5: Create data recovery domain
    # =========================================================================
    def stage5_create_data_domain(self):
        """Create domain file for all allocated data"""
        print("\n" + "="*60)
        print("STAGE 5: Create Data Recovery Domain")
        print("="*60)

        if not self.allocated_clusters:
            print("ERROR: No allocated clusters found")
            return False

        regions = [(r[0], r[1]) for r in self.allocated_clusters]
        domain_path = self.job_dir / "all_data_domain.txt"

        # Skip re-creation if already done — just report current status
        if self.state.get("data_domain_created") and domain_path.exists():
            print(f"Using existing domain file: {domain_path}")
        else:
            domain_path = self.create_domain_file(regions, "all_data_domain.txt")
            print(f"Created domain file: {domain_path}")

        total_size = sum(r[1] for r in regions)
        print(f"  Regions: {len(regions):,}")
        print(f"  Total size: {total_size/(1024**3):.2f} GB")

        # Single-pass log check (replaces 100k individual log reads)
        total_recovered = self._total_recovered_bytes(regions)
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
    # STAGE 5b: Priority user folder recovery
    # =========================================================================
    def stage5b_priority_recovery(self):
        """Recover priority user folders (Desktop/Documents/Pictures) before full data sweep."""
        print("\n" + "="*60)
        print("STAGE 5b: Priority User Folder Recovery")
        print("="*60)
        print("Recover important user folders FIRST so they arrive before AppData/Windows/Program Files.\n")

        preset_keys = list(PRIORITY_PRESETS.keys())
        for i, (key, preset) in enumerate(PRIORITY_PRESETS.items(), 1):
            dirs_preview = ', '.join((preset.get('tier1_dirs') or preset.get('priority_dirs', []))[:4])
            print(f"  [{i}] {preset['name']}")
            print(f"      Folders: {dirs_preview}...")
        print("  [s] Skip priority recovery")
        print()

        options = {str(i): p['name'] for i, p in enumerate(PRIORITY_PRESETS.values(), 1)}
        options['s'] = 'Skip'
        choice = self.prompt_with_timeout("Select preset", options, '1', timeout=120)

        if choice == 's':
            print("Skipping priority recovery.")
            return True

        try:
            preset_key = preset_keys[int(choice) - 1]
        except (ValueError, IndexError):
            preset_key = 'default'

        print(f"\nPreset: {PRIORITY_PRESETS[preset_key]['name']}")

        # Recover all MFT fragments before scanning — stage 2 only fetched the
        # first 10 entries; the full MFT is needed to locate user folder files.
        print("\nEnsuring full MFT is recovered before scanning...")
        mft_runs = self._get_mft_data_runs()
        if mft_runs:
            total_mft = sum(s for _, s in mft_runs)
            recovered_mft = sum(s * self.check_region_recovered(o, s) / 100 for o, s in mft_runs)
            mft_pct = (recovered_mft / total_mft * 100) if total_mft else 0
            if mft_pct < 99:
                print(f"  MFT is {mft_pct:.1f}% recovered — fetching all {len(mft_runs)} fragment(s) ({total_mft/(1024**2):.1f} MB)...")
                mft_domain = self.create_domain_file(mft_runs, "mft_full_domain.txt")
                ok = self.run_ddrescue(mft_domain, "Full MFT (for priority scan)", loose_domain=True)
                if not ok and self.state.get("device_disappeared"):
                    print("  Source device lost during MFT fetch — cannot scan. Resume after reconnecting.")
                    return False
            else:
                print(f"  MFT already {mft_pct:.1f}% recovered.")
        else:
            print("  WARNING: Could not determine MFT fragment locations — scan may be incomplete")

        print("Parsing MFT on destination device to locate priority files...")
        t0 = time.time()
        t1_runs, t2_runs = self._parse_mft_for_priority_domains(preset_key)
        elapsed = time.time() - t0
        print(f"  MFT scan completed in {elapsed:.1f}s")

        if not t1_runs and not t2_runs:
            print("No priority file data found — proceeding to full recovery.")
            return True

        priority_complete = True

        if t1_runs:
            domain = self.create_domain_file(t1_runs, "priority_t1_domain.txt")
            total = sum(s for _, s in t1_runs)
            print(f"\nTier 1 domain: {len(t1_runs):,} regions, {total/(1024**3):.2f} GB")
            print("  (Desktop, Documents, Pictures, local user folders)")
            if not self.run_ddrescue(domain, "Priority Tier 1 — Local User Folders", loose_domain=True):
                priority_complete = False

        # Skip tier 2 if the source already vanished during tier 1 — it would only fail too
        if t2_runs and priority_complete:
            domain = self.create_domain_file(t2_runs, "priority_t2_domain.txt")
            total = sum(s for _, s in t2_runs)
            print(f"\nTier 2 domain: {len(t2_runs):,} regions, {total/(1024**3):.2f} GB")
            print("  (OneDrive, Dropbox, cloud sync folders)")
            if not self.run_ddrescue(domain, "Priority Tier 2 — Cloud Sync Folders", loose_domain=True):
                priority_complete = False

        # Only mark done if priority recovery actually finished without the source
        # disconnecting. Otherwise leave the flag unset so resume re-runs this stage
        # rather than skipping straight to the full sweep with priority folders missing.
        if not priority_complete or self.state.get("device_disappeared"):
            print("\n  Priority recovery incomplete — source device disconnected. Will resume on reconnect.")
            return False

        self.state["priority_recovery_done"] = True
        self.save_state()
        return True

    # =========================================================================
    # STAGE 6: Recover all data
    # =========================================================================
    def stage6_recover_data(self):
        """Run ddrescue to recover all allocated data"""
        print("\n" + "="*60)
        print("STAGE 6: Recover All Allocated Data")
        print("="*60)

        if self.allocated_clusters:
            total_alloc = sum(r[1] for r in self.allocated_clusters)
            n_ranges = len(self.allocated_clusters)
            print(f"  Allocated data: {total_alloc / (1024**3):.2f} GB across {n_ranges:,} ranges")

        domain_path = self.job_dir / "all_data_domain.txt"
        if not domain_path.exists():
            print("ERROR: Data domain file not found")
            return False

        # Use -L (loose-domain) flag to allow gaps between regions
        success = self.run_ddrescue(domain_path, "All Allocated Data", loose_domain=True)

        if not success:
            print("\nWARNING: ddrescue failed or device disappeared during data recovery")
            # Still check what we got before failing

        # Check final status — allocated_clusters are 4-tuples (start, size, c_start, c_end)
        regions = [(r[0], r[1]) for r in self.allocated_clusters]
        total_size = sum(r[1] for r in regions)

        # Single-pass merge-scan; never loop check_region_recovered over 100k+ regions (O(N*M))
        total_recovered = self._total_recovered_bytes(regions)

        overall_pct = (total_recovered / total_size * 100) if total_size > 0 else 0

        print(f"\nFinal recovery status:")
        print(f"  Recovered: {total_recovered/(1024**3):.2f} GB ({overall_pct:.1f}%)")

        self.state["final_recovery_pct"] = overall_pct
        self.save_state()

        return True

    # =========================================================================
    # Helper methods for aggressive retry
    # =========================================================================
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

    # =========================================================================
    # STAGE 7 (Optional): Recover unknown regions
    # =========================================================================
    def stage7_recover_unknown(self):
        """Optionally recover clusters whose allocation status was unknown"""
        if not self.unknown_cluster_ranges:
            # Check state for saved unknown ranges
            if "unknown_ranges" in self.state:
                self.unknown_cluster_ranges = [(r[0], r[1], 0, 0) for r in self.state["unknown_ranges"]]

        if not self.unknown_cluster_ranges:
            print("\nNo unknown cluster ranges to recover.")
            return True

        print("\n" + "="*60)
        print("STAGE 7 (Optional): Recover Unknown Regions")
        print("="*60)

        unknown_total = sum(r[1] for r in self.unknown_cluster_ranges)
        print(f"\n{len(self.unknown_cluster_ranges)} regions totaling {unknown_total/(1024**3):.2f} GB")
        print("These clusters are from unrecovered portions of $Bitmap.")
        print("They might contain data, or might be empty - we couldn't tell.")

        options = {
            'y': "Yes, recover unknown regions (recommended for data recovery)",
            'n': "No, skip unknown regions"
        }
        choice = self.prompt_with_timeout(
            "Recover unknown cluster regions?",
            options,
            'y'
        )

        if choice == 'n':
            print("Skipping unknown regions.")
            return True

        # Check device exists before attempting recovery
        if not self.check_device_exists(self.source):
            print(f"\nERROR: Source device {self.source} is not accessible!")
            print("Cannot recover unknown regions - device may have disconnected.")
            return False

        # Create domain file for unknown regions
        regions = [(r[0], r[1]) for r in self.unknown_cluster_ranges]
        domain_path = self.create_domain_file(regions, "unknown_data_domain.txt")

        print(f"\nCreated domain file: {domain_path}")
        success = self.run_ddrescue(domain_path, "Unknown Cluster Regions")

        if not success:
            print("\nWARNING: ddrescue failed or device disappeared during unknown region recovery")

        # Check status — single-pass merge-scan; never loop check_region_recovered over large region sets (O(N*M))
        total_recovered = self._total_recovered_bytes(regions)

        overall_pct = (total_recovered / unknown_total * 100) if unknown_total > 0 else 0
        print(f"\nUnknown regions recovery: {overall_pct:.1f}%")

        return True

    # =========================================================================
    # STAGE 8: Aggressive retry for bad sectors
    # =========================================================================
    def stage8_aggressive_retry(self):
        """Retry recovery with aggressive settings, prioritized by importance"""
        print("\n" + "="*60)
        print("STAGE 8: Prioritized Aggressive Retry")
        print("="*60)

        # Define domains in priority order (most critical first)
        domains = [
            ("1. Critical Structures (GPT, boot, MFT header)",
             self.job_dir / "critical_structures_domain.txt", True),
            ("2. $Bitmap (needed to find allocated data)",
             self.job_dir / "bitmap_data_domain.txt", False),
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

        # Calculate combined domain stats
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

        choice = self.prompt_with_timeout(
            "How would you like to proceed?",
            options,
            's',  # Default to skip - user can always run again
            timeout=120
        )

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

            # Check device before each domain
            if not self.check_device_exists(self.source):
                print(f"\nERROR: Source device {self.source} is not accessible!")
                print("Cannot continue aggressive retry - device may have disconnected.")
                return False

            # Never show 100% if there are bad sectors
            display_pct = min(old_pct, 99.99) if bad_bytes > 0 else old_pct
            print(f"  Before: {display_pct:.2f}%, {bad_bytes:,} bytes in {bad_areas} bad areas")

            current_bad = bad_bytes

            # Pass 1: Standard aggressive (-r3)
            print(f"\n  --- Pass 1: Standard aggressive (-r3) ---")
            flags = ['-f', '-d', '-A', '-r3', '-M'] + (['-L'] if use_loose else [])
            rc = self._exec_ddrescue(flags, domain=domain_path)
            if rc == 130:
                print("\n  Aborted — stopping aggressive retry.")
                return False

            # Check after pass 1
            new_bad, new_areas, total, new_pct = self._get_domain_bad_sectors(domain_path)
            if new_bad < current_bad:
                print(f"\n  Pass 1 recovered: +{current_bad - new_bad:,} bytes")
                current_bad = new_bad

            # Pass 2: Extra aggressive (-r5) if still have bad sectors
            if new_bad > 0:
                print(f"\n  --- Pass 2: Extra aggressive (-r5) ---")
                flags = ['-f', '-d', '-A', '-r5', '-M'] + (['-L'] if use_loose else [])
                rc = self._exec_ddrescue(flags, domain=domain_path)
                if rc == 130:
                    print("\n  Aborted — stopping aggressive retry.")
                    return False

                # Check after pass 2
                new_bad, new_areas, total, new_pct = self._get_domain_bad_sectors(domain_path)
                if new_bad < current_bad:
                    print(f"\n  Pass 2 recovered: +{current_bad - new_bad:,} bytes")

            # Final status for this domain
            final_display_pct = min(new_pct, 99.99) if new_bad > 0 else 100.0
            print(f"\n  After: {final_display_pct:.2f}%, {new_bad:,} bytes in {new_areas} bad areas")

            improvement = bad_bytes - new_bad
            if improvement > 0:
                print(f"  Total improvement: +{improvement:,} bytes recovered!")
            elif new_bad == bad_bytes:
                print(f"  No change - these sectors may be permanently unreadable")

        # Final summary
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

        allocated_pct = (total_rescued_bytes / total_domain_bytes * 100) if total_domain_bytes > 0 else 0

        print(f"\nRecovery Summary:")
        print(f"  Allocated data recovered: {total_rescued_bytes/(1024**3):.2f} GB of {total_domain_bytes/(1024**3):.2f} GB")

        if final_bad_total == 0:
            print(f"  Allocated data status:    ✓ 100% COMPLETE")
        else:
            display_pct = min(allocated_pct, 99.99)
            print(f"  Allocated data status:    {display_pct:.2f}% ({final_bad_total:,} bytes unrecovered)")

        print(f"  Remaining bad sectors:    {final_bad_total:,} bytes in {final_areas_total} areas")

        if final_bad_total == 0:
            print("\n✓ All file data fully recovered!")
        else:
            print(f"\nThese {final_areas_total} bad areas may be permanently unreadable.")

        return True

    # =========================================================================
    # Main workflow
    # =========================================================================
    def run(self):
        """Run the full iterative recovery workflow"""
        print("="*60)
        print("ITERATIVE TARGETED RECOVERY")
        print("="*60)
        print(f"Source: {self.source}")
        print(f"Destination: {self.dest}")
        print(f"Log: {self.log_file}")
        print(f"Job directory: {self.job_dir}")
        print()

        # Check if this is a resume (state file has previous run data)
        is_resume = "source_identity" in self.state or "dest_identity" in self.state

        if is_resume:
            # Validate source and destination match previous run
            print("Resuming previous recovery job - validating drives...")

            # A drive may have re-enumerated to a different /dev node since the last run.
            # Locate each by its saved serial and update the path before validating.
            self._relocate_devices_on_resume()

            if "source_identity" in self.state:
                if not self._validate_drive_identity(self.source, self.state["source_identity"], "SOURCE"):
                    return False

            if "dest_identity" in self.state:
                if not self._validate_drive_identity(self.dest, self.state["dest_identity"], "DESTINATION"):
                    return False

            # Source identity just validated → the drive is back. Clear any stale
            # "disappeared" flag from a prior aborted run so it doesn't falsely abort
            # after stage 6 (it gets re-set live if the drive drops again this run).
            if self.state.get("device_disappeared"):
                self.state["device_disappeared"] = False
                self.save_state()

            print()
        else:
            # First run - save drive identities
            print("New recovery job - recording drive identities...")
            self.state["source_identity"] = self._get_drive_identity(self.source)
            self.state["dest_identity"] = self._get_drive_identity(self.dest)

            src_id = self.state["source_identity"]
            dst_id = self.state["dest_identity"]

            print(f"  Source:  {src_id.get('model', 'unknown')} / {src_id.get('serial', 'unknown')}")
            print(f"  Dest:    {dst_id.get('model', 'unknown')} / {dst_id.get('serial', 'unknown')}")

            self.save_state()
            print()

        # Early sanity check: destination should be at least as large as source
        if self.dest_size is not None and self.drive_size is not None:
            if self.dest_size < self.drive_size:
                print(f"WARNING: Destination ({self.dest_size/(1024**3):.1f} GB) is smaller than source ({self.drive_size/(1024**3):.1f} GB)")
                print("         This is OK for targeted recovery if allocated data fits.")
                print()

        # Stage 1: Boot sector
        if not self.stage1_boot_sector():
            print("FAILED at Stage 1: Boot sector")
            return False

        # Stage 2: MFT header
        if not self.stage2_mft_header():
            print("FAILED at Stage 2: MFT header")
            return False

        # Stage 3: $Bitmap recovery
        if not self.stage3_bitmap_recovery():
            print("WARNING at Stage 3: $Bitmap incomplete, continuing anyway...")

        # Stage 4: Parse $Bitmap
        if not self.stage4_parse_bitmap():
            print("FAILED at Stage 4: Parse $Bitmap")
            return False

        # Stage 5: Create data domain
        if not self.stage5_create_data_domain():
            print("FAILED at Stage 5: Create data domain")
            return False

        # Validate destination size before proceeding with large recovery
        total_allocated = self.state.get("total_allocated_bytes", 0)
        if total_allocated > 0:
            if not self.validate_destination_size(total_allocated):
                print("\nERROR: Destination is too small for the allocated data.")
                print("Please use a larger destination drive and restart.")
                return False

        # Stage 0 (bootable-first mode): Critical disk structures BEFORE data recovery
        if self.bootable_first and not self.state.get("critical_structures_recovered"):
            print("\n" + "="*60)
            print("Stage 0 (bootable-first): Critical Disk Structures")
            print("="*60)
            print("Recovering GPT, EFI, backup boot sector BEFORE data recovery...")
            if not self.stage0_critical_structures():
                print("WARNING: Critical structures incomplete - continuing with data recovery anyway")
            else:
                print("Critical structures recovered successfully")

        # Stage 5b: Priority user folder recovery (before full sweep)
        if not self.state.get("priority_recovery_done"):
            self.stage5b_priority_recovery()

        # Stage 6: Ask before recovering all data
        print("\n" + "="*60)
        print("Ready for Stage 6: Full Data Recovery")
        print("="*60)

        options = {
            'y': "Yes, proceed with full data recovery",
            'n': "No, skip data recovery"
        }
        choice = self.prompt_with_timeout(
            "Proceed with full data recovery?",
            options,
            'y'  # Default to yes
        )

        if choice != 'n':
            self.stage6_recover_data()

        # Check if device disappeared during data recovery
        if self.state.get("device_disappeared") or not self.check_device_exists(self.source):
            print("\n" + "="*60)
            print("SOURCE DEVICE DISCONNECTED")
            print("="*60)
            print(f"Device {self.source} is no longer accessible.")
            print("Recovery progress has been saved. You can resume when the device is reconnected.")
            print(f"\nFinal recovery status: {self.state.get('final_recovery_pct', 'unknown')}%")
            return False

        # Stage 7: Optional recovery of unknown regions
        self.stage7_recover_unknown()

        # Stage 8: Aggressive retry for any remaining bad sectors
        if self.check_device_exists(self.source):
            self.stage8_aggressive_retry()
        else:
            print("\nSkipping aggressive retry - source device not accessible")

        # Stage 0: Critical disk structures (GPT, EFI, backup boot sector)
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

        # Save the recovered $MFT so the visualizer's file-level map works.
        self._extract_mft_for_visualizer()

        print("\n" + "="*60)
        print("WORKFLOW COMPLETE")
        print("="*60)
        print("Destination should now be mountable when connected to another system.")

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
        if os.path.exists(self.log_file):
            try:
                with open(self.log_file, 'r') as f:
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
        print()

        # Disconnect-resilient: pauses + waits for the verified drive(s) if either drops.
        rc = self._exec_ddrescue(['-d', '-f'])
        if rc == 0:
            print(f"\nFull clone completed successfully!")
        elif rc == 130:
            print(f"\nClone interrupted - progress saved in log file.")
        else:
            print(f"\nClone finished with exit code {rc}")


def main():
    parser = argparse.ArgumentParser(
        description='Iterative targeted NTFS recovery with bootstrapped workflow',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  # Standard recovery (data-first, critical structures last)
  iterative-targeted-recovery.py /dev/sde /dev/sdc recovery.log ./job_example

  # Bootable-first (recover GPT/EFI/boot sectors BEFORE data recovery)
  iterative-targeted-recovery.py --bootable-first /dev/sde /dev/sdc recovery.log ./job_example
'''
    )

    parser.add_argument('source', help='Source device (e.g., /dev/sde)')
    parser.add_argument('dest', help='Destination device (e.g., /dev/sdc)')
    parser.add_argument('log', help='DDRescue log file path')
    parser.add_argument('job_dir', nargs='?', default='./recovery_job',
                        help='Job directory for state files (default: ./recovery_job)')
    parser.add_argument('--bootable-first', '-b', action='store_true',
                        help='Recover critical disk structures (GPT, EFI, backup boot) BEFORE data recovery')

    args = parser.parse_args()

    recovery = TargetedRecovery(
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
