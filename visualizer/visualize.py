#!/usr/bin/env python3
"""
Recovery Visualizer - Block Map + File Mapping
Generates an interactive HTML visualization of ddrescue recovery status.

Usage:
    # Phase 1: Block map only
    python3 visualize.py --log /path/to/recovery.log --output map.html

    # Phase 2: With file mapping from recovery job
    python3 visualize.py --job /path/to/recovery_job/ --output map.html
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import List, Tuple, Dict, Optional, Any
from dataclasses import dataclass
from bisect import bisect_right


@dataclass
class Region:
    """A contiguous region from ddrescue log."""
    start: int
    size: int
    status: str

    @property
    def end(self) -> int:
        return self.start + self.size


def parse_size(size_str: str) -> int:
    """Parse human-readable size like '2TB', '500GB', '1024' to bytes."""
    size_str = size_str.strip().upper()
    multipliers = {
        'B': 1,
        'K': 1024,
        'KB': 1024,
        'M': 1024**2,
        'MB': 1024**2,
        'G': 1024**3,
        'GB': 1024**3,
        'T': 1024**4,
        'TB': 1024**4,
    }

    match = re.match(r'^(\d+(?:\.\d+)?)\s*([A-Z]*)?$', size_str)
    if not match:
        raise ValueError(f"Cannot parse size: {size_str}")

    value = float(match.group(1))
    unit = match.group(2) or 'B'

    if unit not in multipliers:
        raise ValueError(f"Unknown unit: {unit}")

    return int(value * multipliers[unit])


def format_size(size_bytes: int) -> str:
    """Format bytes as human-readable string."""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}" if size_bytes != int(size_bytes) else f"{int(size_bytes)} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"


def parse_ddrescue_log(log_path: Path) -> Tuple[List[Region], Dict]:
    """
    Parse ddrescue log file into list of regions.

    Returns:
        Tuple of (regions list, metadata dict)
    """
    regions = []
    metadata = {
        'version': None,
        'command': None,
        'start_time': None,
        'current_time': None,
        'status_line': None,
        'current_pos': None,
        'current_status': None,
        'current_pass': None,
    }

    with open(log_path, 'r') as f:
        for line in f:
            line = line.strip()

            # Skip empty lines
            if not line:
                continue

            # Parse comments for metadata
            if line.startswith('#'):
                if 'Created by GNU ddrescue' in line:
                    match = re.search(r'version (\d+\.\d+)', line)
                    if match:
                        metadata['version'] = match.group(1)
                elif 'Command line:' in line:
                    metadata['command'] = line.split(':', 1)[1].strip()
                elif 'Start time:' in line:
                    metadata['start_time'] = line.split(':', 1)[1].strip()
                elif 'Current time:' in line:
                    metadata['current_time'] = line.split(':', 1)[1].strip()
                elif 'current_pos' in line and 'current_status' in line:
                    # This is the header line, next non-comment line is status
                    pass
                continue

            # Parse data lines: pos size status
            parts = line.split()
            if len(parts) >= 3:
                try:
                    pos = int(parts[0], 16)
                    size = int(parts[1], 16)
                    status = parts[2]

                    # First line after header might be current position line
                    if metadata['current_pos'] is None and len(parts) >= 3:
                        # Check if this looks like a status line (single char status, pass number)
                        if len(status) == 1 and status in '+-?*/' and len(parts) == 3:
                            # This could be status line or data line
                            # Status line has: pos status pass
                            # Data line has: pos size status
                            # Heuristic: if third field is a number, it's likely status line
                            try:
                                int(parts[2])
                                # It's a number, so this is status line
                                metadata['current_pos'] = pos
                                metadata['current_status'] = parts[1]
                                metadata['current_pass'] = parts[2]
                                continue
                            except ValueError:
                                pass

                    if status in '+-?*/' and size > 0:
                        regions.append(Region(pos, size, status))
                except (ValueError, IndexError):
                    continue

    return regions, metadata


def calc_display_block_size(drive_size: int, target_blocks: int = 200000) -> int:
    """Calculate optimal display block size based on drive size."""
    block_size = drive_size // target_blocks
    # Round up to power of 2 for cleaner math, minimum 1MB
    if block_size < 1024 * 1024:
        return 1024 * 1024  # 1MB minimum

    # Find next power of 2
    power = 1
    while power < block_size:
        power *= 2
    return power


def aggregate_blocks(regions: List[Region], drive_size: int, block_size: int) -> List[str]:
    """
    Aggregate regions into fixed-size display blocks.

    Each display block gets the 'worst' status of any region it overlaps:
    Priority: - (bad) > * (non-scraped) > / (non-trimmed) > ? (untried) > + (rescued)
    """
    # Status priority (higher = worse)
    status_priority = {'+': 0, '?': 1, '/': 2, '*': 3, '-': 4}

    num_blocks = (drive_size + block_size - 1) // block_size
    blocks = ['+'] * num_blocks  # Default to rescued (best case)

    # Build sorted list of region starts for binary search
    region_starts = [r.start for r in regions]

    for block_idx in range(num_blocks):
        block_start = block_idx * block_size
        block_end = block_start + block_size

        # Find regions that might overlap this block using binary search
        # Start from region that could overlap (start <= block_end)
        idx = bisect_right(region_starts, block_end) - 1
        if idx < 0:
            idx = 0

        # Check regions that might overlap
        worst_priority = 0
        worst_status = '+'

        for i in range(max(0, idx - 1), len(regions)):
            r = regions[i]

            # Region starts after block ends - no more overlaps possible
            if r.start >= block_end:
                break

            # Check if region overlaps block
            if r.start < block_end and r.end > block_start:
                priority = status_priority.get(r.status, 0)
                if priority > worst_priority:
                    worst_priority = priority
                    worst_status = r.status

        blocks[block_idx] = worst_status

    return blocks


def calc_stats(regions: List[Region], drive_size: int) -> Dict:
    """Calculate recovery statistics."""
    stats = {
        'total_bytes': drive_size,
        'rescued_bytes': 0,
        'bad_bytes': 0,
        'non_scraped_bytes': 0,
        'non_trimmed_bytes': 0,
        'untried_bytes': 0,
    }

    status_map = {
        '+': 'rescued_bytes',
        '-': 'bad_bytes',
        '*': 'non_scraped_bytes',
        '/': 'non_trimmed_bytes',
        '?': 'untried_bytes',
    }

    for r in regions:
        key = status_map.get(r.status)
        if key:
            stats[key] += r.size

    # Calculate percentages
    total = stats['total_bytes'] or 1
    stats['rescued_pct'] = stats['rescued_bytes'] / total * 100
    stats['bad_pct'] = stats['bad_bytes'] / total * 100
    stats['non_scraped_pct'] = stats['non_scraped_bytes'] / total * 100
    stats['non_trimmed_pct'] = stats['non_trimmed_bytes'] / total * 100
    stats['untried_pct'] = stats['untried_bytes'] / total * 100

    return stats


def generate_html(blocks: List[str], stats: Dict, metadata: Dict,
                  block_size: int, output_path: Path,
                  file_entries: Optional[Dict] = None,
                  file_intervals: Optional[List] = None,
                  job_state: Optional[Dict] = None,
                  regions: Optional[List[Region]] = None,
                  job_dir: Optional[Path] = None) -> None:
    """Generate self-contained HTML file with canvas visualization and file browser."""

    # Calculate canvas dimensions
    pixels_per_block = 3
    max_width = 1200
    blocks_per_row = max_width // pixels_per_block
    num_rows = (len(blocks) + blocks_per_row - 1) // blocks_per_row
    canvas_width = blocks_per_row * pixels_per_block
    canvas_height = num_rows * pixels_per_block

    # Convert blocks to JSON for embedding
    blocks_json = json.dumps(blocks)
    stats_json = json.dumps(stats)
    metadata_json = json.dumps(metadata)

    # Get cluster/block size for calculations
    cluster_size = job_state.get('cluster_size', job_state.get('block_size', 4096)) if job_state else 4096

    # Get partition offset - NTFS saves in sectors, HFS+ saves in bytes
    partition_offset = job_state.get('partition_offset', 0) if job_state else 0
    # NTFS stores partition_offset in sectors (typically 2048 = 1MB)
    # HFS+ stores in bytes (typically ~411000000 for partition 2)
    # Heuristic: if offset < 1000000 and there's an mft_cluster key, it's likely NTFS in sectors
    if partition_offset > 0 and partition_offset < 1000000 and job_state and 'mft_cluster' in job_state:
        sector_size = job_state.get('sector_size', 512)
        partition_offset = partition_offset * sector_size

    # Prepare file mapping data if available
    file_tree_json = '{}'
    file_stats_json = '{}'

    # Get allocated space info from job state for accurate percentage calc
    total_allocated_bytes = job_state.get('total_allocated_bytes', 0) if job_state else 0
    data_recovery_pct = 0  # Will be calculated from file-level data below
    domain_recovery_pct = 0  # Will be calculated from domain file if available
    domain_recovered_bytes = 0
    domain_total_bytes = 0

    # Calculate domain-based recovery (most accurate - matches ddrescue output)
    if regions and job_dir:
        domain_file = job_dir / 'all_data_domain.txt'
        if domain_file.exists():
            try:
                domain_ranges = parse_domain_file(domain_file)
                recovered_intervals = build_recovered_intervals(regions)
                domain_recovered_bytes, domain_total_bytes, domain_recovery_pct = calc_domain_recovery(
                    domain_ranges, recovered_intervals)
                print(f"  Domain recovery: {domain_recovery_pct:.2f}% ({domain_recovered_bytes/(1024**3):.2f} GB / {domain_total_bytes/(1024**3):.2f} GB)")
            except Exception as e:
                print(f"  Warning: Could not calculate domain recovery: {e}")

    if file_entries and file_intervals and regions:
        # Calculate recovery status for each file
        recovered_intervals = build_recovered_intervals(regions)
        file_status = calc_file_recovery(file_entries, file_intervals, recovered_intervals,
                                         cluster_size, partition_offset)

        # Calculate overall recovery % using $Bitmap-based total (more accurate than MFT sizes)
        # Only count bytes from actual on-disk extents (not resident file sizes which may be corrupt)
        # Compare recovered on-disk extent bytes to TOTAL on-disk extent bytes (like
        # units). The old code divided the actual-size-capped numerator by the $Bitmap
        # total (mixed units), which understated a 100%-recovered drive as ~19%.
        total_extent_bytes = 0
        recovered_extent_bytes = 0
        for fs in file_status.values():
            # Only count files that actually have on-disk extents (not resident files)
            if fs.get('has_extents', True):  # Default True for backward compat
                total_extent_bytes += fs.get('extent_total', 0)
                recovered_extent_bytes += fs.get('extent_recovered', 0)

        if total_extent_bytes > 0:
            data_recovery_pct = (recovered_extent_bytes / total_extent_bytes) * 100
            print(f"  Recovery of file data: {data_recovery_pct:.2f}% ({recovered_extent_bytes/(1024**3):.2f} GB / {total_extent_bytes/(1024**3):.2f} GB)")

        # Build file tree
        file_tree = build_file_tree(file_entries, file_status)
        file_tree_json = json.dumps(file_tree)
        file_stats_json = json.dumps(file_tree['stats'])

        # Convert file entries to simpler format for JSON
        files_data = {}
        for rec_num, entry in file_entries.items():
            status_info = file_status.get(rec_num, {})
            files_data[rec_num] = {
                'name': entry.name,
                'path': entry.full_path or entry.name,
                'size': entry.size,
                'is_dir': entry.is_directory,
                'recovery_pct': status_info.get('recovery_pct', 0),
                'status': status_info.get('status', 'unknown'),
            }
        files_json = json.dumps(files_data)

        # Convert intervals to JSON (start, end, file_id)
        intervals_json = json.dumps(file_intervals)
    elif file_entries and file_intervals:
        # No regions, can't calculate recovery status
        files_data = {}
        for rec_num, entry in file_entries.items():
            files_data[rec_num] = {
                'name': entry.name,
                'path': entry.full_path or entry.name,
                'size': entry.size,
                'is_dir': entry.is_directory,
            }
        files_json = json.dumps(files_data)
        intervals_json = json.dumps(file_intervals)
    else:
        files_json = '{}'
        intervals_json = '[]'

    # Get bitmap recovery info from job state
    bitmap_recovery_pct = job_state.get('bitmap_recovered_pct', 100) if job_state else 100
    # data_recovery_pct was calculated above from file-level data, or 0 if no files

    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Recovery Visualizer</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #1a1a2e;
            color: #eee;
            min-height: 100vh;
        }}
        .main-layout {{
            display: flex;
            height: 100vh;
        }}
        .map-panel {{
            flex: 1;
            padding: 20px;
            overflow: auto;
        }}
        .file-panel {{
            width: 450px;
            background: #0f0f23;
            border-left: 1px solid #333;
            display: flex;
            flex-direction: column;
            overflow: hidden;
        }}
        .file-panel.collapsed {{
            width: 40px;
        }}
        .file-panel.collapsed .file-panel-content {{
            display: none;
        }}
        .panel-toggle {{
            position: absolute;
            right: 450px;
            top: 50%;
            transform: translateY(-50%);
            background: #16213e;
            border: 1px solid #333;
            color: #888;
            padding: 10px 5px;
            cursor: pointer;
            border-radius: 4px 0 0 4px;
            z-index: 100;
        }}
        .panel-toggle:hover {{ background: #1e3a5f; color: #fff; }}
        h1 {{ margin-bottom: 20px; color: #fff; font-size: 1.5em; }}

        .stats-bar {{
            display: flex;
            gap: 15px;
            flex-wrap: wrap;
            margin-bottom: 15px;
            padding: 12px;
            background: #16213e;
            border-radius: 8px;
        }}
        .stat {{
            display: flex;
            align-items: center;
            gap: 6px;
        }}
        .stat-dot {{
            width: 10px;
            height: 10px;
            border-radius: 2px;
        }}
        .stat-value {{ font-weight: bold; font-size: 0.9em; }}
        .stat-label {{ color: #888; font-size: 0.8em; }}

        .progress-bar {{
            height: 20px;
            background: #333;
            border-radius: 4px;
            overflow: hidden;
            margin-bottom: 15px;
            display: flex;
        }}
        .progress-segment {{ height: 100%; }}

        .canvas-container {{
            background: #0f0f23;
            border-radius: 8px;
            padding: 10px;
            overflow: auto;
        }}
        #blockmap {{
            display: block;
            image-rendering: pixelated;
            cursor: crosshair;
        }}

        .tooltip {{
            position: fixed;
            background: rgba(0, 0, 0, 0.95);
            color: #fff;
            padding: 10px 14px;
            border-radius: 6px;
            font-size: 12px;
            pointer-events: none;
            z-index: 1000;
            display: none;
            max-width: 350px;
            box-shadow: 0 4px 12px rgba(0,0,0,0.5);
        }}
        .tooltip-row {{ display: flex; justify-content: space-between; gap: 15px; margin: 2px 0; }}
        .tooltip-label {{ color: #888; }}
        .tooltip-value {{ font-family: monospace; }}

        .legend {{
            display: flex;
            gap: 15px;
            margin-top: 10px;
            flex-wrap: wrap;
            font-size: 0.85em;
        }}
        .legend-item {{ display: flex; align-items: center; gap: 5px; }}
        .legend-color {{ width: 14px; height: 14px; border-radius: 2px; }}

        .metadata {{
            margin-top: 15px;
            padding: 12px;
            background: #16213e;
            border-radius: 8px;
            font-size: 0.8em;
            color: #888;
        }}
        .metadata code {{
            color: #aaa;
            background: #0f0f23;
            padding: 2px 5px;
            border-radius: 3px;
        }}

        /* File Browser Panel */
        .file-header {{
            padding: 15px;
            background: #16213e;
            border-bottom: 1px solid #333;
        }}
        .file-header h2 {{
            font-size: 1.1em;
            margin-bottom: 10px;
        }}
        .file-stats {{
            display: flex;
            gap: 12px;
            font-size: 0.85em;
            flex-wrap: wrap;
        }}
        .file-stat {{
            display: flex;
            align-items: center;
            gap: 5px;
        }}
        .file-stat-icon {{
            width: 8px;
            height: 8px;
            border-radius: 50%;
        }}
        .filter-buttons {{
            display: flex;
            gap: 5px;
            margin-top: 10px;
        }}
        .filter-btn {{
            padding: 5px 10px;
            background: #1a1a2e;
            border: 1px solid #333;
            color: #888;
            border-radius: 4px;
            cursor: pointer;
            font-size: 0.8em;
        }}
        .filter-btn:hover {{ background: #16213e; color: #fff; }}
        .filter-btn.active {{ background: #1e3a5f; color: #fff; border-color: #3b82f6; }}

        .search-box {{
            margin-top: 10px;
        }}
        .search-box input {{
            width: 100%;
            padding: 8px 10px;
            background: #1a1a2e;
            border: 1px solid #333;
            border-radius: 4px;
            color: #fff;
            font-size: 0.85em;
        }}
        .search-box input:focus {{
            outline: none;
            border-color: #3b82f6;
        }}

        .file-tree-container {{
            flex: 1;
            overflow: auto;
            padding: 10px;
        }}
        .file-tree {{
            font-size: 0.85em;
            line-height: 1.6;
        }}
        .tree-item {{
            cursor: pointer;
            padding: 2px 0;
            white-space: nowrap;
        }}
        .tree-item:hover {{
            background: rgba(255,255,255,0.05);
        }}
        .tree-folder {{
            color: #60a5fa;
        }}
        .tree-folder::before {{
            content: '\\25B6';
            display: inline-block;
            width: 12px;
            font-size: 0.7em;
            transition: transform 0.15s;
        }}
        .tree-folder.open::before {{
            transform: rotate(90deg);
        }}
        .tree-children {{
            margin-left: 16px;
            display: none;
        }}
        .tree-folder.open + .tree-children {{
            display: block;
        }}
        .tree-file {{
            margin-left: 16px;
            display: flex;
            align-items: center;
            gap: 6px;
        }}
        .tree-file-icon {{
            width: 8px;
            height: 8px;
            border-radius: 50%;
            flex-shrink: 0;
        }}
        .tree-file-name {{
            overflow: hidden;
            text-overflow: ellipsis;
        }}
        .tree-file-size {{
            color: #666;
            font-size: 0.85em;
            margin-left: auto;
            padding-left: 10px;
        }}
        .tree-file-pct {{
            font-size: 0.85em;
            min-width: 45px;
            text-align: right;
        }}
        .status-recovered {{ color: #22c55e; }}
        .status-partial {{ color: #f59e0b; }}
        .status-unrecovered {{ color: #ef4444; }}
        .hidden {{ display: none !important; }}
    </style>
</head>
<body>
    <div class="main-layout">
        <div class="map-panel">
            <h1>Recovery Visualizer</h1>

            <div class="stats-bar" id="stats-bar"></div>
            <div class="progress-bar" id="progress-bar"></div>

            <div class="canvas-container">
                <canvas id="blockmap" width="{canvas_width}" height="{canvas_height}"></canvas>
            </div>

            <div class="legend">
                <div class="legend-item"><div class="legend-color" style="background: #22c55e;"></div><span>Rescued</span></div>
                <div class="legend-item"><div class="legend-color" style="background: #ef4444;"></div><span>Bad sectors</span></div>
                <div class="legend-item"><div class="legend-color" style="background: #6b7280;"></div><span>Untried</span></div>
                <div class="legend-item"><div class="legend-color" style="background: #f59e0b;"></div><span>Non-scraped</span></div>
                <div class="legend-item"><div class="legend-color" style="background: #a855f7;"></div><span>Non-trimmed</span></div>
            </div>

            <div class="metadata" id="metadata"></div>
        </div>

        <div class="file-panel" id="file-panel">
            <div class="file-header">
                <h2>File Recovery Status</h2>
                <div class="file-stats" id="file-stats"></div>
                <div class="filter-buttons" id="filter-buttons">
                    <button class="filter-btn active" data-filter="all">All</button>
                    <button class="filter-btn" data-filter="recovered">Recovered</button>
                    <button class="filter-btn" data-filter="partial">Partial</button>
                    <button class="filter-btn" data-filter="unrecovered">Unrecovered</button>
                </div>
                <div class="tree-controls" style="margin-top: 8px;">
                    <button class="filter-btn" id="expand-all">Expand All</button>
                    <button class="filter-btn" id="collapse-all">Collapse All</button>
                </div>
                <div class="search-box">
                    <input type="text" id="file-search" placeholder="Search files...">
                </div>
            </div>
            <div class="file-tree-container">
                <div class="file-tree" id="file-tree"></div>
            </div>
        </div>

        <div class="tooltip" id="tooltip"></div>
    </div>

    <script>
    const blocks = {blocks_json};
    const stats = {stats_json};
    const metadata = {metadata_json};
    const blockSize = {block_size};
    const pixelsPerBlock = {pixels_per_block};
    const blocksPerRow = {blocks_per_row};

    // File mapping data (Phase 2)
    const files = {files_json};
    const fileIntervals = {intervals_json};
    const clusterSize = {cluster_size};
    const partitionOffset = {partition_offset};
    const hasFileMapping = Object.keys(files).length > 0;

    // File tree data (Phase 3)
    const fileTree = {file_tree_json};
    const fileStats = {file_stats_json};
    let currentFilter = 'all';
    let searchQuery = '';

    // Job state data for used space calculations
    const totalAllocatedBytes = {total_allocated_bytes};
    const dataRecoveryPct = {data_recovery_pct};
    const bitmapRecoveryPct = {bitmap_recovery_pct};
    const domainRecoveryPct = {domain_recovery_pct};
    const domainRecoveredBytes = {domain_recovered_bytes};
    const domainTotalBytes = {domain_total_bytes};
    const hasAllocatedData = totalAllocatedBytes > 0;
    const hasDomainData = domainTotalBytes > 0;
    // Confidence based on how much MFT/Bitmap we recovered
    const confidencePct = Math.min(bitmapRecoveryPct, hasFileMapping ? 95 : 50);

    const statusColors = {{
        '+': '#22c55e',  // green - rescued
        '-': '#ef4444',  // red - bad
        '?': '#6b7280',  // gray - untried
        '*': '#f59e0b',  // yellow/orange - non-scraped
        '/': '#a855f7',  // purple - non-trimmed
    }};

    const statusNames = {{
        '+': 'Rescued',
        '-': 'Bad sector',
        '?': 'Untried',
        '*': 'Non-scraped',
        '/': 'Non-trimmed',
    }};

    function formatBytes(bytes) {{
        const units = ['B', 'KB', 'MB', 'GB', 'TB'];
        let i = 0;
        while (bytes >= 1024 && i < units.length - 1) {{
            bytes /= 1024;
            i++;
        }}
        return bytes.toFixed(bytes < 10 ? 2 : 1) + ' ' + units[i];
    }}

    function formatHex(num) {{
        return '0x' + num.toString(16).toUpperCase().padStart(10, '0');
    }}

    // Render stats bar
    function renderStats() {{
        const statsBar = document.getElementById('stats-bar');

        // Calculate files fully recovered percentage
        const totalFiles = fileStats?.total || 0;
        const fullyRecoveredFiles = fileStats?.recovered || 0;
        const partialFiles = fileStats?.partial || 0;
        const filesFullyRecoveredPct = totalFiles > 0 ? (fullyRecoveredFiles / totalFiles * 100) : 0;

        // If we have domain data, show all three stats
        if (hasDomainData) {{
            const usedSpace = domainTotalBytes;
            const freeSpace = stats.total_bytes - usedSpace;
            const domainPct = domainRecoveryPct.toFixed(1);
            const filePct = dataRecoveryPct.toFixed(1);
            const filesPct = filesFullyRecoveredPct.toFixed(1);
            const remainingBytes = usedSpace - domainRecoveredBytes;

            statsBar.innerHTML = `
                <div style="width: 100%; margin-bottom: 10px; padding-bottom: 10px; border-bottom: 1px solid #333;">
                    <div style="color: #888; font-size: 0.85em; margin-bottom: 8px;">
                        Targeted: <strong style="color: #fff;">${{formatBytes(usedSpace)}}</strong> of user data /
                        <strong style="color: #fff;">${{formatBytes(freeSpace)}}</strong> free space (skipped)
                    </div>
                    <div style="display: flex; flex-direction: column; gap: 8px;">
                        <div style="display: flex; gap: 20px; flex-wrap: wrap; align-items: center;">
                            <div class="stat">
                                <div class="stat-dot" style="background: #22c55e;"></div>
                                <span class="stat-value">${{domainPct}}%</span>
                                <span class="stat-label">of allocated space recovered (${{formatBytes(domainRecoveredBytes)}})</span>
                            </div>
                            <div class="stat">
                                <div class="stat-dot" style="background: #f59e0b;"></div>
                                <span class="stat-value">${{(100 - domainRecoveryPct).toFixed(1)}}%</span>
                                <span class="stat-label">remaining (${{formatBytes(remainingBytes)}})</span>
                            </div>
                        </div>
                        <div style="display: flex; gap: 20px; flex-wrap: wrap; align-items: center; padding-top: 5px; border-top: 1px solid #333;">
                            <div class="stat">
                                <div class="stat-dot" style="background: #3b82f6;"></div>
                                <span class="stat-value">${{filePct}}%</span>
                                <span class="stat-label">of file content recovered</span>
                            </div>
                            <div class="stat">
                                <div class="stat-dot" style="background: #8b5cf6;"></div>
                                <span class="stat-value">${{filesPct}}%</span>
                                <span class="stat-label">of files fully recovered (${{fullyRecoveredFiles.toLocaleString()}} / ${{totalFiles.toLocaleString()}})</span>
                            </div>
                        </div>
                    </div>
                </div>
                <div style="color: #666; font-size: 0.8em; margin-top: 8px;">
                    Whole drive: ${{stats.rescued_pct.toFixed(1)}}% rescued, ${{stats.untried_pct.toFixed(1)}}% untried, ${{stats.bad_pct.toFixed(3)}}% bad
                </div>
                <div style="color: #888; font-size: 0.75em; margin-top: 6px; font-style: italic;">
                    Confidence: ${{confidencePct.toFixed(0)}}% (based on ${{bitmapRecoveryPct.toFixed(1)}}% $Bitmap + ${{hasFileMapping ? 'MFT parsed' : 'no MFT'}})
                </div>
            `;
        }} else if (hasAllocatedData) {{
            // Fallback to file-based calculation
            const usedSpace = totalAllocatedBytes;
            const freeSpace = stats.total_bytes - usedSpace;
            const rescuedOfUsed = (dataRecoveryPct).toFixed(1);
            const remainingOfUsed = (100 - dataRecoveryPct).toFixed(1);
            const rescuedBytes = usedSpace * dataRecoveryPct / 100;
            const remainingBytes = usedSpace - rescuedBytes;

            statsBar.innerHTML = `
                <div style="width: 100%; margin-bottom: 10px; padding-bottom: 10px; border-bottom: 1px solid #333;">
                    <div style="color: #888; font-size: 0.85em; margin-bottom: 5px;">
                        Drive reports: <strong style="color: #fff;">${{formatBytes(usedSpace)}}</strong> used /
                        <strong style="color: #fff;">${{formatBytes(freeSpace)}}</strong> free
                        <span style="color: #666; font-size: 0.9em;">(from $Bitmap)</span>
                    </div>
                    <div style="display: flex; gap: 15px; flex-wrap: wrap;">
                        <div class="stat">
                            <div class="stat-dot" style="background: #22c55e;"></div>
                            <span class="stat-value">${{rescuedOfUsed}}%</span>
                            <span class="stat-label">of used space rescued (${{formatBytes(rescuedBytes)}})</span>
                        </div>
                        <div class="stat">
                            <div class="stat-dot" style="background: #f59e0b;"></div>
                            <span class="stat-value">${{remainingOfUsed}}%</span>
                            <span class="stat-label">remaining (${{formatBytes(remainingBytes)}})</span>
                        </div>
                    </div>
                </div>
                <div style="color: #666; font-size: 0.8em; margin-top: 8px;">
                    Whole drive: ${{stats.rescued_pct.toFixed(1)}}% rescued, ${{stats.untried_pct.toFixed(1)}}% untried, ${{stats.bad_pct.toFixed(3)}}% bad
                </div>
                <div style="color: #888; font-size: 0.75em; margin-top: 6px; font-style: italic;">
                    Confidence: ${{confidencePct.toFixed(0)}}% (based on ${{bitmapRecoveryPct.toFixed(1)}}% $Bitmap + ${{hasFileMapping ? 'MFT parsed' : 'no MFT'}})
                </div>
            `;
        }} else {{
            // Fallback: show whole drive stats
            const items = [
                {{ color: '#22c55e', value: stats.rescued_pct.toFixed(2) + '%', label: 'Rescued', bytes: stats.rescued_bytes }},
                {{ color: '#ef4444', value: stats.bad_pct.toFixed(3) + '%', label: 'Bad', bytes: stats.bad_bytes }},
                {{ color: '#6b7280', value: stats.untried_pct.toFixed(2) + '%', label: 'Untried', bytes: stats.untried_bytes }},
                {{ color: '#f59e0b', value: stats.non_scraped_pct.toFixed(3) + '%', label: 'Non-scraped', bytes: stats.non_scraped_bytes }},
            ];

            statsBar.innerHTML = items.map(item => `
                <div class="stat">
                    <div class="stat-dot" style="background: ${{item.color}};"></div>
                    <span class="stat-value">${{item.value}}</span>
                    <span class="stat-label">${{item.label}} (${{formatBytes(item.bytes)}})</span>
                </div>
            `).join('');
        }}
    }}

    // Render progress bar
    function renderProgressBar() {{
        const bar = document.getElementById('progress-bar');
        const segments = [
            {{ color: '#22c55e', pct: stats.rescued_pct }},
            {{ color: '#f59e0b', pct: stats.non_scraped_pct }},
            {{ color: '#a855f7', pct: stats.non_trimmed_pct }},
            {{ color: '#ef4444', pct: stats.bad_pct }},
            {{ color: '#6b7280', pct: stats.untried_pct }},
        ];

        bar.innerHTML = segments.map(seg =>
            `<div class="progress-segment" style="width: ${{seg.pct}}%; background: ${{seg.color}};"></div>`
        ).join('');
    }}

    // Render canvas
    function renderCanvas() {{
        const canvas = document.getElementById('blockmap');
        const ctx = canvas.getContext('2d');

        // Group by color for fewer state changes
        const byColor = {{}};
        blocks.forEach((status, i) => {{
            const color = statusColors[status] || '#333';
            if (!byColor[color]) byColor[color] = [];
            byColor[color].push(i);
        }});

        // Draw each color group
        for (const [color, indices] of Object.entries(byColor)) {{
            ctx.fillStyle = color;
            for (const i of indices) {{
                const x = (i % blocksPerRow) * pixelsPerBlock;
                const y = Math.floor(i / blocksPerRow) * pixelsPerBlock;
                ctx.fillRect(x, y, pixelsPerBlock, pixelsPerBlock);
            }}
        }}
    }}

    // Binary search to find files at a given byte offset
    function findFilesAtOffset(offset) {{
        if (!hasFileMapping || fileIntervals.length === 0) return [];

        // Convert byte offset to cluster number
        const cluster = Math.floor((offset - partitionOffset) / clusterSize);
        const results = [];

        // Binary search for potential matches
        let lo = 0, hi = fileIntervals.length - 1;
        while (lo <= hi) {{
            const mid = Math.floor((lo + hi) / 2);
            const [start, end, fileId] = fileIntervals[mid];
            if (cluster < start) {{
                hi = mid - 1;
            }} else if (cluster >= end) {{
                lo = mid + 1;
            }} else {{
                // Found a match, but check for others nearby
                results.push(fileId);
                // Check neighbors
                for (let i = mid - 1; i >= 0 && fileIntervals[i][1] > cluster; i--) {{
                    if (fileIntervals[i][0] <= cluster) results.push(fileIntervals[i][2]);
                }}
                for (let i = mid + 1; i < fileIntervals.length && fileIntervals[i][0] <= cluster; i++) {{
                    if (fileIntervals[i][1] > cluster) results.push(fileIntervals[i][2]);
                }}
                break;
            }}
        }}

        // Map to file info
        return [...new Set(results)].map(id => files[id]).filter(f => f);
    }}

    // Tooltip
    function setupTooltip() {{
        const canvas = document.getElementById('blockmap');
        const tooltip = document.getElementById('tooltip');

        canvas.addEventListener('mousemove', (e) => {{
            const rect = canvas.getBoundingClientRect();
            const x = e.clientX - rect.left;
            const y = e.clientY - rect.top;

            const blockX = Math.floor(x / pixelsPerBlock);
            const blockY = Math.floor(y / pixelsPerBlock);
            const blockIndex = blockY * blocksPerRow + blockX;

            if (blockIndex >= 0 && blockIndex < blocks.length) {{
                const status = blocks[blockIndex];
                const offset = blockIndex * blockSize;

                // Find files at this offset
                const filesHere = findFilesAtOffset(offset);

                let fileHtml = '';
                if (filesHere.length > 0) {{
                    const fileList = filesHere.slice(0, 3).map(f => {{
                        const name = f.path || f.name;
                        const shortName = name.length > 40 ? '...' + name.slice(-37) : name;
                        return `<div style="color: #60a5fa;">${{shortName}}</div>`;
                    }}).join('');
                    const moreCount = filesHere.length > 3 ? `<div style="color: #888;">+${{filesHere.length - 3}} more</div>` : '';
                    fileHtml = `
                        <div class="tooltip-row" style="flex-direction: column; align-items: flex-start; margin-top: 8px; border-top: 1px solid #333; padding-top: 8px;">
                            <span class="tooltip-label" style="margin-bottom: 4px;">Files:</span>
                            ${{fileList}}
                            ${{moreCount}}
                        </div>
                    `;
                }}

                tooltip.innerHTML = `
                    <div class="tooltip-row">
                        <span class="tooltip-label">Status:</span>
                        <span class="tooltip-value" style="color: ${{statusColors[status]}}">${{statusNames[status]}}</span>
                    </div>
                    <div class="tooltip-row">
                        <span class="tooltip-label">Offset:</span>
                        <span class="tooltip-value">${{formatHex(offset)}}</span>
                    </div>
                    <div class="tooltip-row">
                        <span class="tooltip-label">Block:</span>
                        <span class="tooltip-value">${{blockIndex.toLocaleString()}} / ${{blocks.length.toLocaleString()}}</span>
                    </div>
                    <div class="tooltip-row">
                        <span class="tooltip-label">Range:</span>
                        <span class="tooltip-value">${{formatBytes(offset)}} - ${{formatBytes(offset + blockSize)}}</span>
                    </div>
                    ${{fileHtml}}
                `;

                tooltip.style.display = 'block';
                tooltip.style.left = (e.clientX + 15) + 'px';
                tooltip.style.top = (e.clientY + 15) + 'px';
            }}
        }});

        canvas.addEventListener('mouseleave', () => {{
            tooltip.style.display = 'none';
        }});
    }}

    // Render metadata
    function renderMetadata() {{
        const div = document.getElementById('metadata');
        let html = '<strong>Log Info:</strong><br>';
        if (metadata.command) html += `Command: <code>${{metadata.command}}</code><br>`;
        if (metadata.current_time) html += `Generated: ${{metadata.current_time}}<br>`;
        html += `Block size: <code>${{formatBytes(blockSize)}}</code> | `;
        html += `Total blocks: <code>${{blocks.length.toLocaleString()}}</code> | `;
        html += `Drive size: <code>${{formatBytes(stats.total_bytes)}}</code>`;
        div.innerHTML = html;
    }}

    // ===== File Tree Functions (Phase 3) =====

    function renderFileStats() {{
        const div = document.getElementById('file-stats');
        if (!fileStats || !fileStats.total) {{
            div.innerHTML = '<span style="color:#888">No file data available</span>';
            return;
        }}
        div.innerHTML = `
            <div class="file-stat">
                <div class="file-stat-icon" style="background: #22c55e;"></div>
                <span>${{fileStats.recovered || 0}} recovered</span>
            </div>
            <div class="file-stat">
                <div class="file-stat-icon" style="background: #f59e0b;"></div>
                <span>${{fileStats.partial || 0}} partial</span>
            </div>
            <div class="file-stat">
                <div class="file-stat-icon" style="background: #ef4444;"></div>
                <span>${{fileStats.unrecovered || 0}} unrecovered</span>
            </div>
            <div class="file-stat" style="color: #888;">
                <span>${{fileStats.total || 0}} total files</span>
            </div>
        `;
    }}

    function matchesFilter(node) {{
        if (currentFilter === 'all') return true;
        if (node.type === 'dir') {{
            // Show dir if any child matches
            return Object.values(node.children || {{}}).some(c => matchesFilter(c));
        }}
        return node.status === currentFilter;
    }}

    function matchesSearch(node, query) {{
        if (!query) return true;
        const q = query.toLowerCase();
        if (node.name.toLowerCase().includes(q)) return true;
        if (node.type === 'dir') {{
            return Object.values(node.children || {{}}).some(c => matchesSearch(c, query));
        }}
        return false;
    }}

    // --- Lazy file-tree rendering: only build DOM for expanded folders. A full
    // eager render of a large tree (100k+ nodes) pegs the browser for minutes, so
    // each folder's children are built on demand when it is expanded. ---
    const _dirRegistry = [];
    function _dirId(node) {{
        if (node.__id === undefined) {{ node.__id = _dirRegistry.length; _dirRegistry.push(node); }}
        return node.__id;
    }}
    function _esc(s) {{
        return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
    }}
    function _sortedChildren(node) {{
        return Object.values(node.children || {{}}).sort((a, b) => {{
            if (a.type !== b.type) return a.type === 'dir' ? -1 : 1;
            return a.name.localeCompare(b.name);
        }});
    }}
    function buildFileRow(node, displayName) {{
        const statusColor = {{'recovered': '#22c55e', 'partial': '#f59e0b', 'unrecovered': '#ef4444'}}[node.status] || '#666';
        const pctClass = 'status-' + (node.status || 'unknown');
        const pctText = node.recovery_pct !== undefined ? node.recovery_pct.toFixed(0) + '%' : '?';
        const nm = _esc(displayName || node.name);
        return `<div class="tree-item tree-file" data-status="${{node.status}}">` +
               `<div class="tree-file-icon" style="background: ${{statusColor}};"></div>` +
               `<span class="tree-file-name" title="${{nm}}">${{nm}}</span>` +
               `<span class="tree-file-size">${{formatBytes(node.size || 0)}}</span>` +
               `<span class="tree-file-pct ${{pctClass}}">${{pctText}}</span></div>`;
    }}
    // Render DIRECT children only (not recursive). Folders get an empty children
    // container that is filled lazily on expand.
    function renderFolderChildren(node) {{
        let html = '';
        for (const c of _sortedChildren(node)) {{
            if (c.type === 'dir') {{
                const id = _dirId(c);
                html += `<div class="tree-item tree-folder" data-node-id="${{id}}">${{_esc(c.name)}}</div>` +
                        `<div class="tree-children" data-node-id="${{id}}"></div>`;
            }} else {{
                html += buildFileRow(c);
            }}
        }}
        return html;
    }}
    function _lazyLoad(folderEl) {{
        const childrenDiv = folderEl.nextElementSibling;
        if (childrenDiv && childrenDiv.classList.contains('tree-children') && childrenDiv.dataset.loaded !== '1') {{
            const node = _dirRegistry[parseInt(folderEl.dataset.nodeId)];
            childrenDiv.innerHTML = node ? renderFolderChildren(node) : '';
            childrenDiv.dataset.loaded = '1';
        }}
    }}
    // Search / non-"all" filter -> fast flat (capped) result list over the data.
    function renderFlatResults(container) {{
        const q = (searchQuery || '').toLowerCase();
        const CAP = 2000;
        const results = [];
        let truncated = false;
        (function walk(node, path) {{
            if (results.length >= CAP) {{ truncated = true; return; }}
            for (const c of Object.values(node.children || {{}})) {{
                if (results.length >= CAP) {{ truncated = true; return; }}
                const cpath = path ? path + '/' + c.name : c.name;
                if (c.type === 'dir') {{ walk(c, cpath); }}
                else {{
                    const okFilter = (currentFilter === 'all') || (c.status === currentFilter);
                    const okSearch = !q || (c.name && c.name.toLowerCase().includes(q));
                    if (okFilter && okSearch) results.push([c, cpath]);
                }}
            }}
        }})(fileTree, '');
        let html = '';
        if (!results.length) {{
            html = '<div style="color:#888; padding:20px;">No matching files.</div>';
        }} else {{
            if (truncated) html += `<div style="color:#888; padding:6px 10px;">Showing first ${{CAP}} matches - refine to narrow.</div>`;
            for (const [c, p] of results) html += buildFileRow(c, p);
        }}
        container.innerHTML = html;
    }}
    function renderFileTree() {{
        const container = document.getElementById('file-tree');
        if (!fileTree || !fileTree.children) {{
            container.innerHTML = '<div style="color:#888; padding: 20px;">No file data available.<br><br>Run visualizer with --job flag pointing to a recovery job directory.</div>';
            return;
        }}

        // Search or a non-"all" filter -> fast flat capped list (avoids rendering the tree)
        if ((searchQuery && searchQuery.length) || currentFilter !== 'all') {{
            renderFlatResults(container);
            return;
        }}

        // Default: lazy tree. Only the top level is built now; deeper levels are
        // rendered on demand when a folder is expanded.
        container.innerHTML = renderFolderChildren(fileTree);

        if (!container.dataset.bound) {{
            container.addEventListener('click', (e) => {{
                const folder = e.target.closest('.tree-folder');
                if (!folder || !container.contains(folder)) return;
                folder.classList.toggle('open');
                if (folder.classList.contains('open')) _lazyLoad(folder);
            }});
            container.dataset.bound = '1';
        }}

        // Auto-expand (and lazily load) the first level, but skip very large
        // folders (e.g. the orphan bucket) so the initial load stays snappy --
        // those expand on click instead.
        container.querySelectorAll(':scope > .tree-folder').forEach(folder => {{
            const node = _dirRegistry[parseInt(folder.dataset.nodeId)];
            const n = node && node.children ? Object.keys(node.children).length : 0;
            if (n <= 5000) {{
                folder.classList.add('open');
                _lazyLoad(folder);
            }}
        }});
    }}

    function setupFileTreeControls() {{
        // Filter buttons
        document.querySelectorAll('.filter-btn[data-filter]').forEach(btn => {{
            btn.addEventListener('click', () => {{
                document.querySelectorAll('.filter-btn[data-filter]').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                currentFilter = btn.dataset.filter;
                renderFileTree();
            }});
        }});

        // Search box
        const searchInput = document.getElementById('file-search');
        let searchTimeout;
        searchInput.addEventListener('input', () => {{
            clearTimeout(searchTimeout);
            searchTimeout = setTimeout(() => {{
                searchQuery = searchInput.value;
                renderFileTree();
            }}, 200);
        }});

        // Expand/Collapse all. Expand operates on currently-rendered folders and
        // lazy-loads them, so we never materialize the whole tree at once -- click
        // Expand All again to go another level deeper.
        document.getElementById('expand-all').addEventListener('click', () => {{
            document.querySelectorAll('#file-tree .tree-folder:not(.open)').forEach(f => {{
                f.classList.add('open');
                _lazyLoad(f);
            }});
        }});
        document.getElementById('collapse-all').addEventListener('click', () => {{
            document.querySelectorAll('#file-tree .tree-folder.open').forEach(f => f.classList.remove('open'));
        }});
    }}

    // Initialize
    renderStats();
    renderProgressBar();
    renderCanvas();
    setupTooltip();
    renderMetadata();
    renderFileStats();
    renderFileTree();
    setupFileTreeControls();
    </script>
</body>
</html>
'''

    with open(output_path, 'w') as f:
        f.write(html)

    print(f"Generated: {output_path}")
    print(f"  Blocks: {len(blocks):,} ({format_size(block_size)} each)")
    print(f"  Rescued: {stats['rescued_pct']:.2f}%")
    print(f"  Bad: {stats['bad_pct']:.4f}%")


def load_job_state(job_dir: Path) -> Optional[Dict]:
    """Load recovery_state.json from job directory."""
    state_file = job_dir / 'recovery_state.json'
    if state_file.exists():
        with open(state_file) as f:
            return json.load(f)
    return None


def build_recovered_intervals(regions: List[Region]) -> List[Tuple[int, int]]:
    """Build sorted list of (start, end) for recovered regions."""
    recovered = []
    for r in regions:
        if r.status == '+':
            recovered.append((r.start, r.end))
    recovered.sort()
    return recovered


def parse_domain_file(domain_path: Path) -> List[Tuple[int, int]]:
    """Parse ddrescue domain file into list of (start, end) byte ranges."""
    ranges = []
    with open(domain_path, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split()
            if len(parts) >= 2:
                try:
                    start = int(parts[0], 16)
                    size = int(parts[1], 16)
                    if size > 0:
                        ranges.append((start, start + size))
                except ValueError:
                    continue
    ranges.sort()
    return ranges


def calc_domain_recovery(domain_ranges: List[Tuple[int, int]],
                         recovered_intervals: List[Tuple[int, int]]) -> Tuple[int, int, float]:
    """
    Calculate how much of a domain has been recovered.

    Returns: (recovered_bytes, total_domain_bytes, recovery_pct)
    """
    total_domain = sum(end - start for start, end in domain_ranges)
    if total_domain == 0:
        return 0, 0, 0.0

    recovered_in_domain = 0
    rec_idx = 0

    for domain_start, domain_end in domain_ranges:
        # Find recovered regions that might overlap with this domain range
        while rec_idx < len(recovered_intervals) and recovered_intervals[rec_idx][1] <= domain_start:
            rec_idx += 1

        # Check all potentially overlapping recovered regions
        check_idx = rec_idx
        while check_idx < len(recovered_intervals):
            rec_start, rec_end = recovered_intervals[check_idx]
            if rec_start >= domain_end:
                break

            # Calculate overlap
            overlap_start = max(rec_start, domain_start)
            overlap_end = min(rec_end, domain_end)
            if overlap_end > overlap_start:
                recovered_in_domain += overlap_end - overlap_start

            check_idx += 1

    pct = (recovered_in_domain / total_domain) * 100 if total_domain > 0 else 0
    return recovered_in_domain, total_domain, pct


def calc_file_recovery(file_entries: Dict, file_intervals: List,
                       recovered_intervals: List[Tuple[int, int]],
                       cluster_size: int, partition_offset: int) -> Dict:
    """
    Calculate recovery status for each file.

    Returns dict of file_id -> {
        'recovered_bytes': int,
        'total_bytes': int,
        'recovery_pct': float,
        'status': 'recovered' | 'partial' | 'unrecovered'
    }
    """
    # Build binary-searchable list of recovered region starts
    rec_starts = [r[0] for r in recovered_intervals]

    file_status = {}

    for file_id, entry in file_entries.items():
        if entry.is_directory:
            continue

        total_bytes = 0
        recovered_bytes = 0

        # Get extents for this file (data_runs for NTFS, extents for HFS+)
        extents = getattr(entry, 'data_runs', None) or getattr(entry, 'extents', [])

        for start_cluster, cluster_count in extents:
            # Convert clusters to byte offsets
            extent_start = partition_offset + (start_cluster * cluster_size)
            extent_size = cluster_count * cluster_size
            extent_end = extent_start + extent_size
            total_bytes += extent_size

            # Check overlap with recovered regions using binary search
            idx = bisect_right(rec_starts, extent_start)
            if idx > 0:
                idx -= 1

            # Scan through potentially overlapping recovered regions
            while idx < len(recovered_intervals):
                rec_start, rec_end = recovered_intervals[idx]

                if rec_start >= extent_end:
                    break

                # Calculate overlap
                overlap_start = max(rec_start, extent_start)
                overlap_end = min(rec_end, extent_end)
                if overlap_end > overlap_start:
                    recovered_bytes += overlap_end - overlap_start

                idx += 1

        # Raw on-disk extent totals (before the actual-size cap below). The global
        # file-data recovery % must compare like units, so it uses these rather than
        # the actual-size-capped values.
        extent_total_raw = total_bytes
        extent_recovered_raw = recovered_bytes

        # Use actual file size if available and smaller than extent size
        actual_size = entry.size if entry.size > 0 else total_bytes
        if actual_size < total_bytes:
            # Scale recovered bytes proportionally
            if total_bytes > 0:
                recovered_bytes = int(recovered_bytes * actual_size / total_bytes)
            total_bytes = actual_size

        # Handle files with no extents (resident files stored in MFT, or empty)
        extents_list = getattr(entry, 'data_runs', None) or getattr(entry, 'extents', [])
        has_extents = bool(extents_list)
        if not extents_list:
            # Resident file (data stored in MFT record itself) or truly empty
            # If MFT was recovered, resident files are recovered
            # Cap resident file size to reasonable max (700 bytes) to avoid corrupt MFT data
            pct = 100.0
            status = 'recovered'
            resident_size = min(entry.size, 700) if entry.size > 0 else 0
            total_bytes = resident_size
            recovered_bytes = resident_size
        elif total_bytes > 0:
            pct = (recovered_bytes / total_bytes) * 100
            if pct >= 99.9:
                status = 'recovered'
            elif pct > 0:
                status = 'partial'
            else:
                status = 'unrecovered'
        else:
            pct = 100.0
            status = 'recovered'  # Empty files are "recovered"

        file_status[file_id] = {
            'recovered_bytes': recovered_bytes,
            'total_bytes': total_bytes,
            'recovery_pct': pct,
            'status': status,
            'has_extents': has_extents,
            'extent_total': extent_total_raw,
            'extent_recovered': extent_recovered_raw
        }

    return file_status


def build_file_tree(file_entries: Dict, file_status: Dict) -> Dict:
    """
    Build hierarchical file tree structure for JSON embedding.

    Returns nested dict structure suitable for rendering as collapsible tree.
    """
    # Root of the tree
    root = {'name': '/', 'type': 'dir', 'children': {}, 'stats': {'total': 0, 'recovered': 0, 'partial': 0, 'unrecovered': 0}}

    for file_id, entry in file_entries.items():
        path = entry.full_path or entry.name
        if not path:
            continue

        # Split path into components
        parts = [p for p in path.replace('\\', '/').split('/') if p]
        if not parts:
            continue

        # Navigate/create tree structure
        current = root
        for i, part in enumerate(parts):
            is_last = (i == len(parts) - 1)

            if part not in current['children']:
                if is_last and not entry.is_directory:
                    # File node
                    status_info = file_status.get(file_id, {})
                    node = {
                        'name': part,
                        'type': 'file',
                        'size': entry.size,
                        'file_id': file_id,
                        'recovery_pct': status_info.get('recovery_pct', 0),
                        'status': status_info.get('status', 'unknown'),
                        'recovered_bytes': status_info.get('recovered_bytes', 0),
                        'total_bytes': status_info.get('total_bytes', 0),
                    }
                    current['children'][part] = node

                    # Update parent stats
                    status = status_info.get('status', 'unknown')
                    if status in root['stats']:
                        root['stats'][status] += 1
                    root['stats']['total'] += 1
                else:
                    # Directory node
                    current['children'][part] = {
                        'name': part,
                        'type': 'dir',
                        'children': {},
                    }

            current = current['children'][part]

    return root


def detect_filesystem(job_state: Dict) -> str:
    """Detect filesystem type from job state."""
    # Check for NTFS indicators
    if 'mft_cluster' in job_state or 'mft_byte_offset' in job_state:
        return 'ntfs'
    # Check for HFS+ indicators
    if 'hfs_partition' in job_state or 'alloc_file_extents' in job_state:
        return 'hfsplus'
    # Check for block_size (could be either, but with allocation file it's HFS+)
    if 'block_size' in job_state and 'alloc_file_extracted' in job_state:
        return 'hfsplus'
    return 'unknown'


def load_file_index(job_dir: Path, fs_type: str, job_state: Dict) -> Tuple[Optional[Dict], Optional[List]]:
    """
    Load file entries and build cluster/block index.
    Returns (entries_dict, intervals_list) or (None, None) if no metadata available.
    """
    entries = None
    intervals = None

    if fs_type == 'ntfs':
        mft_path = job_dir / 'mft.raw'
        if mft_path.exists():
            try:
                from parse_ntfs import parse_mft, build_paths, build_cluster_index
                print(f"Parsing NTFS MFT: {mft_path}")
                entries = parse_mft(mft_path)
                print(f"  Found {len(entries):,} file entries")
                build_paths(entries)
                cluster_size = job_state.get('cluster_size', 4096)
                intervals = build_cluster_index(entries, cluster_size)
                print(f"  Built index with {len(intervals):,} extents")
            except ImportError:
                print("  Warning: parse_ntfs module not found")
            except Exception as e:
                print(f"  Warning: Failed to parse MFT: {e}")

    elif fs_type == 'hfsplus':
        catalog_path = job_dir / 'catalog.raw'
        if catalog_path.exists():
            try:
                from parse_hfs import parse_catalog, build_paths, build_block_index
                print(f"Parsing HFS+ Catalog: {catalog_path}")
                entries = parse_catalog(catalog_path)
                print(f"  Found {len(entries):,} file entries")
                build_paths(entries)
                intervals = build_block_index(entries)
                print(f"  Built index with {len(intervals):,} extents")
            except ImportError:
                print("  Warning: parse_hfs module not found")
            except Exception as e:
                print(f"  Warning: Failed to parse Catalog: {e}")

    return entries, intervals


def main():
    parser = argparse.ArgumentParser(description='Generate recovery visualization from ddrescue log')
    parser.add_argument('--log', '-l', help='Path to ddrescue log file')
    parser.add_argument('--job', '-j', help='Path to recovery job directory (with recovery_state.json)')
    parser.add_argument('--size', '-s', help='Drive size (e.g., 2TB, 500GB). Auto-detected if not specified.')
    parser.add_argument('--output', '-o', default='recovery_map.html', help='Output HTML file')
    parser.add_argument('--block-size', '-b', help='Display block size (e.g., 4MB). Auto-calculated if not specified.')

    args = parser.parse_args()

    # Determine log file path
    log_path = None
    job_dir = None
    job_state = None
    fs_type = 'unknown'
    file_entries = None
    file_intervals = None

    if args.job:
        job_dir = Path(args.job)
        if not job_dir.exists():
            print(f"Error: Job directory not found: {job_dir}", file=sys.stderr)
            sys.exit(1)

        # Load job state
        job_state = load_job_state(job_dir)
        if job_state:
            fs_type = detect_filesystem(job_state)
            print(f"Job directory: {job_dir}")
            print(f"  Filesystem: {fs_type.upper()}")

            # Try to find log file in job directory or from command line
            if args.log:
                log_path = Path(args.log)
            else:
                # Look for common log file names
                for name in ['recovery.log', 'ddrescue.log', '*.log']:
                    matches = list(job_dir.glob(name))
                    if matches:
                        log_path = matches[0]
                        break

            # Load file index if available
            file_entries, file_intervals = load_file_index(job_dir, fs_type, job_state)

    elif args.log:
        log_path = Path(args.log)
    else:
        print("Error: Must specify either --log or --job", file=sys.stderr)
        sys.exit(1)

    if not log_path or not log_path.exists():
        print(f"Error: Log file not found: {log_path}", file=sys.stderr)
        sys.exit(1)

    print(f"Parsing: {log_path}")
    regions, metadata = parse_ddrescue_log(log_path)
    print(f"  Found {len(regions):,} regions")

    if not regions:
        print("Error: No regions found in log file", file=sys.stderr)
        sys.exit(1)

    # Determine drive size
    if args.size:
        drive_size = parse_size(args.size)
    else:
        # Auto-detect from last region
        drive_size = max(r.end for r in regions)
        print(f"  Auto-detected drive size: {format_size(drive_size)}")

    # Determine block size
    if args.block_size:
        block_size = parse_size(args.block_size)
    else:
        block_size = calc_display_block_size(drive_size)

    print(f"  Display block size: {format_size(block_size)}")

    # Aggregate blocks
    print("Aggregating blocks...")
    blocks = aggregate_blocks(regions, drive_size, block_size)
    print(f"  Generated {len(blocks):,} display blocks")

    # Calculate stats
    stats = calc_stats(regions, drive_size)

    # Add file mapping info to metadata if available
    if file_entries:
        metadata['has_file_mapping'] = True
        metadata['file_count'] = len(file_entries)
        metadata['filesystem'] = fs_type
    else:
        metadata['has_file_mapping'] = False

    # Generate HTML
    output_path = Path(args.output)
    generate_html(blocks, stats, metadata, block_size, output_path,
                  file_entries=file_entries, file_intervals=file_intervals,
                  job_state=job_state, regions=regions, job_dir=job_dir)


if __name__ == '__main__':
    main()
