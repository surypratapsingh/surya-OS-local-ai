#!/usr/bin/env python3
"""
Verify a NOVA release signature (connected machine only).

Usage:
    python3 verify-release.py [--signature FILE] [--public-key FILE]

Example:
    python3 verify-release.py  # Uses defaults
    python3 verify-release.py --signature custom-sig.txt --public-key /path/to/root.pub

Defaults:
    signature: nova-release-signature.txt
    public-key: ../kernel/trust/root-key.pub
"""

import sys
import hashlib
import base64
import argparse
from pathlib import Path

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519
except ImportError:
    print("ERROR: cryptography package not found.")
    print("Install with: pip install cryptography")
    sys.exit(1)


def load_public_key(key_path: Path) -> ed25519.Ed25519PublicKey:
    """Load Ed25519 public key from PEM file."""
    with open(key_path, "rb") as f:
        pem_data = f.read()

    key = serialization.load_pem_public_key(pem_data)
    if not isinstance(key, ed25519.Ed25519PublicKey):
        raise ValueError(f"Expected Ed25519 public key, got {type(key).__name__}")

    return key


def get_public_key_fingerprint(key_path: Path) -> str:
    """Get SHA256 fingerprint of public key."""
    with open(key_path, "rb") as f:
        key_data = f.read()

    digest = hashlib.sha256(key_data).hexdigest()
    return digest


def parse_signature_file(sig_path: Path) -> dict:
    """Parse signature file and extract fields."""
    with open(sig_path, "r") as f:
        content = f.read()

    result = {}
    for line in content.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        if ": " in line:
            key, value = line.split(": ", 1)
            result[key.lower()] = value

    return result


def verify_signature(
    signature_b64: str,
    message_path: Path,
    public_key: ed25519.Ed25519PublicKey
) -> bool:
    """Verify an Ed25519 signature."""
    try:
        signature_bytes = base64.b64decode(signature_b64)
    except Exception as e:
        raise ValueError(f"Invalid base64 signature: {e}")

    with open(message_path, "rb") as f:
        message = f.read()

    try:
        public_key.verify(signature_bytes, message)
        return True
    except Exception:
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Verify a NOVA release signature."
    )
    parser.add_argument(
        "--signature",
        type=Path,
        default=Path("nova-release-signature.txt"),
        help="Path to signature file"
    )
    parser.add_argument(
        "--public-key",
        type=Path,
        default=Path("kernel/trust/root-key.pub"),
        help="Path to public key"
    )
    parser.add_argument(
        "--message-file",
        type=Path,
        help="Override message file from signature (default: from signature file)"
    )

    args = parser.parse_args()

    # Validate inputs
    if not args.signature.exists():
        print(f"ERROR: Signature file not found: {args.signature}")
        sys.exit(1)

    if not args.public_key.exists():
        print(f"ERROR: Public key file not found: {args.public_key}")
        sys.exit(1)

    print("=== NOVA Release Verification ===\n")

    # Load public key
    print(f"Loading public key: {args.public_key}")
    try:
        public_key = load_public_key(args.public_key)
    except Exception as e:
        print(f"ERROR: Failed to load public key: {e}")
        sys.exit(1)

    fingerprint = get_public_key_fingerprint(args.public_key)
    print(f"Fingerprint (SHA256): {fingerprint}")
    print()

    # Parse signature file
    print(f"Loading signature file: {args.signature}")
    try:
        sig_data = parse_signature_file(args.signature)
    except Exception as e:
        print(f"ERROR: Failed to parse signature file: {e}")
        sys.exit(1)

    if "signature" not in sig_data:
        print("ERROR: No 'signature' field in signature file")
        sys.exit(1)

    signature_b64 = sig_data["signature"]
    algorithm = sig_data.get("algorithm", "unknown")
    version = sig_data.get("version", "unknown")

    print(f"Algorithm: {algorithm}")
    print(f"Version: {version}")
    print()

    # Determine message file
    if args.message_file:
        message_path = args.message_file
    else:
        message_filename = sig_data.get("message-file", "nova-release-hash.txt")
        message_path = Path(message_filename)

    if not message_path.exists():
        print(f"ERROR: Message file not found: {message_path}")
        sys.exit(1)

    print(f"Message file: {message_path}")
    print()

    # Verify signature
    print("Verifying signature...")
    try:
        is_valid = verify_signature(signature_b64, message_path, public_key)
    except Exception as e:
        print(f"ERROR: Verification failed: {e}")
        sys.exit(1)

    print()
    if is_valid:
        print("✓ SIGNATURE VERIFIED")
        print("\nRelease is authentic and signed by the root key holder.")
        print("Safe to proceed with installation.")
        return 0
    else:
        print("✗ SIGNATURE VERIFICATION FAILED")
        print("\nThe release may be tampered or corrupted.")
        print("DO NOT INSTALL. Investigate before proceeding.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
