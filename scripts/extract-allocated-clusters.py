#!/usr/bin/env python3
"""
extract-allocated-clusters.py - Fast extraction of allocated clusters from NTFS $Bitmap

Instead of parsing each file's cluster runs individually (slow for 100K+ files),
this reads the $Bitmap file which contains a bit for each cluster indicating
whether it's allocated or free.

Usage: sudo python3 extract-allocated-clusters.py <device> <partition_offset_sectors> <output_dir>
"""

import sys
import subprocess
import os
from pathlib import Path

def run_cmd(cmd, timeout=60):
    """Run command and return output"""
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return result.stdout, result.returncode
    except subprocess.TimeoutExpired:
        return "", -1

def get_fs_params(device, offset):
    """Get filesystem parameters using fsstat"""
    output, _ = run_cmd(f"fsstat -o {offset} {device}")

    params = {}
    for line in output.split('\n'):
        if 'Cluster Size:' in line:
            params['cluster_size'] = int(line.split()[-1])
        if 'Total Cluster Range:' in line:
            # Format: "Total Cluster Range: 0 - 488369917"
            parts = line.split()
            params['total_clusters'] = int(parts[-1]) + 1
        if 'Sector Size:' in line:
            params['sector_size'] = int(line.split()[-1])

    return params

def extract_bitmap(device, offset, output_dir):
    """Extract $Bitmap file (MFT entry 6)"""
    bitmap_path = f"{output_dir}/bitmap.raw"
    cmd = f"icat -o {offset} {device} 6 > {bitmap_path}"
    _, rc = run_cmd(cmd, timeout=300)
    if rc != 0:
        print(f"Error extracting $Bitmap")
        return None
    return bitmap_path

def parse_bitmap(bitmap_path, total_clusters, cluster_size, partition_byte_offset):
    """Parse bitmap to find allocated cluster ranges"""

    allocated_ranges = []
    current_start = None
    current_end = None

    with open(bitmap_path, 'rb') as f:
        bitmap_data = f.read()

    total_allocated = 0

    for byte_idx, byte in enumerate(bitmap_data):
        for bit_idx in range(8):
            cluster_num = byte_idx * 8 + bit_idx
            if cluster_num >= total_clusters:
                break

            is_allocated = bool(byte & (1 << bit_idx))

            if is_allocated:
                total_allocated += 1
                if current_start is None:
                    current_start = cluster_num
                    current_end = cluster_num
                else:
                    current_end = cluster_num
            else:
                if current_start is not None:
                    # End of an allocated range
                    start_byte = partition_byte_offset + (current_start * cluster_size)
                    size_bytes = (current_end - current_start + 1) * cluster_size
                    allocated_ranges.append((start_byte, size_bytes, current_start, current_end))
                    current_start = None
                    current_end = None

    # Don't forget last range
    if current_start is not None:
        start_byte = partition_byte_offset + (current_start * cluster_size)
        size_bytes = (current_end - current_start + 1) * cluster_size
        allocated_ranges.append((start_byte, size_bytes, current_start, current_end))

    return allocated_ranges, total_allocated

def create_ddrescue_domain(ranges, output_path, job_name=""):
    """Create ddrescue domain file from allocated ranges"""

    with open(output_path, 'w') as f:
        f.write("# Mapfile. Created by GNU ddrescue version 1.23\n")
        f.write(f"# Domain file for data clusters - {job_name}\n")
        f.write(f"# Contains {len(ranges)} allocated regions\n")
        f.write("# current_pos  current_status  current_pass\n")

        if ranges:
            f.write(f"0x{ranges[0][0]:X}     +               1\n")
        else:
            f.write("0x00000000     +               1\n")

        f.write("#      pos        size  status\n")

        for start_byte, size_bytes, _, _ in ranges:
            f.write(f"0x{start_byte:X}  0x{size_bytes:X}  +\n")

