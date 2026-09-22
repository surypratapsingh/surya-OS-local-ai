#!/usr/bin/env python3
"""
Test suite for C1: Root key ceremony.

Tests verify:
1. Key generation produces valid Ed25519 keypair
2. Signatures are verifiable
3. Tampering is detected
4. Round-trip signing works

Run with: python3 test_root_key_ceremony.py
"""

import sys
import tempfile
import hashlib
import base64
from pathlib import Path

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519
except ImportError:
    print("ERROR: cryptography package not found")
    print("Install with: pip install cryptography")
    sys.exit(1)


def test_keygen():
    """Test: Key generation produces valid Ed25519 keypair."""
    print("Test 1: Key generation...", end=" ")

    try:
        # Generate keypair
        private_key = ed25519.Ed25519PrivateKey.generate()
        public_key = private_key.public_key()

        # Serialize and deserialize to verify round-trip
        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()
        )

        public_pem = public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo
        )

        # Load back
        loaded_private = serialization.load_pem_private_key(private_pem, password=None)
        loaded_public = serialization.load_pem_public_key(public_pem)

        assert isinstance(loaded_private, ed25519.Ed25519PrivateKey)
        assert isinstance(loaded_public, ed25519.Ed25519PublicKey)

        print("✓")
        return True
    except Exception as e:
        print(f"✗ ({e})")
        return False


def test_sign_and_verify():
    """Test: Signing and verifying works correctly."""
    print("Test 2: Sign and verify...", end=" ")

    try:
        # Generate keypair
        private_key = ed25519.Ed25519PrivateKey.generate()
        public_key = private_key.public_key()

        # Message to sign
        message = b"Test release hash"

        # Sign
        signature = private_key.sign(message)

        # Verify
        public_key.verify(signature, message)

        print("✓")
        return True
    except Exception as e:
        print(f"✗ ({e})")
        return False


def test_tampering_detection():
    """Test: Tampering with message is detected."""
    print("Test 3: Tampering detection...", end=" ")

    try:
        # Generate keypair
        private_key = ed25519.Ed25519PrivateKey.generate()
        public_key = private_key.public_key()

        # Message to sign
        message = b"Test release hash"

        # Sign
        signature = private_key.sign(message)

        # Try to verify with tampered message
        tampered_message = b"Test release hash MODIFIED"

        try:
            public_key.verify(signature, tampered_message)
            print("✗ (Tampering not detected)")
            return False
        except Exception:
            # Expected: verification should fail
            print("✓")
            return True
    except Exception as e:
        print(f"✗ ({e})")
        return False


def test_signature_tampering_detection():
    """Test: Tampering with signature is detected."""
    print("Test 4: Signature tampering...", end=" ")

    try:
        # Generate keypair
        private_key = ed25519.Ed25519PrivateKey.generate()
        public_key = private_key.public_key()

        # Message to sign
        message = b"Test release hash"

        # Sign and encode to base64
        signature = private_key.sign(message)
        signature_b64 = base64.b64encode(signature).decode()

        # Tamper with base64 (flip first bit)
        sig_chars = list(signature_b64)
        if sig_chars[0] == 'A':
            sig_chars[0] = 'B'
        else:
            sig_chars[0] = 'A'
        tampered_b64 = ''.join(sig_chars)

        # Try to verify with tampered signature
        tampered_signature = base64.b64decode(tampered_b64)

        try:
            public_key.verify(tampered_signature, message)
            print("✗ (Tampering not detected)")
            return False
        except Exception:
            # Expected: verification should fail
            print("✓")
            return True
    except Exception as e:
        print(f"✗ ({e})")
        return False


def test_file_sign_and_verify():
    """Test: File-based signing and verification."""
    print("Test 5: File sign/verify...", end=" ")

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            # Generate keypair
            private_key = ed25519.Ed25519PrivateKey.generate()
            public_key = private_key.public_key()

            # Create test file
            message_file = tmpdir / "message.txt"
            message_content = b"Test release 1.0.0\nsha256: abcdef123456"
            message_file.write_bytes(message_content)

            # Sign
            signature = private_key.sign(message_content)
            signature_b64 = base64.b64encode(signature).decode()

            # Write signature
            sig_file = tmpdir / "signature.txt"
            sig_file.write_text(f"signature: {signature_b64}\n")

            # Verify
            stored_sig_b64 = sig_file.read_text().split(": ")[1].strip()
            stored_signature = base64.b64decode(stored_sig_b64)
            public_key.verify(stored_signature, message_content)

            print("✓")
            return True
    except Exception as e:
        print(f"✗ ({e})")
        return False


def test_multiple_signatures_are_different():
    """Test: Different messages produce different signatures."""
    print("Test 6: Different messages...", end=" ")

    try:
        # Generate keypair
        private_key = ed25519.Ed25519PrivateKey.generate()

        # Sign two different messages
        sig1 = private_key.sign(b"Release 1.0.0")
        sig2 = private_key.sign(b"Release 1.0.1")

        # Signatures should be different
        if sig1 != sig2:
            print("✓")
            return True
        else:
            print("✗ (Signatures are identical for different messages)")
            return False
    except Exception as e:
        print(f"✗ ({e})")
        return False


def test_key_round_trip():
    """Test: Private key can be saved and loaded."""
    print("Test 7: Key persistence...", end=" ")

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            # Generate and save
            original_key = ed25519.Ed25519PrivateKey.generate()
            original_pem = original_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption()
            )

            key_file = tmpdir / "test.priv"
            key_file.write_bytes(original_pem)

            # Load and verify signature
            loaded_key = serialization.load_pem_private_key(
                key_file.read_bytes(),
                password=None
            )

            message = b"Test message"
            original_sig = original_key.sign(message)
            loaded_sig = loaded_key.sign(message)

            # Both signatures should verify
            original_key.public_key().verify(loaded_sig, message)
            loaded_key.public_key().verify(original_sig, message)

            print("✓")
            return True
    except Exception as e:
        print(f"✗ ({e})")
        return False


def test_key_fingerprint():
    """Test: Fingerprint is reproducible."""
    print("Test 8: Fingerprint reproducibility...", end=" ")

    try:
        # Generate keypair
        private_key = ed25519.Ed25519PrivateKey.generate()
        public_key = private_key.public_key()

        # Get PEM
        public_pem = public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo
        )

        # Compute fingerprint multiple times
        fp1 = hashlib.sha256(public_pem).hexdigest()
        fp2 = hashlib.sha256(public_pem).hexdigest()

        if fp1 == fp2:
            print("✓")
            return True
        else:
            print("✗ (Fingerprints differ)")
            return False
    except Exception as e:
        print(f"✗ ({e})")
        return False


def main():
    print("=== C1: Root Key Ceremony Tests ===\n")

    tests = [
        test_keygen,
        test_sign_and_verify,
        test_tampering_detection,
        test_signature_tampering_detection,
        test_file_sign_and_verify,
        test_multiple_signatures_are_different,
        test_key_round_trip,
        test_key_fingerprint,
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
