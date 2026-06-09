#!/usr/bin/env python3
"""
iterative-targeted-recovery-fat.py - Bootstrapped FAT Targeted Recovery (FAT12/16/32)

Port of iterative-targeted-recovery.py to the FAT filesystem family. The FAT itself
serves as both the file table AND the allocation map, so the bootstrap chain is one
stage shorter than NTFS:

1. Recover boot sector (BPB) → parse → identify FAT12/16/32, locate FAT, cluster size
2. Recover FAT #1 + FAT #2 (mirror)
3. Parse FAT → walk non-zero, non-bad entries → list of allocated clusters
4. Create data recovery domain
5. Recover all allocated data clusters (THE MAIN EVENT)
6. (Optional) Recover clusters whose FAT entries were unreadable
7. Aggressive retry on failed regions
0. Recover critical disk structures (MBR/GPT, FSInfo, backup boot, both FATs) → mountable destination

Stage 0 runs LAST by default to prioritize file data, but ensures the destination is
immediately mountable. Use --bootable-first (-b) to run it before data recovery.

Input modes (autodetected):
- Partition-direct: source ends in a digit (e.g. /dev/sdi1) → partition_offset = 0,
  parent device derived for MBR/GPT recovery in Stage 0
- Whole-disk: source is a bare device (e.g. /dev/sdi) → MBR/GPT parsed to locate
  the FAT partition (types 0x01/0x04/0x06/0x0B/0x0C/0x0E or MS Basic Data GUID)

FAT12 support: implemented for completeness but UNTESTED in production. The bit-packed
entry decode path will print a warning at runtime when FAT12 is detected.

Usage: sudo python3 iterative-targeted-recovery-fat.py [--bootable-first] <source> <dest> <log> [job_dir]
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
from pathlib import Path

# Default timeout for prompts (in seconds)
DEFAULT_PROMPT_TIMEOUT = 300  # 5 minutes

class FATTargetedRecovery:
    def __init__(self, source, dest, log_file, job_dir, bootable_first=False):
        self.source = source
        self.dest = dest
        self.log_file = log_file
        self.job_dir = Path(job_dir)
        self.job_dir.mkdir(parents=True, exist_ok=True)
        self.bootable_first = bootable_first

        # FAT filesystem geometry — populated by stage1
        self.sector_size = 512
        self.bytes_per_sector = 512
        self.sectors_per_cluster = None
        self.cluster_size = None
        self.fat_type = None  # 12, 16, or 32
        self.num_fats = None
        self.fat_size_bytes = None
        self.fat1_byte_offset = None
        self.fat2_byte_offset = None  # None if NumFATs == 1
        self.root_dir_byte_offset = None  # FAT12/16 only
        self.root_dir_size_bytes = 0     # FAT12/16 only; 0 for FAT32
        self.first_data_byte_offset = None
        self.total_clusters = None
        self.root_cluster = None         # FAT32 only
        self.fsinfo_sector_rel = None    # FAT32 only, relative to partition start
        self.bk_boot_sector_rel = None   # FAT32 only, relative to partition start

        self.allocated_clusters = []      # list of (byte_start, byte_size, cluster_start, cluster_end)
        self.unknown_cluster_ranges = []  # list of (byte_start, byte_size, cluster_start, cluster_end)

        # State file to track progress
        self.state_file = self.job_dir / "recovery_state.json"
        self.state = self.load_state()

        # Restore cached FAT geometry from state (so resume doesn't need stage1 reparse)
        for key in ("bytes_per_sector", "sectors_per_cluster", "cluster_size", "fat_type",
                    "num_fats", "fat_size_bytes", "fat1_byte_offset", "fat2_byte_offset",
                    "root_dir_byte_offset", "root_dir_size_bytes", "first_data_byte_offset",
                    "total_clusters", "root_cluster", "fsinfo_sector_rel", "bk_boot_sector_rel"):
            if key in self.state:
                setattr(self, key, self.state[key])
        if self.bytes_per_sector:
            self.sector_size = self.bytes_per_sector

        # Detect input mode: partition-direct vs whole-disk
        # If source is a partition (e.g. /dev/sdi1), partition_offset = 0 and we derive
        # the parent device (/dev/sdi) for MBR/GPT recovery in Stage 0.
        self.parent_device = None
        self.partition_direct_mode = False
        self._detect_input_mode()

        # Get drive size (must be after _detect_input_mode so parent_device is set)
        self.drive_size = self._detect_drive_size()

        # Detect partition offset (or use saved value from state)
        if "partition_offset" in self.state:
            self.partition_offset = self.state["partition_offset"]
            print(f"Using saved partition offset: {self.partition_offset} sectors")
        elif self.partition_direct_mode:
            self.partition_offset = 0
            print("Partition-direct mode: partition_offset = 0")
        else:
            self.partition_offset = self._detect_partition_offset()

        # Detect destination size for validation
        self.dest_size = self._detect_dest_size()

    def _detect_input_mode(self):
        """Determine if source is a partition (sdi1) or whole disk (sdi).

        Uses /sys/class/block/<name>/partition — exists iff <name> is a partition.
        Sets self.partition_direct_mode and self.parent_device.
        """
        dev_name = os.path.basename(self.source)
        sys_block = f"/sys/class/block/{dev_name}"
        partition_marker = f"{sys_block}/partition"

        if not os.path.exists(partition_marker):
            print(f"Whole-disk mode: source {self.source} is not a partition")
            return

        self.partition_direct_mode = True

        # Derive parent device: /sys/class/block/sdi1 -> .../devices/.../sdi/sdi1
        # Parent name is the directory name one level up from realpath.
        try:
            real = os.path.realpath(sys_block)
            parent_name = os.path.basename(os.path.dirname(real))
            candidate_parent = f"/dev/{parent_name}"
            if os.path.exists(candidate_parent):
                self.parent_device = candidate_parent
                print(f"Partition-direct mode: source={self.source}, parent={self.parent_device}")
            else:
                print(f"Partition-direct mode: source={self.source}, parent device {candidate_parent} not found")
        except Exception as e:
            print(f"Partition-direct mode: source={self.source}, could not derive parent ({e})")

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
            with open(f"/sys/class/block/{device}/size") as f:
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
        Detect FAT partition offset by parsing MBR or GPT.
        Returns offset in sectors. Only called in whole-disk mode.
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

        # Check MBR signature (last 2 bytes should be 0x55 0xAA)
        if mbr[510:512] != b'\x55\xaa':
            print("WARNING: Invalid MBR signature, using default offset 2048")
            return 2048

        # Check if this is a protective MBR (GPT)
        # Partition type 0xEE in first partition entry indicates GPT
        first_partition_type = mbr[450]  # Offset 446 + 4 (type is at offset 4 in entry)

        if first_partition_type == 0xEE:
            # GPT disk - parse GPT partition table
            return self._parse_gpt_for_fat()
        else:
            # MBR disk - parse MBR partition table
            return self._parse_mbr_for_fat(mbr)

    # MBR partition type codes that indicate a FAT filesystem
    FAT_MBR_TYPES = {
        0x01: "FAT12",
        0x04: "FAT16 (<32MB)",
        0x06: "FAT16",
        0x0B: "FAT32 (CHS)",
        0x0C: "FAT32 (LBA)",
        0x0E: "FAT16 (LBA)",
    }

    def _parse_mbr_for_fat(self, mbr):
        """Parse MBR partition table to find a FAT partition.

        Recognizes types 0x01 (FAT12), 0x04/0x06 (FAT16), 0x0B/0x0C (FAT32), 0x0E (FAT16 LBA).
        """
        print("  Parsing MBR partition table...")

        # MBR partition table starts at offset 446, each entry is 16 bytes
        # Entry format: boot(1) + CHS_start(3) + type(1) + CHS_end(3) + LBA_start(4) + sectors(4)
        fat_partitions = []

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

            type_name = self.FAT_MBR_TYPES.get(partition_type, {
                0x07: "NTFS/HPFS/exFAT",
                0x83: "Linux",
                0x82: "Linux swap",
                0xEE: "GPT Protective",
            }.get(partition_type, f"0x{partition_type:02X}"))

            size_gb = (sector_count * 512) / (1024**3)
            print(f"    Partition {i+1}: type={type_name}, start={lba_start}, size={size_gb:.1f}GB")

            if partition_type in self.FAT_MBR_TYPES:
                fat_partitions.append((lba_start, sector_count, i+1, type_name))

        if not fat_partitions:
            print("  WARNING: No FAT partition found in MBR, using default offset 2048")
            return 2048

        if len(fat_partitions) == 1:
            offset = fat_partitions[0][0]
            print(f"  Found FAT partition ({fat_partitions[0][3]}) at sector {offset}")
            return offset

        # Multiple FAT partitions - pick the largest (most likely to be data)
        largest = max(fat_partitions, key=lambda x: x[1])
        print(f"  Multiple FAT partitions found, using largest (partition {largest[2]}, {largest[3]}) at sector {largest[0]}")
        return largest[0]

    def _parse_gpt_for_fat(self):
        """Parse GPT partition table to find a FAT partition (Microsoft Basic Data GUID)."""
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
        fat_partitions = []

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
                fat_partitions.append((start_lba, end_lba - start_lba + 1, i+1))
            else:
                # Print other partitions for reference
                guid_str = type_guid.hex()
                print(f"    Partition {i+1}: GUID={guid_str[:8]}..., start={start_lba}, size={size_gb:.1f}GB")

        if not fat_partitions:
            print("  WARNING: No Microsoft Basic Data partition found in GPT, using default offset 2048")
            return 2048

        # GPT's MS Basic Data GUID covers both NTFS and FAT — we'll let stage1 BPB parse
        # confirm filesystem type; this just identifies candidate partitions.
        if len(fat_partitions) == 1:
            offset = fat_partitions[0][0]
            print(f"  Found Microsoft Basic Data partition at sector {offset} (BPB parse will confirm FAT)")
            return offset

        # Multiple partitions - pick the largest
        largest = max(fat_partitions, key=lambda x: x[1])
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
            # Try stat for files
            import os
            if os.path.isfile(self.dest):
                size = os.path.getsize(self.dest)
                print(f"Detected destination file size: {size/(1024**3):.2f} GB")
                return size
        except:
            pass

        try:
            # Fall back to reading /sys/block
            device = os.path.basename(self.dest)
            with open(f"/sys/class/block/{device}/size") as f:
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
        print(f"\nChoice [{'/'.join(options.keys())}]: ", end='', flush=True)

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
                print(f"\nChoice [{'/'.join(options.keys())}]: ", end='', flush=True)
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

    def run_ddrescue(self, domain_file, description, loose_domain=False):
        """Run ddrescue with domain file.

        Returns:
            True if ddrescue completed successfully, False if device disappeared or error
        """
        print(f"\n{'='*60}")
        print(f"Running ddrescue: {description}")
        print(f"{'='*60}")
        print(f"Domain: {domain_file}")

        # Check if source device exists before running
        if not self.check_device_exists(self.source):
            print(f"\nERROR: Source device {self.source} is not accessible!")
            print("The drive may have disconnected or failed.")
            return False

        # Use -L (loose-domain) for files with gaps between regions
        L_flag = "-L " if loose_domain else ""
        cmd = f"ddrescue -f -d {L_flag}-m {domain_file} {self.source} {self.dest} {self.log_file}"
        print(f"Command: {cmd}")
        print()

        exit_code = os.system(cmd)  # Use os.system to show live output

        # Check if device still exists after ddrescue
        if not self.check_device_exists(self.source):
            print(f"\nWARNING: Source device {self.source} disappeared during recovery!")
            print("The drive may have disconnected or failed.")
            self.state["device_disappeared"] = True
            self.save_state()
            return False

        # ddrescue exit codes: 0=success, 1=some errors but usable, 2=fatal
        # os.system returns exit_code * 256
        if exit_code == 512:
            print("\nWARNING: ddrescue reported fatal errors")
            return False

        return True

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

            # Run ddrescue quietly
            cmd = f"ddrescue -f -d -q -m {domain_path} {self.source} {self.dest} {self.log_file}"
            os.system(cmd)

            recovered_so_far += size

        print(f"\nCompleted {len(regions)} regions")

    # =========================================================================
    # STAGE 0: Critical Disk Structures for FAT mountability
    # =========================================================================
    def stage0_critical_structures(self):
        """Recover critical structures needed for destination to be mountable.

        Whole-disk mode: MBR + (if GPT) GPT primary/backup tables + EFI System
        Partition + the FAT-internal critical structures below.

        Partition-direct mode: only FAT-internal structures (MBR/GPT belong to
        a different device — recover those with a separate ddrescue pass on the
        parent device if you need a bootable parent disk).

        FAT-internal critical structures:
          - Primary boot sector (sector 0 of partition, 512 bytes)
          - FSInfo sector (FAT32, usually sector 1 — value from BPB)
          - Backup boot sector (FAT32, usually sector 6 — value from BPB)
          - (FAT FAT-region top-up is left to Stage 2 and Stage 7 retries)
        """
        print("\n" + "="*60)
        print("STAGE 0: Critical Disk Structures")
        print("="*60)

        regions = []
        partition_byte_offset = self.partition_offset * self.sector_size

        if self.partition_direct_mode:
            print("  Partition-direct mode: skipping MBR/GPT recovery")
            print("  (Run a separate ddrescue on the parent device if you need a bootable parent disk)")
        else:
            # Whole-disk mode: include MBR/GPT regions
            # GPT primary (first 34 sectors, 17 KB) — also covers MBR/protective MBR
            gpt_primary_size = 34 * self.sector_size
            regions.append((0, gpt_primary_size, "MBR / GPT Primary"))
            print(f"  MBR / GPT Primary: sectors 0-33 ({gpt_primary_size} bytes)")

            # GPT backup (last 33 sectors of disk) — harmless on MBR-only disks
            gpt_backup_size = 33 * self.sector_size
            gpt_backup_start = self.drive_size - gpt_backup_size
            regions.append((gpt_backup_start, gpt_backup_size, "GPT Backup"))
            print(f"  GPT Backup: offset {gpt_backup_start} ({gpt_backup_size} bytes)")

            # EFI System Partition, if present (still useful even on FAT data drives)
            efi_region = self._find_efi_partition()
            if efi_region:
                regions.append((*efi_region, "EFI System Partition"))
                print(f"  EFI Partition: offset {efi_region[0]}, size {efi_region[1]/(1024**2):.1f} MB")

        # Primary boot sector of FAT partition (already done in Stage 1, but include for top-up)
        regions.append((partition_byte_offset, self.sector_size, "FAT Boot Sector"))
        print(f"  FAT Boot Sector: offset {partition_byte_offset} ({self.sector_size} bytes)")

        # FAT32: FSInfo + backup boot
        if self.fat_type == 32:
            fsinfo_rel = self.fsinfo_sector_rel if self.fsinfo_sector_rel else 1
            fsinfo_offset = partition_byte_offset + fsinfo_rel * self.sector_size
            regions.append((fsinfo_offset, self.sector_size, "FAT32 FSInfo Sector"))
            print(f"  FAT32 FSInfo: partition sector {fsinfo_rel}, offset {fsinfo_offset}")

            bk_rel = self.bk_boot_sector_rel if self.bk_boot_sector_rel else 6
            bk_offset = partition_byte_offset + bk_rel * self.sector_size
            # FAT32 backup is 3 consecutive sectors (boot + FSInfo backup + reserved)
            regions.append((bk_offset, 3 * self.sector_size, "FAT32 Backup Boot (3 sectors)"))
            print(f"  FAT32 Backup Boot: partition sector {bk_rel}, offset {bk_offset} (3 sectors)")

        if not regions:
            print("  WARNING: Could not detect any critical structures")
            return True

        # Create combined domain
        domain_regions = [(start, size) for start, size, _ in regions]
        domain_path = self.create_domain_file(domain_regions, "critical_structures_domain.txt")

        total_size = sum(r[1] for r in domain_regions)
        recovered = 0
        for start, size in domain_regions:
            pct = self.check_region_recovered(start, size)
            recovered += size * pct / 100

        if recovered >= total_size * 0.99:
            print(f"\n  Critical structures already recovered ({recovered/total_size*100:.1f}%)")
        else:
            print(f"\n  Recovery status: {recovered/total_size*100:.1f}%")

            if not self.check_device_exists(self.source):
                print(f"\n  ERROR: Source device {self.source} is not accessible!")
                return False

            success = self.run_ddrescue(domain_path, "Critical Disk Structures", loose_domain=True)
            if not success:
                print("\n  ERROR: ddrescue failed or device disappeared during critical structure recovery")
                return False

            recovered = 0
            for start, size in domain_regions:
                pct = self.check_region_recovered(start, size)
                recovered += size * pct / 100

            final_pct = (recovered / total_size * 100) if total_size > 0 else 0
            print(f"\n  Post-recovery status: {final_pct:.1f}%")
            if final_pct < 90:
                print(f"  WARNING: Only {final_pct:.1f}% of critical structures recovered")
                print("  The destination may not be mountable")

        self.state["critical_structures_recovered"] = True
        self.save_state()

        # Refresh destination partition table (only meaningful when dest is a block device
        # in whole-disk mode — partprobe on a partition or file is a no-op or error)
        if not self.partition_direct_mode and self._dest_is_block_device():
            print("\n  Refreshing destination partition table...")
            cmd = f"partprobe {self.dest} 2>/dev/null || blockdev --rereadpt {self.dest} 2>/dev/null"
            os.system(cmd)
            time.sleep(1)

        return True

    def _find_efi_partition(self):
        """Look for an EFI System Partition via fdisk -l on the source whole disk.

        Returns (byte_offset, byte_size) or None.
        """
        cmd = f"fdisk -l {self.source} 2>/dev/null"
        output, rc = self.run_cmd(cmd)
        for line in output.split('\n'):
            parts = line.split()
            if len(parts) < 5 or not parts[0].startswith(self.source):
                continue
            if 'EFI' not in line:
                continue
            for i, p in enumerate(parts):
                if p.isdigit():
                    try:
                        efi_start = int(p)
                        if i + 1 < len(parts) and parts[i+1].isdigit():
                            efi_end = int(parts[i+1])
                            return (efi_start * self.sector_size,
                                    (efi_end - efi_start + 1) * self.sector_size)
                    except ValueError:
                        pass
                    break
        return None

    def _dest_is_block_device(self):
        try:
            return os.path.exists(self.dest) and not os.path.isfile(self.dest)
        except Exception:
            return False

    # =========================================================================
    # STAGE 1: Boot Sector + BPB parse (replaces NTFS stages 1 & 2)
    # =========================================================================
    def stage1_boot_sector_and_fat_location(self):
        """Recover boot sector, parse the BPB, identify FAT type and locate the FAT.

        On FAT the boot sector tells us everything we need to locate both FATs in
        one shot — no separate MFT-header / bitmap-runs round-trip is needed.
        """
        print("\n" + "="*60)
        print("STAGE 1: Boot Sector + BPB Parse")
        print("="*60)

        partition_byte_offset = self.partition_offset * self.sector_size
        boot_size = 512

        # Recover primary boot sector
        pct = self.check_region_recovered(partition_byte_offset, boot_size)
        print(f"Boot sector at offset {partition_byte_offset} ({partition_byte_offset/(1024**3):.4f} GB)")
        print(f"Recovery status: {pct:.1f}%")

        if pct < 100:
            domain = self.create_domain_file([(partition_byte_offset, boot_size)], "boot_domain.txt")
            self.run_ddrescue(domain, "Boot Sector")
            pct = self.check_region_recovered(partition_byte_offset, boot_size)

        boot_data = self.extract_bytes(partition_byte_offset, boot_size) if pct >= 100 else b''

        # Try to parse primary BPB
        parsed = self._parse_bpb(boot_data, partition_byte_offset) if pct >= 100 else None

        # Fallback: try backup boot sector at sector 6 of the partition (FAT32 convention)
        if not parsed:
            print(f"\nPrimary boot sector unusable (recovered {pct:.1f}%, BPB parse failed).")
            print("Trying backup boot sector at sector 6 of partition (FAT32 convention)...")
            backup_offset = partition_byte_offset + 6 * self.sector_size
            backup_pct = self.check_region_recovered(backup_offset, boot_size)
            if backup_pct < 100:
                domain = self.create_domain_file([(backup_offset, boot_size)], "boot_backup_domain.txt")
                self.run_ddrescue(domain, "Backup Boot Sector (sector 6)")
                backup_pct = self.check_region_recovered(backup_offset, boot_size)

            if backup_pct >= 100:
                backup_data = self.extract_bytes(backup_offset, boot_size)
                parsed = self._parse_bpb(backup_data, partition_byte_offset)
                if parsed:
                    print("  Backup boot sector parsed successfully. Will use as authoritative BPB.")
                    # Write the recovered backup back into the primary slot in destination
                    # so the destination filesystem is mountable.
                    self._write_bytes_to_dest(partition_byte_offset, backup_data)

        if not parsed:
            # Both primary and backup unusable — offer fallback
            return self._handle_bpb_failure(partition_byte_offset)

        # Persist all geometry to state
        self._save_bpb_to_state()
        self._print_bpb_summary()
        return True

    def _parse_bpb(self, boot_data, partition_byte_offset):
        """Parse a candidate boot sector. Returns True on success, False on sanity-check failure.

        On success, populates self.bytes_per_sector, sectors_per_cluster, fat_type,
        fat1_byte_offset, fat2_byte_offset, root_dir_byte_offset, first_data_byte_offset,
        total_clusters, etc.
        """
        if len(boot_data) < 512:
            return False

        # Check 0x55 0xAA boot signature
        if boot_data[510:512] != b'\x55\xaa':
            print("  BPB sanity: missing 0x55AA boot signature")
            return False

        try:
            bytes_per_sector = struct.unpack('<H', boot_data[0x0B:0x0D])[0]
            sectors_per_cluster = boot_data[0x0D]
            rsvd_sec_cnt = struct.unpack('<H', boot_data[0x0E:0x10])[0]
            num_fats = boot_data[0x10]
            root_ent_cnt = struct.unpack('<H', boot_data[0x11:0x13])[0]
            tot_sec_16 = struct.unpack('<H', boot_data[0x13:0x15])[0]
            fat_sz_16 = struct.unpack('<H', boot_data[0x16:0x18])[0]
            tot_sec_32 = struct.unpack('<I', boot_data[0x20:0x24])[0]
            # FAT32-only fields
            fat_sz_32 = struct.unpack('<I', boot_data[0x24:0x28])[0]
            root_cluster = struct.unpack('<I', boot_data[0x2C:0x30])[0]
            fsinfo_sector_rel = struct.unpack('<H', boot_data[0x30:0x32])[0]
            bk_boot_sector_rel = struct.unpack('<H', boot_data[0x32:0x34])[0]
        except struct.error as e:
            print(f"  BPB sanity: struct.unpack failed ({e})")
            return False

        # Sanity checks — reject obviously corrupt BPBs before computing derived values
        problems = []
        if bytes_per_sector not in (512, 1024, 2048, 4096):
            problems.append(f"BytesPerSec={bytes_per_sector} not in {{512,1024,2048,4096}}")
        if sectors_per_cluster == 0 or (sectors_per_cluster & (sectors_per_cluster - 1)) != 0 or sectors_per_cluster > 128:
            problems.append(f"SecPerClus={sectors_per_cluster} not a power of 2 in [1,128]")
        if num_fats not in (1, 2):
            problems.append(f"NumFATs={num_fats} not in {{1,2}}")
        if rsvd_sec_cnt == 0:
            problems.append("RsvdSecCnt=0")
        fat_sz = fat_sz_16 if fat_sz_16 != 0 else fat_sz_32
        if fat_sz == 0:
            problems.append("Both FATSz16 and FATSz32 are 0")
        tot_sec = tot_sec_16 if tot_sec_16 != 0 else tot_sec_32
        if tot_sec == 0:
            problems.append("Both TotSec16 and TotSec32 are 0")
        if problems:
            print("  BPB sanity checks failed:")
            for p in problems:
                print(f"    - {p}")
            return False

        # Classify FAT type per Microsoft spec (CountOfClusters formula)
        root_dir_sectors = ((root_ent_cnt * 32) + (bytes_per_sector - 1)) // bytes_per_sector
        data_sec = tot_sec - (rsvd_sec_cnt + num_fats * fat_sz + root_dir_sectors)
        if data_sec <= 0:
            print(f"  BPB sanity: DataSec={data_sec} (TotSec={tot_sec} too small relative to header)")
            return False
        count_of_clusters = data_sec // sectors_per_cluster
        if count_of_clusters < 4085:
            fat_type = 12
        elif count_of_clusters < 65525:
            fat_type = 16
        else:
            fat_type = 32

        # FAT32-specific cross-checks
        if fat_type == 32 and fat_sz_32 == 0:
            print("  BPB sanity: detected FAT32 but FATSz32 is 0")
            return False
        if fat_type != 32 and fat_sz_16 == 0:
            print(f"  BPB sanity: detected FAT{fat_type} but FATSz16 is 0")
            return False
        # FAT32 requires RootEntCnt == 0 (root directory lives in a cluster chain).
        # A FAT32-shaped BPB with non-zero RootEntCnt either means the BPB is corrupt
        # or our classifier misfired — refuse to proceed rather than compute wrong offsets.
        if fat_type == 32 and root_ent_cnt != 0:
            print(f"  BPB sanity: FAT32 detected but RootEntCnt={root_ent_cnt} (spec requires 0)")
            return False

        # All checks passed — populate instance state
        self.bytes_per_sector = bytes_per_sector
        self.sector_size = bytes_per_sector
        self.sectors_per_cluster = sectors_per_cluster
        self.cluster_size = bytes_per_sector * sectors_per_cluster
        self.fat_type = fat_type
        self.num_fats = num_fats
        self.fat_size_bytes = fat_sz * bytes_per_sector
        self.fat1_byte_offset = partition_byte_offset + rsvd_sec_cnt * bytes_per_sector
        if num_fats >= 2:
            self.fat2_byte_offset = self.fat1_byte_offset + self.fat_size_bytes
        else:
            self.fat2_byte_offset = None
        if fat_type in (12, 16):
            self.root_dir_byte_offset = partition_byte_offset + (rsvd_sec_cnt + num_fats * fat_sz) * bytes_per_sector
            self.root_dir_size_bytes = root_dir_sectors * bytes_per_sector
            self.first_data_byte_offset = self.root_dir_byte_offset + self.root_dir_size_bytes
            self.root_cluster = None
            self.fsinfo_sector_rel = None
            self.bk_boot_sector_rel = None
        else:
            self.root_dir_byte_offset = None
            self.root_dir_size_bytes = 0
            self.first_data_byte_offset = partition_byte_offset + (rsvd_sec_cnt + num_fats * fat_sz) * bytes_per_sector
            self.root_cluster = root_cluster
            self.fsinfo_sector_rel = fsinfo_sector_rel
            self.bk_boot_sector_rel = bk_boot_sector_rel
        self.total_clusters = count_of_clusters

        if fat_type == 12:
            print("\n  ⚠️  FAT12 detected — FAT12 support is implemented but UNTESTED in production.")
            print("      Proceed with extra caution and verify destination mounts before trusting recovery.")

        return True

    def _print_bpb_summary(self):
        print(f"  Filesystem: FAT{self.fat_type}")
        print(f"  Bytes per sector: {self.bytes_per_sector}")
        print(f"  Sectors per cluster: {self.sectors_per_cluster}")
        print(f"  Cluster size: {self.cluster_size} bytes")
        print(f"  Number of FATs: {self.num_fats}")
        print(f"  FAT size: {self.fat_size_bytes/(1024**2):.2f} MB each")
        print(f"  FAT #1 offset: 0x{self.fat1_byte_offset:X} ({self.fat1_byte_offset/(1024**3):.3f} GB)")
        if self.fat2_byte_offset is not None:
            print(f"  FAT #2 offset: 0x{self.fat2_byte_offset:X} ({self.fat2_byte_offset/(1024**3):.3f} GB)")
        if self.fat_type in (12, 16):
            print(f"  Root dir region: 0x{self.root_dir_byte_offset:X}, {self.root_dir_size_bytes} bytes")
        else:
            print(f"  Root cluster: {self.root_cluster}")
            print(f"  FSInfo sector (rel): {self.fsinfo_sector_rel}")
            print(f"  Backup boot sector (rel): {self.bk_boot_sector_rel}")
        print(f"  First data offset: 0x{self.first_data_byte_offset:X}")
        print(f"  Total clusters: {self.total_clusters:,}")

    def _save_bpb_to_state(self):
        self.state["boot_parsed"] = True
        self.state["partition_offset"] = self.partition_offset
        for key in ("bytes_per_sector", "sectors_per_cluster", "cluster_size", "fat_type",
                    "num_fats", "fat_size_bytes", "fat1_byte_offset", "fat2_byte_offset",
                    "root_dir_byte_offset", "root_dir_size_bytes", "first_data_byte_offset",
                    "total_clusters", "root_cluster", "fsinfo_sector_rel", "bk_boot_sector_rel"):
            self.state[key] = getattr(self, key)
        self.save_state()

    def _handle_bpb_failure(self, partition_byte_offset):
        """Both primary and backup boot sectors are unusable. Prompt the user."""
        print("\n" + "="*60)
        print("BPB UNRECOVERABLE")
        print("="*60)
        print("Could not parse a valid BPB from either the primary boot sector or the")
        print("backup at sector 6. The drive may be severely damaged, or this partition")
        print("may not actually be FAT (check that you specified the right device).")

        options = {
            'a': "Abort (recommended — verify device first)",
            'c': "Clone the entire partition with plain ddrescue (no targeted recovery)",
            'r': "Retry boot sector with aggressive ddrescue (-A -r3 -M) then re-parse",
        }
        choice = self.prompt_with_timeout(
            "BPB parse failed. How would you like to proceed?",
            options,
            'a',
            timeout=120,
        )

        if choice == 'a':
            print("Aborted.")
            return False
        if choice == 'r':
            print("\nRetrying boot sector with -A -r3 -M...")
            boot_size = 512
            domain = self.create_domain_file([(partition_byte_offset, boot_size)], "boot_domain.txt")
            cmd = f"ddrescue -f -d -A -r3 -M -m {domain} {self.source} {self.dest} {self.log_file}"
            print(f"Command: {cmd}\n")
            os.system(cmd)
            # Recurse into stage1 once more
            return self.stage1_boot_sector_and_fat_location()
        if choice == 'c':
            print("\nFalling back to full-partition clone (no targeted recovery).")
            cmd = f"ddrescue -d -f {self.source} {self.dest} {self.log_file}"
            print(f"Command: {cmd}\n")
            ret = os.system(cmd)
            self.state["fell_back_to_full_clone"] = True
            self.save_state()
            if ret == 0:
                print("\nFull clone completed. Targeted recovery cannot continue without a")
                print("parseable BPB, but the clone itself succeeded — the destination should")
                print("contain whatever was readable from the source partition.")
            else:
                print(f"\nFull clone exited with code {ret}.")
            return False
        return False

    def extract_bytes(self, offset, size):
        """Extract bytes from destination at given offset (files and block devices both work)."""
        try:
            with open(self.dest, 'rb') as f:
                f.seek(offset)
                return f.read(size)
        except (OSError, IOError) as e:
            print(f"  WARNING: extract_bytes failed at offset {offset}: {e}")
            return b''

    def _write_bytes_to_dest(self, offset, data):
        """Write a small blob to destination at offset (used to copy backup boot → primary)."""
        try:
            with open(self.dest, 'r+b') as f:
                f.seek(offset)
                f.write(data)
        except (OSError, IOError) as e:
            print(f"  WARNING: Could not write backup boot to primary slot: {e}")

    # =========================================================================
    # STAGE 2: FAT recovery (FAT #1 + FAT #2 mirror)
    # =========================================================================
    def stage2_fat_recovery(self):
        """Recover FAT #1 and (if present) FAT #2.

        On FAT32 these are typically a few hundred MB combined — fast even on
        degraded media. FAT #2 is recovered alongside FAT #1 so stage 3 can
        fall back to it for any sectors where FAT #1 is unreadable.
        """
        print("\n" + "="*60)
        print("STAGE 2: FAT Recovery")
        print("="*60)

        regions = self._fat_regions()
        if not regions:
            print("ERROR: No FAT regions to recover (BPB not parsed?)")
            return False

        total_size = sum(r[1] for r in regions)

        # Show recovery status per region
        total_recovered = 0
        for start, size in regions:
            pct = self.check_region_recovered(start, size)
            total_recovered += size * pct / 100
            label = "FAT #1" if start == self.fat1_byte_offset else "FAT #2"
            print(f"  {label} at 0x{start:X} ({size/(1024**2):.2f} MB): {pct:.1f}% recovered")

        overall_pct = (total_recovered / total_size * 100) if total_size > 0 else 0
        print(f"\nOverall FAT recovery: {overall_pct:.1f}%")

        retry_level = 0
        while overall_pct < 100:
            if overall_pct >= 99.9:
                print(f"\nFAT is {overall_pct:.2f}% recovered — close enough to proceed.")
                break

            missing_bytes = total_size - total_recovered
            entry_width = {12: 1.5, 16: 2, 32: 4}.get(self.fat_type, 4)
            missing_entries = int(missing_bytes / entry_width)

            print(f"\nFAT is {overall_pct:.1f}% recovered")
            print(f"  Missing: {missing_bytes/(1024**2):.2f} MB (~{missing_entries:,} FAT entries / clusters)")

            default_choice = '3' if overall_pct >= 95 else '1'
            options = {
                '1': "Try to recover more (standard pass)",
                '2': "Try harder (with retries: -A -r3 -M)",
                '3': "Continue anyway (unrecoverable FAT sectors will be treated as 'unknown — allocated')",
                'q': "Quit",
            }
            choice = self.prompt_with_timeout(
                f"FAT is {overall_pct:.1f}% recovered. What would you like to do?",
                options,
                default_choice,
            )

            if choice == 'q':
                print("Aborted by user.")
                return False
            if choice == '3':
                print(f"Continuing with {overall_pct:.1f}% FAT recovery...")
                break
            extra_flags = "-A -r3 -M" if choice == '2' else ("-A" if retry_level > 0 else "")

            domain = self.create_domain_file(regions, "fat_domain.txt")
            print(f"\n{'='*60}")
            print(f"Running ddrescue: FAT regions")
            print(f"{'='*60}")
            cmd = f"ddrescue -f -d {extra_flags} -m {domain} {self.source} {self.dest} {self.log_file}"
            print(f"Command: {cmd}")
            os.system(cmd)

            total_recovered = 0
            for start, size in regions:
                pct = self.check_region_recovered(start, size)
                total_recovered += size * pct / 100
            overall_pct = (total_recovered / total_size * 100) if total_size > 0 else 0
            print(f"\nAfter recovery: {overall_pct:.1f}%")

            retry_level += 1

        self.state["fat_recovered_pct"] = overall_pct
        self.save_state()
        return True

    def _fat_regions(self):
        """Return list of (byte_offset, size_bytes) for all FAT copies."""
        if self.fat1_byte_offset is None or self.fat_size_bytes is None:
            return []
        regions = [(self.fat1_byte_offset, self.fat_size_bytes)]
        if self.fat2_byte_offset is not None:
            regions.append((self.fat2_byte_offset, self.fat_size_bytes))
        return regions

    # =========================================================================
    # STAGE 3: Parse FAT → list of allocated clusters
    # =========================================================================
    def stage3_parse_fat(self):
        """Walk both FATs from destination, build merged allocation view.

        For each cluster N:
          - If FAT #1's sector is recovered → use FAT #1 entry
          - Else if FAT #2's sector is recovered → use FAT #2 entry
          - Else → mark cluster as "unknown" (recover anyway in Stage 6)
        Non-zero, non-bad-marker entries indicate the cluster is allocated.
        """
        print("\n" + "="*60)
        print("STAGE 3: Parse FAT")
        print("="*60)

        if not self.fat1_byte_offset or not self.fat_size_bytes or self.total_clusters is None:
            print("ERROR: BPB geometry missing — Stage 1 must run first")
            return False

        # Extract FAT #1 from destination
        print(f"Extracting FAT #1 from destination ({self.fat_size_bytes/(1024**2):.2f} MB)...")
        fat1_data = self.extract_bytes(self.fat1_byte_offset, self.fat_size_bytes)
        if len(fat1_data) < self.fat_size_bytes:
            print(f"  WARNING: short read on FAT #1 ({len(fat1_data)}/{self.fat_size_bytes})")

        fat2_data = b''
        if self.fat2_byte_offset is not None:
            print(f"Extracting FAT #2 from destination ({self.fat_size_bytes/(1024**2):.2f} MB)...")
            fat2_data = self.extract_bytes(self.fat2_byte_offset, self.fat_size_bytes)
            if len(fat2_data) < self.fat_size_bytes:
                print(f"  WARNING: short read on FAT #2 ({len(fat2_data)}/{self.fat_size_bytes})")

        # Build sector-level "is recovered?" bitmap for each FAT so we know per-entry
        # whether to trust FAT #1, fall back to FAT #2, or mark unknown.
        fat1_sector_ok = self._build_sector_ok_map(self.fat1_byte_offset, self.fat_size_bytes)
        fat2_sector_ok = self._build_sector_ok_map(self.fat2_byte_offset, self.fat_size_bytes) if self.fat2_byte_offset is not None else None

        partition_byte_offset = self.partition_offset * self.sector_size
        max_cluster_by_drive = (self.drive_size - partition_byte_offset) // self.cluster_size
        max_cluster = min(self.total_clusters + 1, max_cluster_by_drive)
        print(f"  Total clusters from BPB: {self.total_clusters:,}")
        print(f"  Max cluster by drive size: {max_cluster_by_drive:,}")
        print(f"  Walking FAT entries 2..{max_cluster}...")

        allocated_runs = []   # contiguous-cluster runs of allocated entries
        unknown_runs = []     # contiguous-cluster runs where neither FAT was readable
        cur_alloc_start = None
        cur_alloc_prev = None
        cur_unk_start = None
        cur_unk_prev = None
        total_allocated = 0
        total_unknown = 0

        def flush_alloc():
            nonlocal cur_alloc_start, cur_alloc_prev
            if cur_alloc_start is not None:
                start_byte = self.first_data_byte_offset + (cur_alloc_start - 2) * self.cluster_size
                size_bytes = (cur_alloc_prev - cur_alloc_start + 1) * self.cluster_size
                allocated_runs.append((start_byte, size_bytes, cur_alloc_start, cur_alloc_prev))
                cur_alloc_start = None
                cur_alloc_prev = None

        def flush_unknown():
            nonlocal cur_unk_start, cur_unk_prev
            if cur_unk_start is not None:
                start_byte = self.first_data_byte_offset + (cur_unk_start - 2) * self.cluster_size
                size_bytes = (cur_unk_prev - cur_unk_start + 1) * self.cluster_size
                unknown_runs.append((start_byte, size_bytes, cur_unk_start, cur_unk_prev))
                cur_unk_start = None
                cur_unk_prev = None

        for cluster_num in range(2, max_cluster + 1):
            entry, source = self._read_fat_entry(
                cluster_num,
                fat1_data, fat1_sector_ok,
                fat2_data, fat2_sector_ok,
            )

            if source == 'unknown':
                # Neither FAT entry was readable
                flush_alloc()
                if cur_unk_start is None:
                    cur_unk_start = cluster_num
                cur_unk_prev = cluster_num
                total_unknown += 1
                continue

            # Both FATs (or just FAT #1) gave us a value. Classify it.
            is_allocated = self._fat_entry_is_allocated(entry)
            if is_allocated:
                flush_unknown()
                if cur_alloc_start is None:
                    cur_alloc_start = cluster_num
                cur_alloc_prev = cluster_num
                total_allocated += 1
            else:
                flush_alloc()
                flush_unknown()

        flush_alloc()
        flush_unknown()

        # FAT12/16: the root directory region is always allocated but doesn't appear in the FAT.
        if self.fat_type in (12, 16) and self.root_dir_size_bytes > 0:
            allocated_runs.insert(0, (self.root_dir_byte_offset, self.root_dir_size_bytes, 0, 0))
            print(f"  Including fixed root directory region: 0x{self.root_dir_byte_offset:X} "
                  f"({self.root_dir_size_bytes} bytes)")

        allocated_runs.sort(key=lambda x: x[0])
        self.allocated_clusters = allocated_runs
        self.unknown_cluster_ranges = unknown_runs

        total_data_bytes = sum(r[1] for r in allocated_runs)
        unknown_bytes = sum(r[1] for r in unknown_runs)

        print(f"\n  Allocated clusters: {total_allocated:,} in {len(allocated_runs):,} contiguous run(s)")
        print(f"  Allocated data: {total_data_bytes/(1024**3):.2f} GB")
        if unknown_runs:
            print(f"  Unknown (FAT unreadable) clusters: {total_unknown:,} in {len(unknown_runs):,} run(s)")
            print(f"  Unknown bytes: {unknown_bytes/(1024**3):.2f} GB — will be offered as optional recovery later")
            self.state["unknown_ranges"] = [(r[0], r[1]) for r in unknown_runs]

        # Save analysis summary
        summary_path = self.job_dir / "cluster_analysis.txt"
        with open(summary_path, 'w') as f:
            f.write("Cluster Analysis (FAT)\n")
            f.write("======================\n")
            f.write(f"Filesystem: FAT{self.fat_type}\n")
            f.write(f"Cluster size: {self.cluster_size} bytes\n")
            f.write(f"Total clusters from BPB: {self.total_clusters:,}\n\n")
            f.write(f"Allocated clusters: {total_allocated:,}\n")
            f.write(f"Allocated data: {total_data_bytes/(1024**3):.2f} GB\n")
            f.write(f"Contiguous allocated runs: {len(allocated_runs):,}\n")
            if unknown_runs:
                f.write(f"\nUnknown (unreadable FAT) clusters: {total_unknown:,}\n")
                f.write(f"Unknown bytes: {unknown_bytes/(1024**3):.2f} GB\n")
            f.write("\nTop 20 largest allocated runs:\n")
            for start, size, cstart, cend in sorted(allocated_runs, key=lambda x: x[1], reverse=True)[:20]:
                f.write(f"  Clusters {cstart:,}-{cend:,}: {size/(1024**2):.1f} MB at 0x{start:X}\n")
        print(f"  Analysis saved to {summary_path}")

        self.state["allocated_ranges_count"] = len(allocated_runs)
        self.state["total_allocated_bytes"] = total_data_bytes
        self.save_state()
        return True

    def _build_sector_ok_map(self, region_byte_offset, region_size):
        """Build a list (one entry per sector in the region) telling whether that sector
        is recovered in the ddrescue log. Used to decide per-FAT-entry trust."""
        if region_byte_offset is None:
            return None
        sector_count = (region_size + self.sector_size - 1) // self.sector_size
        ok = [False] * sector_count
        if not os.path.exists(self.log_file):
            return ok
        region_end = region_byte_offset + region_size
        with open(self.log_file) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                parts = line.split()
                if len(parts) < 3 or parts[2] != '+':
                    continue
                try:
                    pos = int(parts[0], 16)
                    sz = int(parts[1], 16)
                except ValueError:
                    continue
                ovl_start = max(pos, region_byte_offset)
                ovl_end = min(pos + sz, region_end)
                if ovl_end <= ovl_start:
                    continue
                first_sec = (ovl_start - region_byte_offset) // self.sector_size
                last_sec = (ovl_end - 1 - region_byte_offset) // self.sector_size
                for s in range(first_sec, last_sec + 1):
                    if 0 <= s < sector_count:
                        ok[s] = True
        return ok

    def _read_fat_entry(self, cluster_num, fat1_data, fat1_ok, fat2_data, fat2_ok):
        """Read FAT entry for cluster_num, with FAT #2 fallback.

        Returns (entry_value_int_or_None, source) where source ∈ {'fat1', 'fat2', 'unknown'}.
        """
        # Compute byte offset within the FAT for this entry
        if self.fat_type == 32:
            byte_offset = cluster_num * 4
            entry_width = 4
        elif self.fat_type == 16:
            byte_offset = cluster_num * 2
            entry_width = 2
        else:  # FAT12
            byte_offset = cluster_num + cluster_num // 2  # 1.5 bytes per entry
            entry_width = 2  # we read 2 bytes and pick 12 of them

        sector_idx = byte_offset // self.sector_size
        # FAT12 entries can straddle a sector boundary; check both sectors involved
        end_sector_idx = (byte_offset + entry_width - 1) // self.sector_size

        def sectors_ok(ok_map):
            if ok_map is None:
                return False
            for s in range(sector_idx, end_sector_idx + 1):
                if s >= len(ok_map) or not ok_map[s]:
                    return False
            return True

        def decode(fat_data):
            if byte_offset + entry_width > len(fat_data):
                return None
            if self.fat_type == 32:
                raw = struct.unpack('<I', fat_data[byte_offset:byte_offset+4])[0]
                return raw & 0x0FFFFFFF  # top 4 bits reserved
            if self.fat_type == 16:
                return struct.unpack('<H', fat_data[byte_offset:byte_offset+2])[0]
            # FAT12: 12 bits packed, two entries per 3 bytes
            pair = struct.unpack('<H', fat_data[byte_offset:byte_offset+2])[0]
            if cluster_num & 1:
                return pair >> 4
            return pair & 0x0FFF

        if sectors_ok(fat1_ok):
            val = decode(fat1_data)
            if val is not None:
                return val, 'fat1'
        if sectors_ok(fat2_ok):
            val = decode(fat2_data)
            if val is not None:
                return val, 'fat2'
        return None, 'unknown'

    def _fat_entry_is_allocated(self, entry):
        """Classify a FAT entry value.

        - 0 → free
        - bad-cluster marker → NOT allocated (filesystem flagged it; don't waste retries)
        - anything else (chain pointer or EOC) → allocated
        """
        if entry is None or entry == 0:
            return False
        if self.fat_type == 32:
            bad = 0x0FFFFFF7
        elif self.fat_type == 16:
            bad = 0xFFF7
        else:
            bad = 0x0FF7
        if entry == bad:
            return False
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
            print("ERROR: No allocated clusters found")
            return False

        # Convert to format for domain file (just start, size)
        regions = [(r[0], r[1]) for r in self.allocated_clusters]

        domain_path = self.create_domain_file(regions, "all_data_domain.txt")

        total_size = sum(r[1] for r in regions)
        print(f"Created domain file: {domain_path}")
        print(f"  Regions: {len(regions):,}")
        print(f"  Total size: {total_size/(1024**3):.2f} GB")

        # Check current recovery status
        total_recovered = 0
        for start, size in regions:
            pct = self.check_region_recovered(start, size)
            total_recovered += size * pct / 100

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

        # Use -L (loose-domain) flag to allow gaps between regions
        success = self.run_ddrescue(domain_path, "All Allocated Data", loose_domain=True)

        if not success:
            print("\nWARNING: ddrescue failed or device disappeared during data recovery")
            # Still check what we got before failing

        # Check final status
        regions = [(r[0], r[1]) for r in self.allocated_clusters]
        total_size = sum(r[1] for r in regions)
        total_recovered = 0

        for start, size in regions:
            pct = self.check_region_recovered(start, size)
            total_recovered += size * pct / 100

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
    # STAGE 6 (Optional): Recover unknown regions
    # =========================================================================
    def stage6_recover_unknown(self):
        """Optionally recover clusters whose allocation status was unknown"""
        if not self.unknown_cluster_ranges:
            # Check state for saved unknown ranges
            if "unknown_ranges" in self.state:
                self.unknown_cluster_ranges = [(r[0], r[1], 0, 0) for r in self.state["unknown_ranges"]]

        if not self.unknown_cluster_ranges:
            print("\nNo unknown cluster ranges to recover.")
            return True

        print("\n" + "="*60)
        print("STAGE 6 (Optional): Recover Unknown Regions")
        print("="*60)

        unknown_total = sum(r[1] for r in self.unknown_cluster_ranges)
        print(f"\n{len(self.unknown_cluster_ranges)} regions totaling {unknown_total/(1024**3):.2f} GB")
        print("These clusters fall in FAT sectors that couldn't be read from either FAT #1 or FAT #2.")
        print("They might contain file data, or might be free - we couldn't tell from the FAT.")

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

        # Check status
        total_recovered = 0
        for start, size in regions:
            pct = self.check_region_recovered(start, size)
            total_recovered += size * pct / 100

        overall_pct = (total_recovered / unknown_total * 100) if unknown_total > 0 else 0
        print(f"\nUnknown regions recovery: {overall_pct:.1f}%")

        return True

    # =========================================================================
    # STAGE 7: Aggressive retry for bad sectors
    # =========================================================================
    def stage7_aggressive_retry(self):
        """Retry recovery with aggressive settings, prioritized by importance"""
        print("\n" + "="*60)
        print("STAGE 7: Prioritized Aggressive Retry")
        print("="*60)

        # Define domains in priority order (most critical first)
        domains = [
            ("1. Critical Structures (MBR/GPT, boot, FSInfo, backup boot)",
             self.job_dir / "critical_structures_domain.txt", True),
            ("2. FAT (needed to find allocated data)",
             self.job_dir / "fat_domain.txt", False),
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

            if "source_identity" in self.state:
                if not self._validate_drive_identity(self.source, self.state["source_identity"], "SOURCE"):
                    return False

            if "dest_identity" in self.state:
                if not self._validate_drive_identity(self.dest, self.state["dest_identity"], "DESTINATION"):
                    return False

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

        # Stage 1: Boot sector + BPB parse
        if not self.stage1_boot_sector_and_fat_location():
            print("FAILED at Stage 1: Boot sector / BPB parse")
            return False

        # Stage 2: FAT recovery (FAT #1 + FAT #2)
        if not self.stage2_fat_recovery():
            print("WARNING at Stage 2: FAT incomplete, continuing anyway...")

        # Stage 3: Parse FAT
        if not self.stage3_parse_fat():
            print("FAILED at Stage 3: Parse FAT")
            return False

        # Stage 4: Create data domain
        if not self.stage4_create_data_domain():
            print("FAILED at Stage 4: Create data domain")
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
            print("Recovering MBR/GPT, FSInfo, backup boot sector BEFORE data recovery...")
            if not self.stage0_critical_structures():
                print("WARNING: Critical structures incomplete - continuing with data recovery anyway")
            else:
                print("Critical structures recovered successfully")

        # Stage 5: Ask before recovering all data
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
            'y'  # Default to yes
        )

        if choice != 'n':
            self.stage5_recover_data()

        # Check if device disappeared during data recovery
        if self.state.get("device_disappeared") or not self.check_device_exists(self.source):
            print("\n" + "="*60)
            print("SOURCE DEVICE DISCONNECTED")
            print("="*60)
            print(f"Device {self.source} is no longer accessible.")
            print("Recovery progress has been saved. You can resume when the device is reconnected.")
            print(f"\nFinal recovery status: {self.state.get('final_recovery_pct', 'unknown')}%")
            return False

        # Stage 6: Optional recovery of unknown regions
        self.stage6_recover_unknown()

        # Stage 7: Aggressive retry for any remaining bad sectors
        if self.check_device_exists(self.source):
            self.stage7_aggressive_retry()
        else:
            print("\nSkipping aggressive retry - source device not accessible")

        # Stage 0: Critical structures (MBR/GPT in whole-disk mode + FSInfo/backup boot)
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
        print(f"Command: ddrescue -d -f {self.source} {self.dest} {self.log_file}")
        print()

        try:
            result = subprocess.run(
                ['ddrescue', '-d', '-f', self.source, self.dest, self.log_file],
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
        description='Iterative targeted FAT12/16/32 recovery with bootstrapped workflow',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  # Partition-direct (recommended when you know which partition is FAT)
  iterative-targeted-recovery-fat.py /dev/sdi1 /dev/sdc recovery.log ./job_iomega

  # Whole-disk (script will locate the FAT partition via MBR/GPT)
  iterative-targeted-recovery-fat.py /dev/sdi /dev/sdc recovery.log ./job_iomega

  # Bootable-first (recover MBR/GPT/FSInfo/backup boot BEFORE data recovery)
  iterative-targeted-recovery-fat.py --bootable-first /dev/sdi1 /dev/sdc recovery.log ./job_iomega
'''
    )

    parser.add_argument('source', help='Source device or partition (e.g., /dev/sdi or /dev/sdi1)')
    parser.add_argument('dest', help='Destination device or image file (e.g., /dev/sdc or /tmp/rescue.img)')
    parser.add_argument('log', help='DDRescue log file path')
    parser.add_argument('job_dir', nargs='?', default='./recovery_job',
                        help='Job directory for state files (default: ./recovery_job)')
    parser.add_argument('--bootable-first', '-b', action='store_true',
                        help='Recover critical disk structures (MBR/GPT, FSInfo, backup boot) BEFORE data recovery')

    args = parser.parse_args()

    recovery = FATTargetedRecovery(
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