def main():
    if len(sys.argv) < 3:
        print("Usage: extract-allocated-clusters.py <device> <partition_offset_sectors> [output_dir]")
        print("Example: extract-allocated-clusters.py /dev/sdc 2048 ./analysis")
        sys.exit(1)

    device = sys.argv[1]
    partition_offset = int(sys.argv[2])
    output_dir = sys.argv[3] if len(sys.argv) > 3 else "./cluster_analysis"

    os.makedirs(output_dir, exist_ok=True)

    print("=" * 60)
    print("Allocated Cluster Extraction Tool")
    print("=" * 60)
    print(f"Device: {device}")
    print(f"Partition offset: {partition_offset} sectors")
    print(f"Output: {output_dir}")
    print()

    # Get filesystem parameters
    print("[1/4] Getting filesystem parameters...")
    params = get_fs_params(device, partition_offset)

    if 'cluster_size' not in params:
        print("Error: Could not determine cluster size")
        sys.exit(1)

    cluster_size = params['cluster_size']
    total_clusters = params.get('total_clusters', 0)
    sector_size = params.get('sector_size', 512)
    partition_byte_offset = partition_offset * sector_size

    print(f"  Cluster size: {cluster_size} bytes")
    print(f"  Total clusters: {total_clusters:,}")
    print(f"  Partition offset: {partition_byte_offset:,} bytes")
    print()

    # Extract $Bitmap
    print("[2/4] Extracting $Bitmap file...")
    bitmap_path = extract_bitmap(device, partition_offset, output_dir)
    if not bitmap_path:
        sys.exit(1)

    bitmap_size = os.path.getsize(bitmap_path)
    print(f"  Bitmap size: {bitmap_size:,} bytes ({bitmap_size * 8:,} bits)")
    print()

    # Parse bitmap
    print("[3/4] Parsing bitmap for allocated clusters...")
    ranges, total_allocated = parse_bitmap(bitmap_path, total_clusters, cluster_size, partition_byte_offset)

    total_allocated_gb = (total_allocated * cluster_size) / (1024**3)
    print(f"  Allocated clusters: {total_allocated:,} ({total_allocated_gb:.2f} GB)")
    print(f"  Contiguous ranges: {len(ranges):,}")
    print()

    # Create ddrescue domain file
    print("[4/4] Creating ddrescue domain file...")
    domain_path = f"{output_dir}/data_domain.txt"
    create_ddrescue_domain(ranges, domain_path, os.path.basename(output_dir))
    print(f"  Created: {domain_path}")

    # Also save human-readable summary
    summary_path = f"{output_dir}/cluster_summary.txt"
    with open(summary_path, 'w') as f:
        f.write(f"Cluster Analysis Summary\n")
        f.write(f"========================\n")
        f.write(f"Device: {device}\n")
        f.write(f"Partition offset: {partition_offset} sectors\n")
        f.write(f"Cluster size: {cluster_size} bytes\n")
        f.write(f"Total clusters: {total_clusters:,}\n")
        f.write(f"Allocated clusters: {total_allocated:,}\n")
        f.write(f"Allocated data: {total_allocated_gb:.2f} GB\n")
        f.write(f"Contiguous ranges: {len(ranges):,}\n")
        f.write(f"\n")
        f.write(f"Top 20 largest ranges:\n")
        sorted_ranges = sorted(ranges, key=lambda x: x[1], reverse=True)[:20]
        for start, size, cstart, cend in sorted_ranges:
            size_mb = size / (1024**2)
            f.write(f"  Clusters {cstart:,}-{cend:,}: {size_mb:.1f} MB at offset 0x{start:X}\n")

    print(f"  Summary: {summary_path}")
    print()
    print("=" * 60)
    print("Done!")
    print("=" * 60)
    print()
    print(f"Next step: Run ddrescue with domain file:")
    print(f"  ddrescue -f -d -m {domain_path} <source> <dest> <log>")

if __name__ == "__main__":
    main()
