#!/usr/bin/env python3
"""
NTFS MFT Parser for Recovery Visualizer
Extracts file names, paths, and data runs (cluster locations) from MFT.
"""

import struct
from dataclasses import dataclass, field
from typing import List, Tuple, Dict, Optional, BinaryIO
from pathlib import Path


@dataclass
class FileEntry:
    """Represents a file or directory from MFT."""
    record_num: int
    parent_ref: int
    name: str
    is_directory: bool
    size: int
    data_runs: List[Tuple[int, int]] = field(default_factory=list)  # [(start_cluster, count), ...]
    full_path: str = ""


# MFT attribute types we care about
ATTR_STANDARD_INFO = 0x10
ATTR_FILE_NAME = 0x30
ATTR_DATA = 0x80
ATTR_INDEX_ROOT = 0x90
ATTR_INDEX_ALLOCATION = 0xA0


def parse_data_runs(data: bytes) -> List[Tuple[int, int]]:
    """
    Parse NTFS data runs from raw bytes.

    Data runs are variable-length encoded:
    - Header byte: low nibble = length bytes, high nibble = offset bytes
    - Length: N bytes (little-endian)
    - Offset: M bytes (little-endian, SIGNED, RELATIVE to previous)
    """
    runs = []
    pos = 0
    prev_lcn = 0

    while pos < len(data):
        header = data[pos]
        if header == 0:
            break

        len_bytes = header & 0x0F
        off_bytes = (header >> 4) & 0x0F
        pos += 1

        if len_bytes == 0:
            break

        # Parse length (unsigned)
        length = int.from_bytes(data[pos:pos + len_bytes], 'little')
        pos += len_bytes

        # Parse offset (signed, relative)
        if off_bytes > 0:
            offset_raw = data[pos:pos + off_bytes]
            offset = int.from_bytes(offset_raw, 'little', signed=False)
            # Handle sign extension
            if offset_raw[-1] & 0x80:
                offset -= (1 << (off_bytes * 8))
            pos += off_bytes

            prev_lcn += offset
            if prev_lcn >= 0 and length > 0:
                runs.append((prev_lcn, length))
        else:
            # Sparse run (no offset = sparse/resident)
            pass

    return runs


def parse_mft_record(data: bytes, record_num: int) -> Optional[FileEntry]:
    """
    Parse a single MFT record (typically 1024 bytes).
    Returns FileEntry or None if record is invalid/deleted.
    """
    # Check signature "FILE"
    if data[0:4] != b'FILE':
        return None

    # Get fixup array info
    fixup_offset = struct.unpack('<H', data[4:6])[0]
    fixup_count = struct.unpack('<H', data[6:8])[0]

    # Apply fixup (repair sector end markers)
    record_data = bytearray(data)
    if fixup_count > 0 and fixup_offset + fixup_count * 2 <= len(data):
        fixup_sig = record_data[fixup_offset:fixup_offset + 2]
        for i in range(1, fixup_count):
            sector_end = 512 * i - 2
            if sector_end + 2 <= len(record_data):
                # Replace marker with actual data
                record_data[sector_end:sector_end + 2] = record_data[fixup_offset + i * 2:fixup_offset + i * 2 + 2]

    # Check flags
    flags = struct.unpack('<H', record_data[22:24])[0]
    if not (flags & 0x01):  # Not in use
        return None

    is_directory = bool(flags & 0x02)

    # Get first attribute offset
    attr_offset = struct.unpack('<H', record_data[20:22])[0]

    entry = FileEntry(
        record_num=record_num,
        parent_ref=0,
        name="",
        is_directory=is_directory,
        size=0
    )

    # Parse attributes
    pos = attr_offset
    while pos + 4 <= len(record_data):
        attr_type = struct.unpack('<I', record_data[pos:pos + 4])[0]

        if attr_type == 0xFFFFFFFF:  # End marker
            break

        attr_len = struct.unpack('<I', record_data[pos + 4:pos + 8])[0]
        if attr_len == 0 or pos + attr_len > len(record_data):
            break

        # Non-resident flag
        non_resident = record_data[pos + 8]

        if attr_type == ATTR_FILE_NAME:
            # $FILE_NAME attribute
            if non_resident == 0:  # Resident
                content_offset = struct.unpack('<H', record_data[pos + 20:pos + 22])[0]
                content_start = pos + content_offset

                if content_start + 66 <= len(record_data):
                    # Parent directory reference (first 6 bytes of 8-byte ref)
                    parent_ref = struct.unpack('<Q', record_data[content_start:content_start + 8])[0]
                    entry.parent_ref = parent_ref & 0x0000FFFFFFFFFFFF  # Lower 48 bits

                    # File name
                    name_len = record_data[content_start + 64]
                    name_type = record_data[content_start + 65]  # 0=POSIX, 1=Win32, 2=DOS, 3=Win32+DOS

                    if name_type != 2:  # Skip DOS-only names
                        name_start = content_start + 66
                        name_bytes = record_data[name_start:name_start + name_len * 2]
                        try:
                            entry.name = name_bytes.decode('utf-16-le')
                        except:
                            entry.name = f"<record_{record_num}>"

        elif attr_type == ATTR_DATA:
            # $DATA attribute - get size and data runs
            if non_resident == 0:  # Resident
                content_len = struct.unpack('<I', record_data[pos + 16:pos + 20])[0]
                entry.size = content_len
            else:  # Non-resident
                # Real size
                if pos + 48 <= len(record_data):
                    entry.size = struct.unpack('<Q', record_data[pos + 48:pos + 56])[0]

                # Data runs offset
                runs_offset = struct.unpack('<H', record_data[pos + 32:pos + 34])[0]
                runs_start = pos + runs_offset

                if runs_start < len(record_data):
                    runs_data = bytes(record_data[runs_start:pos + attr_len])
                    entry.data_runs = parse_data_runs(runs_data)

        pos += attr_len

    return entry if entry.name else None


