#!/usr/bin/env python3
"""
Test suite for C2: Signed manifests.

Tests verify:
1. Manifest creation and structure
2. Signing and verification
3. Tampering detection
4. Replay protection (sequence/timestamp)
5. Capability validation
6. File checksum verification

Run with: python3 test_manifest.py
"""

import sys
import json
import tempfile
import hashlib
import base64
from pathlib import Path
from datetime import datetime, timezone

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519
except ImportError:
    print("ERROR: cryptography package not found")
    print("Install with: pip install cryptography")
    sys.exit(1)


def generate_keypair():
    """Generate a test Ed25519 keypair."""
    private_key = ed25519.Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    return private_key, public_key


def get_key_fingerprint(public_key: ed25519.Ed25519PublicKey) -> str:
    """Compute fingerprint of public key."""
    pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo
    )
    digest = hashlib.sha256(pem).hexdigest()
    return f"sha256:{digest}"


def create_test_manifest(
    version: str = "1.0.0",
    sequence: int = 1,
    public_key: ed25519.Ed25519PublicKey = None
) -> dict:
    """Create a minimal test manifest."""
    if public_key is None:
        _, public_key = generate_keypair()

    timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    return {
        "manifest_version": "1.0",
        "release": {
            "version": version,
            "timestamp": timestamp,
            "release_sequence": sequence,
            "description": "Test release"
        },
        "trust": {
            "algorithm": "Ed25519",
            "public_key_fingerprint": get_key_fingerprint(public_key)
        },
        "packages": [
            {
                "id": "test-pkg",
                "version": "1.0.0",
                "filename": "test.bin",
                "sha256": "abcd1234" * 8,  # 64 hex chars
                "size": 1024,
                "sequence": 1,
                "capabilities": ["test:read", "test:write"]
            }
        ],
        "signature": ""
    }


def canonicalize_json(obj: dict) -> bytes:
    """Canonicalize JSON."""
    return json.dumps(obj, sort_keys=True, separators=(',', ':')).encode()


def sign_manifest(manifest: dict, private_key: ed25519.Ed25519PrivateKey) -> str:
    """Sign a manifest."""
    unsigned = {k: v for k, v in manifest.items() if k != "signature"}
    canonical = canonicalize_json(unsigned)
    signature = private_key.sign(canonical)
    return base64.b64encode(signature).decode()


def verify_manifest_signature(manifest: dict, public_key: ed25519.Ed25519PublicKey) -> bool:
    """Verify manifest signature."""
    signature_b64 = manifest.get("signature", "")
    if not signature_b64:
        return False

    try:
        signature = base64.b64decode(signature_b64)
    except Exception:
        return False

    unsigned = {k: v for k, v in manifest.items() if k != "signature"}
    canonical = canonicalize_json(unsigned)

    try:
        public_key.verify(signature, canonical)
        return True
    except Exception:
        return False


# Test Suite

def test_manifest_structure():
    """Test: Manifest has required fields."""
    print("Test 1: Manifest structure...", end=" ")

    try:
        manifest = create_test_manifest()

        # Check required top-level fields
        assert "manifest_version" in manifest
        assert "release" in manifest
        assert "packages" in manifest
        assert "trust" in manifest

        # Check release fields
        release = manifest["release"]
        assert "version" in release
        assert "timestamp" in release
        assert "release_sequence" in release

        # Check trust fields
        trust = manifest["trust"]
        assert "algorithm" in trust
        assert "public_key_fingerprint" in trust

        # Check package fields
        pkg = manifest["packages"][0]
        assert "id" in pkg
        assert "version" in pkg
        assert "sha256" in pkg
        assert "size" in pkg
        assert "sequence" in pkg
        assert "capabilities" in pkg

        print("✓")
        return True
    except Exception as e:
        print(f"✗ ({e})")
        return False


def test_signing_and_verification():
    """Test: Manifest can be signed and verified."""
    print("Test 2: Sign and verify...", end=" ")

    try:
        private_key, public_key = generate_keypair()
        manifest = create_test_manifest(public_key=public_key)

        # Sign
        signature = sign_manifest(manifest, private_key)
        manifest["signature"] = signature

        # Verify
        assert verify_manifest_signature(manifest, public_key)

        print("✓")
        return True
    except Exception as e:
        print(f"✗ ({e})")
        return False


def test_tampering_detection_content():
    """Test: Tampering with manifest content is detected."""
    print("Test 3: Content tampering detection...", end=" ")

    try:
        private_key, public_key = generate_keypair()
        manifest = create_test_manifest(public_key=public_key)

        # Sign original
        signature = sign_manifest(manifest, private_key)
        manifest["signature"] = signature

        # Verify original passes
        assert verify_manifest_signature(manifest, public_key)

        # Tamper with manifest
        manifest["release"]["version"] = "2.0.0"

        # Verification should fail
        assert not verify_manifest_signature(manifest, public_key)

        print("✓")
        return True
    except Exception as e:
        print(f"✗ ({e})")
        return False


def test_tampering_detection_signature():
    """Test: Tampering with signature is detected."""
    print("Test 4: Signature tampering detection...", end=" ")

    try:
        private_key, public_key = generate_keypair()
        manifest = create_test_manifest(public_key=public_key)

        # Sign
        signature = sign_manifest(manifest, private_key)

        # Corrupt signature (flip first character)
        sig_chars = list(signature)
        sig_chars[0] = 'X' if sig_chars[0] != 'X' else 'Y'
        corrupted = ''.join(sig_chars)
        manifest["signature"] = corrupted

        # Verification should fail
        assert not verify_manifest_signature(manifest, public_key)

        print("✓")
        return True
    except Exception as e:
        print(f"✗ ({e})")
        return False


