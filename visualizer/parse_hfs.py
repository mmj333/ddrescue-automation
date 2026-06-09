#!/usr/bin/env python3
"""
HFS+ Catalog Parser for Recovery Visualizer
Extracts file names, paths, and extents from HFS+ Catalog B-tree.
"""

import struct
from dataclasses import dataclass, field
from typing import List, Tuple, Dict, Optional
from pathlib import Path


@dataclass
class HFSFileEntry:
    """Represents a file or directory from HFS+ Catalog."""
    cnid: int  # Catalog Node ID
    parent_cnid: int
    name: str
    is_directory: bool
    size: int
    extents: List[Tuple[int, int]] = field(default_factory=list)  # [(start_block, count), ...]
    full_path: str = ""


# HFS+ record types
FOLDER_RECORD = 0x0001
FILE_RECORD = 0x0002
FOLDER_THREAD = 0x0003
FILE_THREAD = 0x0004


def parse_hfs_extent(data: bytes, offset: int) -> Tuple[int, int]:
    """Parse a single HFS+ extent (startBlock, blockCount) - big-endian."""
    start_block = struct.unpack('>I', data[offset:offset + 4])[0]
    block_count = struct.unpack('>I', data[offset + 4:offset + 8])[0]
    return (start_block, block_count)


def parse_fork_data(data: bytes) -> Tuple[int, List[Tuple[int, int]]]:
    """
    Parse HFSPlusForkData structure (80 bytes).
    Returns (logical_size, list of extents).
    """
    logical_size = struct.unpack('>Q', data[0:8])[0]

    extents = []
    for i in range(8):
        start, count = parse_hfs_extent(data, 16 + i * 8)
        if count == 0:
            break
        extents.append((start, count))

    return logical_size, extents


def parse_catalog_key(data: bytes, offset: int) -> Tuple[int, int, str]:
    """
    Parse HFSPlusCatalogKey.
    Returns (key_length, parent_cnid, name).
    """
    key_length = struct.unpack('>H', data[offset:offset + 2])[0]
    parent_cnid = struct.unpack('>I', data[offset + 2:offset + 6])[0]

    # Unicode name
    name_length = struct.unpack('>H', data[offset + 6:offset + 8])[0]
    name_data = data[offset + 8:offset + 8 + name_length * 2]

    try:
        # HFS+ uses big-endian UTF-16
        name = name_data.decode('utf-16-be')
    except:
        name = f"<cnid_{parent_cnid}>"

    return key_length, parent_cnid, name


def parse_catalog_record(data: bytes, offset: int, name: str, parent_cnid: int) -> Optional[HFSFileEntry]:
    """
    Parse a catalog record (folder or file).
    """
    record_type = struct.unpack('>H', data[offset:offset + 2])[0]

    if record_type == FOLDER_RECORD:
        # Folder record
        cnid = struct.unpack('>I', data[offset + 8:offset + 12])[0]
        return HFSFileEntry(
            cnid=cnid,
            parent_cnid=parent_cnid,
            name=name,
            is_directory=True,
            size=0
        )

    elif record_type == FILE_RECORD:
        # File record
        cnid = struct.unpack('>I', data[offset + 8:offset + 12])[0]

        # Data fork starts at offset 88 in file record
        data_fork_offset = offset + 88
        size, extents = parse_fork_data(data[data_fork_offset:data_fork_offset + 80])

        return HFSFileEntry(
            cnid=cnid,
            parent_cnid=parent_cnid,
            name=name,
            is_directory=False,
            size=size,
            extents=extents
        )

    return None


def parse_btree_node(data: bytes, node_offset: int, node_size: int) -> List[Tuple[int, bytes, bytes]]:
    """
    Parse a B-tree node and extract records.
    Returns list of (record_offset, key_data, record_data).
    """
    node_data = data[node_offset:node_offset + node_size]

    # Node descriptor (14 bytes)
    # fLink, bLink, kind, height, numRecords, reserved
    kind = node_data[8]  # -1=leaf, 0=index, 1=header, 2=map
    num_records = struct.unpack('>H', node_data[10:12])[0]

    if kind != 0xFF:  # Not a leaf node (we want leaf nodes for actual records)
        return []

    records = []

    # Record offsets are at the end of the node, in reverse order
    for i in range(num_records):
        offset_pos = node_size - 2 - (i * 2)
        record_offset = struct.unpack('>H', node_data[offset_pos:offset_pos + 2])[0]

        # Next record offset
        next_offset_pos = node_size - 2 - ((i + 1) * 2)
        next_record_offset = struct.unpack('>H', node_data[next_offset_pos:next_offset_pos + 2])[0]

        record_data = node_data[record_offset:next_record_offset]
        records.append((record_offset, record_data))

    return records


