#!/usr/bin/env python3
"""
Sign a NOVA manifest with the root key (offline machine only).

Usage:
    python3 sign-manifest.py <manifest.json> <private-key>

Example (on offline machine):
    python3 sign-manifest.py nova-release-1.0.0.manifest nova-root-key/root.priv

Output:
    Updates manifest.json with signature field
"""

import sys
import json
import base64
import argparse
from pathlib import Path

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519
except ImportError:
    print("ERROR: cryptography package not found")
    print("Install with: pip install cryptography")
    sys.exit(1)


def load_private_key(key_path: Path) -> ed25519.Ed25519PrivateKey:
    """Load Ed25519 private key from PEM file."""
    with open(key_path, "rb") as f:
        pem_data = f.read()

    key = serialization.load_pem_private_key(pem_data, password=None)
    if not isinstance(key, ed25519.Ed25519PrivateKey):
        raise ValueError(f"Expected Ed25519 private key, got {type(key).__name__}")

    return key


def canonicalize_json(obj: dict) -> bytes:
    """Convert to canonical JSON (sorted keys, minimal whitespace)."""
    return json.dumps(obj, sort_keys=True, separators=(',', ':')).encode()


def sign_manifest(manifest: dict, private_key: ed25519.Ed25519PrivateKey) -> str:
    """Sign manifest and return base64-encoded signature."""
    # Remove signature field if present
    unsigned = {k: v for k, v in manifest.items() if k != "signature"}

    # Canonicalize
    canonical_bytes = canonicalize_json(unsigned)

    # Sign
    signature_bytes = private_key.sign(canonical_bytes)
    signature_b64 = base64.b64encode(signature_bytes).decode()

    return signature_b64


def main():
    parser = argparse.ArgumentParser(
        description="Sign a NOVA manifest (offline machine only)"
    )
    parser.add_argument(
        "manifest",
        type=Path,
        help="Path to manifest.json"
    )
    parser.add_argument(
        "private_key",
        type=Path,
        help="Path to private key (nova-root-key/root.priv)"
    )

    args = parser.parse_args()

    # Validate inputs
    if not args.manifest.exists():
        print(f"ERROR: Manifest not found: {args.manifest}")
        sys.exit(1)

    if not args.private_key.exists():
        print(f"ERROR: Private key not found: {args.private_key}")
        sys.exit(1)

    print("=== NOVA Manifest Signing ===\n")
    print(f"Manifest: {args.manifest}")
    print(f"Private key: {args.private_key}")
    print()

    # Load manifest
    print("Loading manifest...")
    try:
        with open(args.manifest, "r") as f:
            manifest = json.load(f)
    except Exception as e:
        print(f"ERROR: Failed to load manifest: {e}")
        sys.exit(1)

    # Validate manifest structure
    if "release" not in manifest or "packages" not in manifest:
        print("ERROR: Invalid manifest structure")
        sys.exit(1)

    version = manifest["release"].get("version", "unknown")
    sequence = manifest["release"].get("release_sequence", "unknown")
    pkg_count = len(manifest["packages"])

    print(f"Version: {version}")
    print(f"Sequence: {sequence}")
    print(f"Packages: {pkg_count}")
    print()

    # Load private key
    print("Loading private key...")
    try:
        private_key = load_private_key(args.private_key)
    except Exception as e:
        print(f"ERROR: Failed to load private key: {e}")
        sys.exit(1)

    # Sign manifest
    print("Signing manifest...")
    try:
        signature = sign_manifest(manifest, private_key)
    except Exception as e:
        print(f"ERROR: Failed to sign manifest: {e}")
        sys.exit(1)

    # Update manifest with signature
    manifest["signature"] = signature

    # Write signed manifest
    print(f"Writing signed manifest to {args.manifest}...")
    with open(args.manifest, "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)

    print("\n=== Success ===")
    print(f"Manifest signed with {len(signature)} characters (base64)")
    print()
    print("Next steps:")
    print(f"1. Transfer {args.manifest.name} back to connected machine (via USB)")
    print("2. Verify with: python3 verify-manifest.py")


if __name__ == "__main__":
    main()
