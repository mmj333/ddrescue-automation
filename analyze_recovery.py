#!/usr/bin/env python3
"""
DDRescue + NTFS Analysis Tool
Analyzes ddrescue logs and NTFS structures to enable targeted recovery.
"""

import sys
import os
import subprocess
import struct
from pathlib import Path
from dataclasses import dataclass
from typing import List, Tuple, Optional

# NTFS constants
NTFS_BOOT_SECTOR_SIZE = 512
SECTOR_SIZE = 512

@dataclass
class DdrescueBlock:
    """Represents a block from ddrescue log"""
    pos: int
    size: int
    status: str  # '+' = rescued, '*' = bad, '?' = untried

    @property
    def end(self) -> int:
        return self.pos + self.size

@dataclass
class NtfsInfo:
    """NTFS filesystem information"""
    bytes_per_sector: int = 512
    sectors_per_cluster: int = 8
    mft_cluster: int = 0
    mft_mirror_cluster: int = 0
    total_sectors: int = 0
    partition_offset: int = 0  # bytes

    @property
    def cluster_size(self) -> int:
        return self.bytes_per_sector * self.sectors_per_cluster

    @property
    def mft_offset(self) -> int:
        """MFT offset in bytes from start of disk"""
        return self.partition_offset + (self.mft_cluster * self.cluster_size)


def parse_ddrescue_log(log_path: str) -> Tuple[List[DdrescueBlock], dict]:
    """Parse a ddrescue log file and return blocks + metadata"""
    blocks = []
    metadata = {}

    with open(log_path, 'r') as f:
        for line in f:
            line = line.strip()
            if line.startswith('#'):
                # Parse metadata comments
                if 'current_pos' in line:
                    continue
                if ':' in line:
                    parts = line[1:].strip().split(':', 1)
                    if len(parts) == 2:
                        metadata[parts[0].strip()] = parts[1].strip()
                continue

            if not line or not line.startswith('0x'):
                continue

            parts = line.split()
            if len(parts) >= 3:
                try:
                    pos = int(parts[0], 16)
                    size = int(parts[1], 16)
                    status = parts[2]
                    if status in ['+', '*', '?', '/', '-']:
                        blocks.append(DdrescueBlock(pos, size, status))
                except ValueError:
                    # Skip malformed lines (like the current position line)
                    continue

    return blocks, metadata


def analyze_blocks(blocks: List[DdrescueBlock]) -> dict:
    """Analyze blocks and return statistics"""
    stats = {
        'rescued': 0,
        'bad': 0,
        'untried': 0,
        'total': 0,
        'rescued_ranges': [],
        'bad_ranges': [],
        'untried_ranges': []
    }

    for block in blocks:
        stats['total'] += block.size
        if block.status == '+':
            stats['rescued'] += block.size
            stats['rescued_ranges'].append((block.pos, block.end))
        elif block.status == '*':
            stats['bad'] += block.size
            stats['bad_ranges'].append((block.pos, block.end))
        elif block.status == '?':
            stats['untried'] += block.size
            stats['untried_ranges'].append((block.pos, block.end))

    return stats


def check_region_status(blocks: List[DdrescueBlock], start: int, end: int) -> dict:
    """Check how much of a specific region has been rescued"""
    rescued = 0
    bad = 0
    untried = 0

    for block in blocks:
        # Check overlap with region
        overlap_start = max(block.pos, start)
        overlap_end = min(block.end, end)

        if overlap_start < overlap_end:
            overlap_size = overlap_end - overlap_start
            if block.status == '+':
                rescued += overlap_size
            elif block.status == '*':
                bad += overlap_size
            elif block.status == '?':
                untried += overlap_size

    total = end - start
    return {
        'rescued': rescued,
        'bad': bad,
        'untried': untried,
        'total': total,
        'rescued_pct': (rescued / total * 100) if total > 0 else 0
    }


def format_size(size_bytes: int) -> str:
    """Format byte size to human readable"""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if abs(size_bytes) < 1024:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.2f} PB"


def read_ntfs_boot_sector(device: str, partition_offset: int = 0) -> Optional[NtfsInfo]:
    """Read NTFS boot sector and extract filesystem info"""
    try:
        with open(device, 'rb') as f:
            f.seek(partition_offset)
            boot_sector = f.read(NTFS_BOOT_SECTOR_SIZE)

            # Check NTFS signature
            if boot_sector[3:7] != b'NTFS':
                print(f"Warning: Not an NTFS boot sector at offset {partition_offset}")
                return None

            info = NtfsInfo()
            info.partition_offset = partition_offset

            # Parse BPB (BIOS Parameter Block)
            info.bytes_per_sector = struct.unpack('<H', boot_sector[0x0B:0x0D])[0]
            info.sectors_per_cluster = boot_sector[0x0D]
            info.total_sectors = struct.unpack('<Q', boot_sector[0x28:0x30])[0]

            # MFT cluster number
            info.mft_cluster = struct.unpack('<Q', boot_sector[0x30:0x38])[0]
            info.mft_mirror_cluster = struct.unpack('<Q', boot_sector[0x38:0x40])[0]

            return info
    except PermissionError:
        print("Error: Need root privileges to read device")
        return None
    except Exception as e:
        print(f"Error reading boot sector: {e}")
        return None


