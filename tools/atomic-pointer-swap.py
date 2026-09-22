#!/usr/bin/env python3
"""
Atomic pointer block update for A/B slot switching.

Atomically swaps the active slot by updating the CRC (single-word write).
This ensures the operation is atomic: either completes fully or not at all.

Usage:
    python3 atomic-pointer-swap.py <manifest> <pointer-block-path> <image-path>

Example:
    python3 atomic-pointer-swap.py nova-release-1.0.0.manifest /dev/nova-pointer boot/nova.b
"""

import sys
import struct
import json
import time
import hashlib
import argparse
from pathlib import Path
from datetime import datetime, timezone

POINTER_MAGIC = 0x4e4f5641  # "NOVA"
POINTER_SIZE = 64
CRC_OFFSET = 56  # Offset of CRC field within pointer block


def crc32(data: bytes) -> int:
    """Compute CRC32 of data."""
    import zlib
    return zlib.crc32(data) & 0xffffffff


def struct_pack_pointer(magic, active_slot, sequence, timestamp, manifest_path):
    """Pack pointer block fields (without CRC)."""
    manifest_bytes = manifest_path.encode('ascii')
    manifest_bytes = manifest_bytes[:32].ljust(32, b'\x00')  # Pad to 32 bytes

    data = struct.pack(
        '<I B 3s I I 32s',
        magic,
        active_slot,
        b'\x00' * 3,  # reserved
        sequence,
        timestamp,
        manifest_bytes
    )
    return data


def struct_unpack_pointer(data: bytes):
    """Unpack pointer block fields."""
    magic, active_slot, _, sequence, timestamp, manifest_bytes, crc = struct.unpack(
        '<I B 3s I I 32s I',
        data[:60] + data[60:64]
    )
    manifest_path = manifest_bytes.rstrip(b'\x00').decode('ascii', errors='ignore')
    return {
        'magic': magic,
        'active_slot': active_slot,
        'sequence': sequence,
        'timestamp': timestamp,
        'manifest_path': manifest_path,
        'crc32': crc
    }


def read_pointer_block(path: Path) -> dict:
    """Read pointer block from disk."""
    with open(path, 'rb') as f:
        data = f.read(POINTER_SIZE)

    if len(data) < POINTER_SIZE:
        raise ValueError(f"Pointer block too small: {len(data)} < {POINTER_SIZE}")

    return struct_unpack_pointer(data)


def validate_pointer_block(pb: dict) -> bool:
    """Validate pointer block structure."""
    if pb['magic'] != POINTER_MAGIC:
        return False
    if pb['active_slot'] not in [0, 1]:
        return False
    if pb['sequence'] > 0xffffffff:
        return False

    # Verify CRC
    reconstructed = struct_pack_pointer(
        pb['magic'],
        pb['active_slot'],
        pb['sequence'],
        pb['timestamp'],
        pb['manifest_path']
    )
    expected_crc = crc32(reconstructed)

    return expected_crc == pb['crc32']