def parse_catalog(catalog_path: Path, max_nodes: int = 100000) -> Dict[int, HFSFileEntry]:
    """
    Parse HFS+ Catalog B-tree file.
    Returns dict of CNID -> HFSFileEntry.
    """
    entries = {}

    with open(catalog_path, 'rb') as f:
        catalog_data = f.read()

    if len(catalog_data) < 512:
        print("Catalog file too small")
        return entries

    # Parse header node (first node)
    # BTHeaderRec starts at offset 14 in header node
    header_offset = 14
    tree_depth = struct.unpack('>H', catalog_data[header_offset:header_offset + 2])[0]
    root_node = struct.unpack('>I', catalog_data[header_offset + 2:header_offset + 6])[0]
    leaf_records = struct.unpack('>I', catalog_data[header_offset + 6:header_offset + 10])[0]
    first_leaf = struct.unpack('>I', catalog_data[header_offset + 10:header_offset + 14])[0]
    last_leaf = struct.unpack('>I', catalog_data[header_offset + 14:header_offset + 18])[0]
    node_size = struct.unpack('>H', catalog_data[header_offset + 18:header_offset + 20])[0]

    print(f"  Catalog B-tree: depth={tree_depth}, node_size={node_size}, leaf_records={leaf_records:,}")

    if node_size == 0 or node_size > 32768:
        print(f"  Invalid node size: {node_size}")
        return entries

    total_nodes = len(catalog_data) // node_size

    # Traverse leaf nodes
    nodes_parsed = 0
    for node_num in range(total_nodes):
        if nodes_parsed >= max_nodes:
            break

        node_offset = node_num * node_size
        if node_offset + node_size > len(catalog_data):
            break

        node_data = catalog_data[node_offset:node_offset + node_size]

        # Check if leaf node (kind = -1 = 0xFF)
        kind = node_data[8]
        if kind != 0xFF:
            continue

        num_records = struct.unpack('>H', node_data[10:12])[0]

        # Parse records in this leaf node
        for i in range(num_records):
            try:
                # Get record offset from end of node
                offset_pos = node_size - 2 - (i * 2)
                record_offset = struct.unpack('>H', node_data[offset_pos:offset_pos + 2])[0]

                # Parse key
                key_length, parent_cnid, name = parse_catalog_key(node_data, record_offset)

                # Record data follows key (aligned to 2 bytes)
                key_end = record_offset + 2 + key_length
                if key_end % 2:
                    key_end += 1

                # Parse record
                entry = parse_catalog_record(node_data, key_end, name, parent_cnid)
                if entry:
                    entries[entry.cnid] = entry

            except Exception as e:
                continue

        nodes_parsed += 1

        if nodes_parsed % 1000 == 0:
            print(f"  Parsed {nodes_parsed:,} nodes, {len(entries):,} entries...")

    return entries


def build_paths(entries: Dict[int, HFSFileEntry]) -> None:
    """
    Build full paths by following parent CNIDs.
    Root folder has CNID 2.
    """
    path_cache = {}

    def get_path(cnid: int, depth: int = 0) -> str:
        if depth > 100:
            return "<circular>"

        if cnid in path_cache:
            return path_cache[cnid]

        if cnid == 2:  # Root folder
            path_cache[cnid] = ""
            return ""

        if cnid not in entries:
            return ""

        entry = entries[cnid]

        parent_path = get_path(entry.parent_cnid, depth + 1)
        if parent_path:
            full_path = f"{parent_path}/{entry.name}"
        else:
            full_path = entry.name

        path_cache[cnid] = full_path
        return full_path

    for cnid in entries:
        entries[cnid].full_path = get_path(cnid)


def build_block_index(entries: Dict[int, HFSFileEntry]) -> List[Tuple[int, int, int]]:
    """
    Build sorted list of (start_block, end_block, cnid) for binary search.
    """
    intervals = []

    for cnid, entry in entries.items():
        for start_block, count in entry.extents:
            if count > 0:
                intervals.append((start_block, start_block + count, cnid))

    intervals.sort(key=lambda x: x[0])
    return intervals


if __name__ == '__main__':
    import sys

    if len(sys.argv) < 2:
        print("Usage: parse_hfs.py <catalog.raw>")
        sys.exit(1)

    catalog_path = Path(sys.argv[1])
    print(f"Parsing HFS+ Catalog: {catalog_path}")

    entries = parse_catalog(catalog_path)
    print(f"Found {len(entries):,} entries")

    print("Building paths...")
    build_paths(entries)

    # Show some examples
    print("\nSample files:")
    count = 0
    for entry in entries.values():
        if entry.extents and not entry.is_directory:
            print(f"  {entry.full_path} ({entry.size:,} bytes, {len(entry.extents)} extents)")
            count += 1
            if count >= 10:
                break
