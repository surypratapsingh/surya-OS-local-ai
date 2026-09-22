#!/usr/bin/env python3
"""
Create a NOVA release manifest (on build machine).

A manifest lists all packages in a release with their hashes, versions,
and capabilities.

Usage:
    python3 create-manifest.py --version 1.0.0 --package kernel:1.0.0:path/to/kernel.bin

Example with multiple packages:
    python3 create-manifest.py \\
        --version 1.0.0 \\
        --description "Stable with mathd verifier" \\
        --sequence 42 \\
        --package kernel:1.0.0:build/nova-kernel.bin \\
        --package mathd:1.0.0:build/mathd.bin \\
        --capabilities kernel:boot:memory,boot:cpu,boot:filesystem \\
        --capabilities mathd:compute:expression,compute:derivative

Output: nova-release-<version>.manifest (unsigned JSON)

Next steps:
    1. Transfer to offline machine
    2. Sign with: python3 sign-manifest.py nova-release-1.0.0.manifest
    3. Transfer back and verify
"""

import sys
import json
import hashlib
import argparse
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List, Optional


def compute_sha256(path: Path) -> str:
    """Compute SHA256 hash of a file."""
    sha256 = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            sha256.update(chunk)
    return sha256.hexdigest()


def parse_package_spec(spec: str) -> tuple:
    """Parse package spec: id:version:path"""
    parts = spec.split(":", 2)
    if len(parts) != 3:
        raise ValueError(f"Invalid package spec: {spec}. Expected id:version:path")
    return parts[0], parts[1], Path(parts[2])


def parse_capabilities_spec(spec: str) -> tuple:
    """Parse capabilities spec: package_id:cap1,cap2,cap3"""
    parts = spec.split(":", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid capabilities spec: {spec}. Expected package_id:cap1,cap2")
    package_id = parts[0]
    capabilities = [cap.strip() for cap in parts[1].split(",") if cap.strip()]
    return package_id, capabilities


def get_public_key_fingerprint(pub_key_path: Path) -> str:
    """Load public key and compute SHA256 fingerprint."""
    with open(pub_key_path, "rb") as f:
        key_data = f.read()
    digest = hashlib.sha256(key_data).hexdigest()
    return f"sha256:{digest}"


def create_manifest(
    version: str,
    packages: List[tuple],
    capabilities_map: Dict[str, List[str]],
    release_sequence: int,
    description: Optional[str],
    public_key_path: Path,
) -> dict:
    """Create a manifest dictionary."""

    timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    manifest_packages = []
    for seq_num, (pkg_id, pkg_version, pkg_path) in enumerate(packages, start=1):
        if not pkg_path.exists():
            raise FileNotFoundError(f"Package file not found: {pkg_path}")

        sha256 = compute_sha256(pkg_path)
        size = pkg_path.stat().st_size

        pkg_capabilities = capabilities_map.get(pkg_id, [])

        manifest_packages.append({
            "id": pkg_id,
            "version": pkg_version,
            "filename": pkg_path.name,
            "sha256": sha256,
            "size": size,
            "sequence": seq_num,
            "capabilities": pkg_capabilities
        })

    fingerprint = get_public_key_fingerprint(public_key_path)

    manifest = {
        "manifest_version": "1.0",
        "release": {
            "version": version,
            "timestamp": timestamp,
            "release_sequence": release_sequence,
        },
        "trust": {
            "algorithm": "Ed25519",
            "public_key_fingerprint": fingerprint,
        },
        "packages": manifest_packages,
        "signature": ""  # Empty until signed
    }

    if description:
        manifest["release"]["description"] = description

    return manifest


def canonicalize_json(obj: dict) -> bytes:
    """Convert to canonical JSON (sorted keys, minimal whitespace)."""
    return json.dumps(obj, sort_keys=True, separators=(',', ':')).encode()


def main():
    parser = argparse.ArgumentParser(
        description="Create a NOVA release manifest"
    )
    parser.add_argument(
        "--version",
        required=True,
        help="Release version (X.Y.Z)"
    )
    parser.add_argument(
        "--sequence",
        type=int,
        default=1,
        help="Release sequence number (default: 1)"
    )
    parser.add_argument(
        "--description",
        help="Release description (optional)"
    )
    parser.add_argument(
        "--package",
        action="append",
        dest="packages",
        help="Package spec: id:version:path (repeatable)"
    )
    parser.add_argument(
        "--capabilities",
        action="append",
        dest="capabilities",
        help="Capabilities spec: package_id:cap1,cap2 (repeatable)"
    )
    parser.add_argument(
        "--public-key",
        type=Path,
        default=Path("kernel/trust/root-key.pub"),
        help="Path to public key"
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output manifest file (default: nova-release-<version>.manifest)"
    )

    args = parser.parse_args()

    # Validate inputs
    if not args.packages:
        print("ERROR: At least one package required (--package)")
        sys.exit(1)

    if not args.public_key.exists():
        print(f"ERROR: Public key not found: {args.public_key}")
        sys.exit(1)

    print("=== NOVA Manifest Creation ===\n")
    print(f"Version: {args.version}")
    print(f"Sequence: {args.sequence}")
    print(f"Public key: {args.public_key}")
    print()

    # Parse packages
    print("Parsing packages...")
    packages = []
    for spec in args.packages:
        pkg_id, pkg_version, pkg_path = parse_package_spec(spec)
        if not pkg_path.exists():
            print(f"ERROR: Package file not found: {pkg_path}")
            sys.exit(1)
        packages.append((pkg_id, pkg_version, pkg_path))
        print(f"  ✓ {pkg_id}:{pkg_version} ({pkg_path.stat().st_size:,} bytes)")

    print()

    # Parse capabilities
    capabilities_map = {}
    if args.capabilities:
        print("Parsing capabilities...")
        for spec in args.capabilities:
            pkg_id, caps = parse_capabilities_spec(spec)
            capabilities_map[pkg_id] = caps
            print(f"  ✓ {pkg_id}: {', '.join(caps)}")
        print()

    # Create manifest
    print("Creating manifest...")
    try:
        manifest = create_manifest(
            args.version,
            packages,
            capabilities_map,
            args.sequence,
            args.description,
            args.public_key
        )
    except Exception as e:
        print(f"ERROR: Failed to create manifest: {e}")
        sys.exit(1)

    # Determine output path
    if args.output:
        output_path = args.output
    else:
        output_path = Path(f"nova-release-{args.version}.manifest")

    # Write manifest
    print(f"Writing to {output_path}...")
    with open(output_path, "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)

    print("\n=== Success ===")
    print(f"Manifest: {output_path}")
    print()

    # Compute canonical hash (for reference)
    canonical = canonicalize_json(manifest)
    print("Canonical SHA256 (unsigned):")
    print(hashlib.sha256(canonical).hexdigest())
    print()

    print("Next steps:")
    print("1. Transfer manifest to offline machine (via USB)")
    print(f"2. Sign with: python3 sign-manifest.py {output_path.name} nova-root-key/root.priv")
    print("3. Transfer back and verify: python3 verify-manifest.py")


if __name__ == "__main__":
    main()