def print_analysis_report(log_path: str, device: str = None, partition_offset: int = 1048576):
    """Print comprehensive analysis report"""
    print("=" * 70)
    print("DDRescue Recovery Analysis Report")
    print("=" * 70)

    # Parse log
    blocks, metadata = parse_ddrescue_log(log_path)
    stats = analyze_blocks(blocks)

    print(f"\nLog file: {log_path}")
    if 'Command line' in metadata:
        print(f"Command: {metadata['Command line']}")
    if 'Start time' in metadata:
        print(f"Started: {metadata['Start time']}")

    print(f"\n--- Overall Statistics ---")
    print(f"Total size:     {format_size(stats['total'])}")
    print(f"Rescued (+):    {format_size(stats['rescued'])} ({stats['rescued']/stats['total']*100:.2f}%)")
    print(f"Bad sectors (*): {format_size(stats['bad'])} ({stats['bad']/stats['total']*100:.4f}%)")
    print(f"Non-tried (?):  {format_size(stats['untried'])} ({stats['untried']/stats['total']*100:.2f}%)")

    # Check critical regions for NTFS
    print(f"\n--- Critical Region Analysis ---")

    # Boot sector region (first 1MB + a bit more for GPT)
    boot_region = check_region_status(blocks, 0, 2*1024*1024)
    print(f"Boot sector (0-2MB):     {boot_region['rescued_pct']:.1f}% rescued")

    # Partition start (assuming GPT with partition at 1MB offset)
    part_start = partition_offset
    ntfs_boot = check_region_status(blocks, part_start, part_start + 512)
    print(f"NTFS boot sector:        {ntfs_boot['rescued_pct']:.1f}% rescued")

    # Try to get NTFS info if device provided
    ntfs_info = None
    if device and os.path.exists(device):
        ntfs_info = read_ntfs_boot_sector(device, partition_offset)

        if ntfs_info:
            print(f"\n--- NTFS Filesystem Info ---")
            print(f"Bytes per sector:    {ntfs_info.bytes_per_sector}")
            print(f"Sectors per cluster: {ntfs_info.sectors_per_cluster}")
            print(f"Cluster size:        {format_size(ntfs_info.cluster_size)}")
            print(f"MFT cluster:         {ntfs_info.mft_cluster}")
            print(f"MFT offset:          {format_size(ntfs_info.mft_offset)}")

            # Check MFT region (MFT is typically several hundred MB)
            mft_start = ntfs_info.mft_offset
            mft_size = 256 * 1024 * 1024  # Check first 256MB of MFT area
            mft_region = check_region_status(blocks, mft_start, mft_start + mft_size)
            print(f"\nMFT region ({format_size(mft_start)} - {format_size(mft_start + mft_size)}):")
            print(f"  Rescued: {mft_region['rescued_pct']:.1f}%")

            if mft_region['rescued_pct'] > 90:
                print("  ✓ MFT appears to be mostly recovered!")
            elif mft_region['rescued_pct'] > 50:
                print("  ⚠ MFT partially recovered - may be usable")
            else:
                print("  ✗ MFT not sufficiently recovered - need to target this area")

    # Create visual map
    print(f"\n--- Recovery Map (each char = ~{format_size(stats['total']//70)}) ---")
    create_visual_map(blocks, stats['total'], width=70)

    return blocks, stats, ntfs_info


def create_visual_map(blocks: List[DdrescueBlock], total_size: int, width: int = 70):
    """Create ASCII visual map of recovery status"""
    chunk_size = total_size // width
    map_line = []

    for i in range(width):
        chunk_start = i * chunk_size
        chunk_end = (i + 1) * chunk_size

        # Count status in this chunk
        rescued = 0
        bad = 0
        untried = 0

        for block in blocks:
            overlap_start = max(block.pos, chunk_start)
            overlap_end = min(block.end, chunk_end)

            if overlap_start < overlap_end:
                overlap_size = overlap_end - overlap_start
                if block.status == '+':
                    rescued += overlap_size
                elif block.status == '*':
                    bad += overlap_size
                else:
                    untried += overlap_size

        total = rescued + bad + untried
        if total == 0:
            map_line.append(' ')
        elif bad > total * 0.5:
            map_line.append('X')  # Mostly bad
        elif rescued > total * 0.8:
            map_line.append('█')  # Mostly rescued
        elif rescued > total * 0.5:
            map_line.append('▓')  # Partially rescued
        elif rescued > total * 0.2:
            map_line.append('▒')  # Some rescued
        elif rescued > 0:
            map_line.append('░')  # Little rescued
        else:
            map_line.append('·')  # Nothing rescued

    print(''.join(map_line))
    print("Legend: █=rescued ▓▒░=partial ·=untried X=bad")


def main():
    if len(sys.argv) < 2:
        print("Usage: analyze_recovery.py <ddrescue_log> [device] [partition_offset_bytes]")
        print("\nExample:")
        print("  analyze_recovery.py recovery.log")
        print("  analyze_recovery.py recovery.log /dev/sdc 1048576")
        sys.exit(1)

    log_path = sys.argv[1]
    device = sys.argv[2] if len(sys.argv) > 2 else None
    partition_offset = int(sys.argv[3]) if len(sys.argv) > 3 else 1048576  # Default 1MB

    if not os.path.exists(log_path):
        print(f"Error: Log file not found: {log_path}")
        sys.exit(1)

    print_analysis_report(log_path, device, partition_offset)


if __name__ == '__main__':
    main()
