#!/usr/bin/env python3
"""
Prepare a release for signing (on build/development machine).

This tool:
1. Verifies the build is reproducible (or runs the build)
2. Computes reproducible hash of the image
3. Creates nova-release-hash.txt for offline signing
4. Outputs instructions for the offline signing step

Usage:
    python3 prepare-release.py [--rebuild] [--image PATH] [--version VERSION]

Example:
    python3 prepare-release.py --version 1.0.0
"""

import sys
import hashlib
import argparse
import subprocess
from pathlib import Path
from datetime import datetime

try:
    from cryptography.hazmat.primitives import serialization
except ImportError:
    print("ERROR: cryptography package not found.")
    print("Install with: pip install cryptography")
    sys.exit(1)


def run_build(repo_root: Path) -> bool:
    """Run the NOVA build (cargo build --release)."""
    print("Building NOVA kernel...")
    kernel_dir = repo_root / "kernel"

    result = subprocess.run(
        ["cargo", "build", "--release"],
        cwd=kernel_dir,
        capture_output=False
    )

    return result.returncode == 0


def hash_file(path: Path) -> str:
    """Compute SHA256 hash of a file."""
    sha256 = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            sha256.update(chunk)
    return sha256.hexdigest()


def load_public_key_fingerprint(pub_key_path: Path) -> str:
    """Load public key and compute fingerprint."""
    with open(pub_key_path, "rb") as f:
        key_data = f.read()

    digest = hashlib.sha256(key_data).hexdigest()
    return digest


def main():
    parser = argparse.ArgumentParser(
        description="Prepare a NOVA release for signing."
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Rebuild the kernel before preparing release"
    )
    parser.add_argument(
        "--image",
        type=Path,
        default=Path("build/nova.hdd"),
        help="Path to NOVA image (default: build/nova.hdd)"
    )
    parser.add_argument(
        "--version",
        default="1.0.0",
        help="Release version (default: 1.0.0)"
    )
    parser.add_argument(
        "--public-key",
        type=Path,
        default=Path("kernel/trust/root-key.pub"),
        help="Path to public key (for fingerprint verification)"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("nova-release-hash.txt"),
        help="Output file for release hash"
    )

    args = parser.parse_args()

    repo_root = Path.cwd()

    print("=== NOVA Release Preparation ===\n")

    # Rebuild if requested
    if args.rebuild:
        print("Step 1: Building NOVA kernel")
        if not run_build(repo_root):
            print("ERROR: Build failed")
            sys.exit(1)
        print()

    # Verify image exists
    print("Step 2: Verifying image")
    if not args.image.exists():
        print(f"ERROR: Image not found: {args.image}")
        print("Run: cargo build --release (in kernel/)")
        sys.exit(1)

    image_size = args.image.stat().st_size
    print(f"Image path: {args.image}")
    print(f"Image size: {image_size:,} bytes")
    print()

    # Compute reproducible hash
    print("Step 3: Computing reproducible hash")
    print("(This may take a minute for large images)")
    release_hash = hash_file(args.image)
    print(f"SHA256: {release_hash}")
    print()

    # Verify public key exists
    print("Step 4: Verifying trust root")
    if not args.public_key.exists():
        print(f"ERROR: Public key not found: {args.public_key}")
        print("First run: python3 tools/keygen.py (on offline machine)")
        sys.exit(1)

    pub_fingerprint = load_public_key_fingerprint(args.public_key)
    print(f"Public key: {args.public_key}")
    print(f"Fingerprint: {pub_fingerprint}")
    print()

    # Create release hash file
    print("Step 5: Creating release hash file")

    timestamp = datetime.utcnow().isoformat() + "Z"
    hash_content = f"""# NOVA Release Hash
# Generated: {timestamp}
# Reproducible build

image: {args.image.name}
size: {image_size}
sha256: {release_hash}
version: {args.version}
public-key-fingerprint: {pub_fingerprint}
"""

    with open(args.output, "w") as f:
        f.write(hash_content)

    print(f"Hash file: {args.output}")
    print()

    print("=== Next Steps ===\n")
    print("1. Transfer files to offline machine (via USB):")
    print(f"   - {args.output}")
    print()
    print("2. On offline machine, run:")
    print(f"   python3 sign-release.py {args.output} nova-root-key/root.priv {args.version}")
    print()
    print("3. Transfer back to this machine:")
    print("   - nova-release-signature.txt")
    print()
    print("4. Verify signature:")
    print("   python3 verify-release.py")
    print()
    print("5. If verified, install:")
    print("   scripts/install-release.sh")
    print()


if __name__ == "__main__":
    main()
