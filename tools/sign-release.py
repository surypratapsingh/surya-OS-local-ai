#!/usr/bin/env python3
"""
Sign a NOVA release with the root key (offline machine only).

Usage:
    python3 sign-release.py <image-path> <private-key-path> [version]

Example (on offline machine):
    python3 sign-release.py nova-release-hash.txt nova-root-key/root.priv 1.0.0

Output:
    nova-release-signature.txt  (transfer to connected machine for installation)
"""

import sys
import hashlib
import base64
import argparse
from pathlib import Path
from datetime import datetime

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519
except ImportError:
    print("ERROR: cryptography package not found.")
    print("Install with: pip install cryptography")
    sys.exit(1)


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


def load_private_key(key_path: Path) -> ed25519.Ed25519PrivateKey:
    """Load Ed25519 private key from PEM file (PKCS8 format recommended)."""
    with open(key_path, "rb") as f:
        pem_data = f.read()

    key = serialization.load_pem_private_key(pem_data, password=None)
    if not isinstance(key, ed25519.Ed25519PrivateKey):
        raise ValueError(f"Expected Ed25519 private key, got {type(key).__name__}")

    return key


def sign_file(message_path: Path, private_key: ed25519.Ed25519PrivateKey) -> str:
    """Sign a file and return base64-encoded signature."""
    with open(message_path, "rb") as f:
        message = f.read()

    signature_bytes = private_key.sign(message)
    signature_b64 = base64.b64encode(signature_bytes).decode()

    return signature_b64


def format_version(version: str) -> str:
    """Validate and format version string (semver)."""
    parts = version.split(".")
    if len(parts) < 2:
        raise ValueError(f"Version must be in format X.Y.Z, got {version}")
    return version


def main():
    parser = argparse.ArgumentParser(
        description="Sign a NOVA release with the offline root key."
    )
    parser.add_argument(
        "message_file",
        type=Path,
        help="Release hash file (nova-release-hash.txt) to sign"
    )
    parser.add_argument(
        "private_key",
        type=Path,
        help="Path to private key (nova-root-key/root.priv)"
    )
    parser.add_argument(
        "--version",
        default="1.0.0",
        help="Release version (default: 1.0.0)"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("nova-release-signature.txt"),
        help="Output file for signature (default: nova-release-signature.txt)"
    )

    args = parser.parse_args()

    # Validate inputs
    if not args.message_file.exists():
        print(f"ERROR: Message file not found: {args.message_file}")
        sys.exit(1)

    if not args.private_key.exists():
        print(f"ERROR: Private key file not found: {args.private_key}")
        sys.exit(1)

    try:
        version = format_version(args.version)
    except ValueError as e:
        print(f"ERROR: Invalid version: {e}")
        sys.exit(1)

    # Check file permissions (warn if private key is world-readable)
    import os
    mode = os.stat(args.private_key).st_mode
    if mode & 0o077:
        print(f"WARNING: Private key has permissive permissions: {oct(mode)}")
        print("Recommended: chmod 600 <private-key-path>")

    print("=== NOVA Release Signing ===")
    print(f"Version: {version}")
    print(f"Message file: {args.message_file}")
    print(f"Private key: {args.private_key}")
    print()

    # Load private key
    print("Loading private key...")
    try:
        private_key = load_private_key(args.private_key)
    except Exception as e:
        print(f"ERROR: Failed to load private key: {e}")
        sys.exit(1)

    # Sign
    print("Signing release...")
    try:
        signature_b64 = sign_file(args.message_file, private_key)
    except Exception as e:
        print(f"ERROR: Failed to sign release: {e}")
        sys.exit(1)

    # Write signature file
    timestamp = datetime.utcnow().isoformat() + "Z"
    signature_content = f"""# NOVA Release Signature
# Generated: {timestamp}
# Version: {version}

signature: {signature_b64}
algorithm: Ed25519
message-file: {args.message_file.name}
version: {version}
"""

    print(f"Writing signature to {args.output}...")
    with open(args.output, "w") as f:
        f.write(signature_content)

    print("\n=== Success ===")
    print(f"Signature written to: {args.output}")
    print("\nNext steps:")
    print("1. Transfer nova-release-signature.txt to the connected machine (via USB)")
    print("2. On connected machine: python3 verify-release.py")
    print("3. If verification passes, run: scripts/install-release.sh")


if __name__ == "__main__":
    main()
