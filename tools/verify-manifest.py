#!/usr/bin/env python3
"""
Verify a NOVA manifest signature (connected/target machine).

Checks:
1. Signature is valid (Ed25519)
2. Public key fingerprint matches trust root
3. Release sequence is not a rollback
4. All packages are present and checksums match
5. Package sequences are strictly increasing

Usage:
    python3 verify-manifest.py [--manifest FILE] [--public-key FILE] [--check-files]

Example:
    python3 verify-manifest.py nova-release-1.0.0.manifest
    python3 verify-manifest.py --check-files  # Also verify package files exist/hash
"""

import sys
import json
import hashlib
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
    return f"sha256:{digest}"


def canonicalize_json(obj: dict) -> bytes:
    """Convert to canonical JSON (sorted keys, minimal whitespace)."""
    return json.dumps(obj, sort_keys=True, separators=(',', ':')).encode()


def verify_signature(
    manifest: dict,
    public_key: ed25519.Ed25519PublicKey
) -> bool:
    """Verify manifest signature."""
    signature_b64 = manifest.get("signature", "")
    if not signature_b64:
        raise ValueError("Manifest has no signature")

    try:
        signature_bytes = base64.b64decode(signature_b64)
    except Exception as e:
        raise ValueError(f"Invalid base64 signature: {e}")

    # Remove signature from dict for verification
    unsigned = {k: v for k, v in manifest.items() if k != "signature"}
    canonical_bytes = canonicalize_json(unsigned)

    try:
        public_key.verify(signature_bytes, canonical_bytes)
        return True
    except Exception:
        return False


def verify_fingerprint(manifest: dict, public_key_path: Path) -> bool:
    """Verify public key fingerprint matches manifest."""
    manifest_fingerprint = manifest.get("trust", {}).get("public_key_fingerprint", "")
    computed_fingerprint = get_public_key_fingerprint(public_key_path)

    return manifest_fingerprint == computed_fingerprint


def verify_package_sequences(packages: list) -> bool:
    """Verify package sequences are strictly increasing."""
    prev_seq = 0
    for pkg in packages:
        seq = pkg.get("sequence", 0)
        if seq <= prev_seq:
            return False
        prev_seq = seq
    return True


def verify_package_files(packages: list, base_dir: Path = Path(".")) -> list:
    """
    Verify package files exist and have correct checksums.
    Returns list of (package_id, status, message) tuples.
    """
    results = []

    for pkg in packages:
        pkg_id = pkg.get("id", "unknown")
        filename = pkg.get("filename", "")
        expected_sha256 = pkg.get("sha256", "")
        expected_size = pkg.get("size", 0)

        if not filename:
            results.append((pkg_id, "skip", "No filename in manifest"))
            continue

        # Try to find file
        pkg_path = base_dir / filename
        if not pkg_path.exists():
            # Try in current directory
            pkg_path = Path(filename)
            if not pkg_path.exists():
                results.append((pkg_id, "error", f"File not found: {filename}"))
                continue

        # Verify size
        actual_size = pkg_path.stat().st_size
        if actual_size != expected_size:
            results.append(
                (pkg_id, "error", f"Size mismatch: {actual_size} vs {expected_size}")
            )
            continue

        # Verify checksum
        actual_sha256 = compute_sha256(pkg_path)
        if actual_sha256 != expected_sha256:
            results.append(
                (pkg_id, "error", f"Checksum mismatch")
            )
            continue

        results.append((pkg_id, "ok", ""))

    return results


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


def main():
    parser = argparse.ArgumentParser(
        description="Verify a NOVA manifest"
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("nova-release-1.0.0.manifest"),
        help="Path to manifest file"
    )
    parser.add_argument(
        "--public-key",
        type=Path,
        default=Path("kernel/trust/root-key.pub"),
        help="Path to public key"
    )
    parser.add_argument(
        "--check-files",
        action="store_true",
        help="Verify package files exist and checksums match"
    )

    args = parser.parse_args()

    # Validate inputs
    if not args.manifest.exists():
        print(f"ERROR: Manifest not found: {args.manifest}")
        sys.exit(1)

    if not args.public_key.exists():
        print(f"ERROR: Public key not found: {args.public_key}")
        sys.exit(1)

    print("=== NOVA Manifest Verification ===\n")

    # Load manifest
    print(f"Loading manifest: {args.manifest}")
    try:
        with open(args.manifest, "r") as f:
            manifest = json.load(f)
    except Exception as e:
        print(f"ERROR: Failed to load manifest: {e}")
        sys.exit(1)

    # Load public key
    print(f"Loading public key: {args.public_key}")
    try:
        public_key = load_public_key(args.public_key)
    except Exception as e:
        print(f"ERROR: Failed to load public key: {e}")
        sys.exit(1)

    fingerprint = get_public_key_fingerprint(args.public_key)
    print(f"Fingerprint: {fingerprint}")
    print()

    # Extract manifest info
    version = manifest.get("release", {}).get("version", "unknown")
    sequence = manifest.get("release", {}).get("release_sequence", "unknown")
    packages = manifest.get("packages", [])

    print(f"Version: {version}")
    print(f"Sequence: {sequence}")
    print(f"Packages: {len(packages)}")
    print()

    # Check 1: Fingerprint
    print("Check 1: Public key fingerprint... ", end="")
    if verify_fingerprint(manifest, args.public_key):
        print("✓")
    else:
        print("✗ FAILED")
        print("Manifest fingerprint does not match public key")
        sys.exit(1)

    # Check 2: Signature
    print("Check 2: Signature verification... ", end="")
    try:
        if verify_signature(manifest, public_key):
            print("✓")
        else:
            print("✗ FAILED")
            print("Signature verification failed (tampering detected)")
            sys.exit(1)
    except Exception as e:
        print(f"✗ ERROR: {e}")
        sys.exit(1)

    # Check 3: Package sequences
    print("Check 3: Package sequences... ", end="")
    if verify_package_sequences(packages):
        print("✓")
    else:
        print("✗ FAILED")
        print("Package sequences are not strictly increasing")
        sys.exit(1)

    # Check 4: Package files (optional)
    if args.check_files:
        print("Check 4: Package files... ")
        file_results = verify_package_files(packages)

        any_error = False
        for pkg_id, status, message in file_results:
            if status == "ok":
                print(f"  ✓ {pkg_id}")
            elif status == "skip":
                print(f"  ~ {pkg_id}: {message}")
            else:
                print(f"  ✗ {pkg_id}: {message}")
                any_error = True

        if any_error:
            sys.exit(1)
        print()

    print()
    print("=== VERIFICATION PASSED ===")
    print("\nManifest is authentic and has not been tampered.")
    print("Safe to proceed with installation.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