def parse_mft(mft_path: Path, max_records: int = 0) -> Dict[int, FileEntry]:
    """
    Parse MFT file and return dict of record_num -> FileEntry.

    Parses the ENTIRE MFT by default (max_records=0, bounded by end of file). A
    nonzero max_records caps how many records are read -- do NOT cap a real MFT:
    files whose parent directory lives in the unparsed tail can't resolve their
    parent and get orphaned to the root.
    """
    entries = {}
    record_size = 1024  # Standard MFT record size

    with open(mft_path, 'rb') as f:
        record_num = 0
        while max_records <= 0 or record_num < max_records:
            data = f.read(record_size)
            if len(data) < record_size:
                break

            entry = parse_mft_record(data, record_num)
            if entry:
                entries[record_num] = entry

            record_num += 1

            # Progress indicator
            if record_num % 50000 == 0:
                print(f"  Parsed {record_num:,} MFT records, {len(entries):,} valid entries...")

    return entries


def build_paths(entries: Dict[int, FileEntry]) -> None:
    """
    Build full paths by following parent references.
    Modifies entries in place, setting full_path.

    Files whose parent directory is missing (not recovered/parsed) can't be
    placed in the real tree, so they -- and their subtrees -- are bucketed under a
    synthetic '_other_found_files' folder instead of being scattered at the root.
    """
    ORPHAN_DIR = "_other_found_files"
    path_cache = {}

    def get_path(record_num: int, depth: int = 0):
        if depth > 100:  # Prevent infinite loops
            return "<circular>"

        if record_num in path_cache:
            return path_cache[record_num]

        if record_num not in entries:
            return None  # parent not present -> caller buckets this as an orphan

        entry = entries[record_num]

        # Root directory (record 5)
        if record_num == 5 or entry.parent_ref == record_num:
            path_cache[record_num] = ""
            return ""

        parent_path = get_path(entry.parent_ref, depth + 1)
        if parent_path is None:
            # Parent directory is missing -> orphan. Bucket it (and, via the cache,
            # its descendants) under the synthetic found-files folder.
            full_path = f"{ORPHAN_DIR}/{entry.name}"
        elif parent_path == "":
            full_path = entry.name  # parent is the volume root
        else:
            full_path = f"{parent_path}/{entry.name}"

        path_cache[record_num] = full_path
        return full_path

    for record_num in entries:
        entries[record_num].full_path = get_path(record_num) or ""


def build_cluster_index(entries: Dict[int, FileEntry], cluster_size: int) -> List[Tuple[int, int, int]]:
    """
    Build sorted list of (start_cluster, end_cluster, record_num) for binary search.
    """
    intervals = []

    for record_num, entry in entries.items():
        for start_cluster, count in entry.data_runs:
            intervals.append((start_cluster, start_cluster + count, record_num))

    # Sort by start cluster
    intervals.sort(key=lambda x: x[0])
    return intervals


def files_at_cluster(intervals: List[Tuple[int, int, int]], cluster: int,
                     entries: Dict[int, FileEntry]) -> List[FileEntry]:
    """
    Binary search to find files at a given cluster.
    """
    from bisect import bisect_right

    # Find potential matches
    starts = [i[0] for i in intervals]
    idx = bisect_right(starts, cluster) - 1

    results = []
    # Check intervals around this index
    for i in range(max(0, idx - 5), min(len(intervals), idx + 10)):
        start, end, record_num = intervals[i]
        if start <= cluster < end:
            if record_num in entries:
                results.append(entries[record_num])

    return results


if __name__ == '__main__':
    import sys

    if len(sys.argv) < 2:
        print("Usage: parse_ntfs.py <mft.raw>")
        sys.exit(1)

    mft_path = Path(sys.argv[1])
    print(f"Parsing MFT: {mft_path}")

    entries = parse_mft(mft_path)
    print(f"Found {len(entries):,} file entries")

    print("Building paths...")
    build_paths(entries)

    # Show some examples
    print("\nSample files:")
    count = 0
    for entry in entries.values():
        if entry.data_runs and not entry.is_directory:
            print(f"  {entry.full_path} ({entry.size:,} bytes, {len(entry.data_runs)} extents)")
            count += 1
            if count >= 10:
                break