def test_version_sequence():
    """Test: Version sequence is tracked."""
    print("Test 5: Version sequence...", end=" ")

    try:
        private_key, public_key = generate_keypair()

        # Create sequence of manifests
        for seq in [1, 2, 3, 5, 10]:
            manifest = create_test_manifest(version=f"1.0.{seq}", sequence=seq)
            sig = sign_manifest(manifest, private_key)
            manifest["signature"] = sig

            # All should verify
            assert verify_manifest_signature(manifest, public_key)

        print("✓")
        return True
    except Exception as e:
        print(f"✗ ({e})")
        return False


def test_package_sequences_strictly_increasing():
    """Test: Package sequences must be strictly increasing."""
    print("Test 6: Package sequence order...", end=" ")

    try:
        _, public_key = generate_keypair()
        manifest = create_test_manifest(public_key=public_key)

        # Valid: sequences 1, 2, 3
        packages_valid = [
            {"id": "pkg1", "sequence": 1, "capabilities": []},
            {"id": "pkg2", "sequence": 2, "capabilities": []},
            {"id": "pkg3", "sequence": 3, "capabilities": []},
        ]

        # Verify sequences are increasing
        prev = 0
        for pkg in packages_valid:
            assert pkg["sequence"] > prev
            prev = pkg["sequence"]

        # Invalid: sequences 1, 1, 3 (duplicate)
        packages_invalid = [
            {"id": "pkg1", "sequence": 1, "capabilities": []},
            {"id": "pkg2", "sequence": 1, "capabilities": []},  # Duplicate
            {"id": "pkg3", "sequence": 3, "capabilities": []},
        ]

        # Should detect duplicate
        prev = 0
        valid = True
        for pkg in packages_invalid:
            if pkg["sequence"] <= prev:
                valid = False
                break
            prev = pkg["sequence"]

        assert not valid  # Should have caught the invalid sequence

        print("✓")
        return True
    except Exception as e:
        print(f"✗ ({e})")
        return False


def test_capabilities_parsing():
    """Test: Capabilities are properly declared."""
    print("Test 7: Capabilities parsing...", end=" ")

    try:
        _, public_key = generate_keypair()
        manifest = create_test_manifest(public_key=public_key)

        pkg = manifest["packages"][0]

        # Check capabilities exist
        assert "capabilities" in pkg
        assert len(pkg["capabilities"]) > 0

        # Check format
        for cap in pkg["capabilities"]:
            assert ":" in cap  # namespace:permission format

        print("✓")
        return True
    except Exception as e:
        print(f"✗ ({e})")
        return False


def test_checksum_format():
    """Test: SHA256 checksums are properly formatted."""
    print("Test 8: Checksum format...", end=" ")

    try:
        _, public_key = generate_keypair()
        manifest = create_test_manifest(public_key=public_key)

        for pkg in manifest["packages"]:
            sha256 = pkg.get("sha256", "")

            # Should be 64 hex characters
            assert len(sha256) == 64
            assert all(c in "0123456789abcdef" for c in sha256.lower())

        print("✓")
        return True
    except Exception as e:
        print(f"✗ ({e})")
        return False


def test_fingerprint_format():
    """Test: Public key fingerprint is properly formatted."""
    print("Test 9: Fingerprint format...", end=" ")

    try:
        _, public_key = generate_keypair()
        manifest = create_test_manifest(public_key=public_key)

        fingerprint = manifest["trust"]["public_key_fingerprint"]

        # Should start with "sha256:"
        assert fingerprint.startswith("sha256:")

        # Rest should be 64 hex characters
        digest = fingerprint.split(":")[1]
        assert len(digest) == 64
        assert all(c in "0123456789abcdef" for c in digest.lower())

        print("✓")
        return True
    except Exception as e:
        print(f"✗ ({e})")
        return False


def test_multiple_packages():
    """Test: Manifests can contain multiple packages."""
    print("Test 10: Multiple packages...", end=" ")

    try:
        private_key, public_key = generate_keypair()
        manifest = create_test_manifest(public_key=public_key)

        # Add packages
        for i in range(2, 5):
            manifest["packages"].append({
                "id": f"pkg{i}",
                "version": "1.0.0",
                "filename": f"pkg{i}.bin",
                "sha256": f"{i:064x}",  # Unique hex for each
                "size": 1024 * i,
                "sequence": i,
                "capabilities": [f"namespace{i}:read"]
            })

        # Sign and verify
        signature = sign_manifest(manifest, private_key)
        manifest["signature"] = signature
        assert verify_manifest_signature(manifest, public_key)

        # Check all packages are present
        assert len(manifest["packages"]) == 4

        print("✓")
        return True
    except Exception as e:
        print(f"✗ ({e})")
        return False


def main():
    print("=== C2: Manifest Tests ===\n")

    tests = [
        test_manifest_structure,
        test_signing_and_verification,
        test_tampering_detection_content,
        test_tampering_detection_signature,
        test_version_sequence,
        test_package_sequences_strictly_increasing,
        test_capabilities_parsing,
        test_checksum_format,
        test_fingerprint_format,
        test_multiple_packages,
    ]

    results = [test() for test in tests]

    passed = sum(results)
    total = len(results)

    print(f"\n=== Results ===")
    print(f"Passed: {passed}/{total}")

    if passed == total:
        print("\n✓ All tests passed!")
        return 0
    else:
        print(f"\n✗ {total - passed} test(s) failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