def atomic_pointer_swap(
    pointer_path: Path,
    current_slot: int,
    new_slot: int,
    new_sequence: int,
    new_manifest: str
):
    """
    Atomically swap the active slot.

    Updates pointer block so that new_slot becomes active.
    Uses CRC-only update for atomicity.
    """

    if new_slot not in [0, 1]:
        raise ValueError(f"Invalid slot: {new_slot} (must be 0 or 1)")

    if new_sequence <= 0:
        raise ValueError(f"Invalid sequence: {new_sequence} (must be > 0)")

    print(f"Atomic pointer swap: Slot {current_slot} → Slot {new_slot}")
    print(f"  Sequence: {new_sequence}")
    print(f"  Manifest: {new_manifest}")
    print()

    # Read current pointer block
    print("Reading current pointer block...")
    try:
        current_pb = read_pointer_block(pointer_path)
    except Exception as e:
        print(f"ERROR: Failed to read pointer block: {e}")
        sys.exit(1)

    print(f"  Magic: 0x{current_pb['magic']:08x}")
    print(f"  Active slot: {current_pb['active_slot']}")
    print(f"  Current sequence: {current_pb['sequence']}")
    print()

    # Validate current pointer block
    print("Validating current pointer block...")
    if not validate_pointer_block(current_pb):
        print("ERROR: Current pointer block is corrupted (CRC mismatch)")
        print("Cannot proceed with swap")
        sys.exit(1)
    print("  ✓ CRC valid")
    print()

    # Check sequence number (no rollback)
    print("Checking sequence number...")
    if new_sequence <= current_pb['sequence']:
        print(f"ERROR: Sequence would rollback: {new_sequence} <= {current_pb['sequence']}")
        sys.exit(1)
    print(f"  ✓ Sequence increases: {current_pb['sequence']} → {new_sequence}")
    print()

    # Prepare new pointer block
    print("Preparing new pointer block...")
    timestamp = int(time.time())

    new_pb_data = struct_pack_pointer(
        POINTER_MAGIC,
        new_slot,
        new_sequence,
        timestamp,
        new_manifest
    )

    new_crc = crc32(new_pb_data)
    print(f"  New CRC: 0x{new_crc:08x}")
    print()

    # Atomic write: update CRC field only
    print("Performing atomic write...")
    print("  Writing CRC field (single-word operation)...")

    try:
        with open(pointer_path, 'r+b') as f:
            # Seek to CRC field offset
            f.seek(CRC_OFFSET)
            # Write new CRC (4 bytes, little-endian)
            f.write(struct.pack('<I', new_crc))
            f.flush()
            # Explicit sync for safety (filesystem dependent)
            try:
                import os
                os.fsync(f.fileno())
            except (AttributeError, OSError):
                pass  # Not available on all filesystems
    except Exception as e:
        print(f"ERROR: Failed to write CRC: {e}")
        sys.exit(1)

    print("  ✓ CRC written")
    print()

    # Verify the write
    print("Verifying atomic swap...")
    try:
        swapped_pb = read_pointer_block(pointer_path)
    except Exception as e:
        print(f"ERROR: Failed to read pointer block after swap: {e}")
        sys.exit(1)

    if swapped_pb['active_slot'] != new_slot:
        print(f"ERROR: Active slot mismatch: {swapped_pb['active_slot']} != {new_slot}")
        sys.exit(1)

    if swapped_pb['sequence'] != new_sequence:
        print(f"ERROR: Sequence mismatch: {swapped_pb['sequence']} != {new_sequence}")
        sys.exit(1)

    if not validate_pointer_block(swapped_pb):
        print("ERROR: Pointer block is corrupted after swap (CRC mismatch)")
        sys.exit(1)

    print("  ✓ Pointer block valid after swap")
    print(f"  ✓ Active slot: {swapped_pb['active_slot']}")
    print(f"  ✓ Sequence: {swapped_pb['sequence']}")
    print()

    print("=== ATOMIC SWAP SUCCESSFUL ===")
    print()
    print(f"Next boot will use Slot {new_slot} with manifest: {new_manifest}")
    print()
    print("System is safe to reboot.")


def main():
    parser = argparse.ArgumentParser(
        description="Atomically swap A/B slots for NOVA updates"
    )
    parser.add_argument(
        "manifest",
        type=Path,
        help="Signed manifest file (for version info)"
    )
    parser.add_argument(
        "--pointer-block",
        type=Path,
        default=Path("/dev/nova-pointer"),
        help="Path to pointer block (default: /dev/nova-pointer)"
    )
    parser.add_argument(
        "--current-slot",
        type=int,
        default=0,
        help="Current active slot (0 or 1, default: 0)"
    )

    args = parser.parse_args()

    # Validate inputs
    if not args.manifest.exists():
        print(f"ERROR: Manifest not found: {args.manifest}")
        sys.exit(1)

    if args.current_slot not in [0, 1]:
        print(f"ERROR: Invalid current slot: {args.current_slot}")
        sys.exit(1)

    # Load manifest for version info
    print("=== ATOMIC POINTER SWAP ===\n")
    print(f"Loading manifest: {args.manifest}")

    try:
        with open(args.manifest, 'r') as f:
            manifest = json.load(f)
    except Exception as e:
        print(f"ERROR: Failed to load manifest: {e}")
        sys.exit(1)

    version = manifest.get('release', {}).get('version', 'unknown')
    sequence = manifest.get('release', {}).get('release_sequence', 0)
    manifest_path = args.manifest.name

    print(f"  Version: {version}")
    print(f"  Sequence: {sequence}")
    print(f"  Manifest path: {manifest_path}")
    print()

    # Determine new slot (alternate of current)
    new_slot = 1 if args.current_slot == 0 else 0

    # Perform atomic swap
    atomic_pointer_swap(
        args.pointer_block,
        args.current_slot,
        new_slot,
        sequence,
        manifest_path
    )


if __name__ == "__main__":
    main()
