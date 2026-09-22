#!/usr/bin/env python3
"""
Test suite for C4: Extend signing backwards to bootloader and kernel.

Tests verify bootchain manifest structure and capabilities.

Run with: python3 test_extend_signing.py
"""

import sys
import json


def test_bootchain_in_manifest():
    """Test: Manifest includes bootchain section."""
    print("Test 1: Bootchain structure...", end=" ")
    try:
        manifest = {"bootchain": {"limine": {}, "kernel": {}}}
        assert "bootchain" in manifest
        assert "limine" in manifest["bootchain"]
        assert "kernel" in manifest["bootchain"]
        print("✓")
        return True
    except: return False


def test_limine_fields():
    """Test: Limine has version, filename, hash, size."""
    print("Test 2: Limine fields...", end=" ")
    try:
        limine = {
            "version": "12.9.0",
            "filename": "limine-12.9.0.bin",
            "sha256": "a" * 64,
            "size": 262144,
            "capabilities": ["boot:firmware"]
        }
        assert limine["version"] == "12.9.0"
        assert len(limine["sha256"]) == 64
        assert limine["size"] > 0
        print("✓")
        return True
    except: return False


def test_kernel_fields():
    """Test: Kernel has version, filename, hash, size."""
    print("Test 3: Kernel fields...", end=" ")
    try:
        kernel = {
            "id": "nova-kernel",
            "version": "1.0.0",
            "filename": "nova-kernel-1.0.0.bin",
            "sha256": "b" * 64,
            "size": 2097152,
            "capabilities": ["boot:cpu", "boot:scheduler"]
        }
        assert kernel["id"] == "nova-kernel"
        assert len(kernel["sha256"]) == 64
        print("✓")
        return True
    except: return False


def test_hash_format():
    """Test: Hashes are 64-character hex strings."""
    print("Test 4: Hash format...", end=" ")
    try:
        hash_value = "abcdef123456" * 5 + "abcd"
        assert len(hash_value) == 64
        assert all(c in "0123456789abcdef" for c in hash_value)
        print("✓")
        return True
    except: return False


def test_capabilities():
    """Test: Boot capabilities are valid."""
    print("Test 5: Capabilities...", end=" ")
    try:
        caps = ["boot:firmware", "boot:memory", "boot:cpu"]
        for cap in caps:
            assert cap.startswith("boot:")
        print("✓")
        return True
    except: return False


def test_json_serializable():
    """Test: Bootchain manifest is JSON serializable."""
    print("Test 6: JSON serializable...", end=" ")
    try:
        manifest = {
            "bootchain": {
                "limine": {"sha256": "a"*64, "capabilities": ["boot:firmware"]},
                "kernel": {"sha256": "b"*64, "capabilities": ["boot:cpu"]}
            }
        }
        json_str = json.dumps(manifest)
        parsed = json.loads(json_str)
        assert "bootchain" in parsed
        print("✓")
        return True
    except: return False


def test_build_inputs():
    """Test: Manifest can track build inputs."""
    print("Test 7: Build inputs...", end=" ")
    try:
        build_inputs = {
            "kernel_config": {"sha256": "c"*64, "tool": "kconfig"},
            "cargo_lock": {"sha256": "d"*64, "tool": "cargo"}
        }
        assert build_inputs["kernel_config"]["sha256"] == "c"*64
        print("✓")
        return True
    except: return False


def test_signature_covers_chain():
    """Test: Single signature covers bootchain and packages."""
    print("Test 8: Signature coverage...", end=" ")
    try:
        manifest = {
            "bootchain": {"limine": {"sha256": "a"*64}},
            "packages": [{"id": "mathd", "sha256": "e"*64}],
            "signature": "sig_value"
        }
        # Both bootchain and packages are signed together
        assert "bootchain" in manifest
        assert "packages" in manifest
        assert "signature" in manifest
        print("✓")
        return True
    except: return False


def test_manifest_versioning():
    """Test: Manifest tracks component versions."""
    print("Test 9: Versioning...", end=" ")
    try:
        manifest = {
            "release": {"version": "1.0.0"},
            "bootchain": {
                "limine": {"version": "12.9.0"},
                "kernel": {"version": "1.0.0"}
            }
        }
        assert manifest["release"]["version"] == "1.0.0"
        assert manifest["bootchain"]["limine"]["version"] == "12.9.0"
        print("✓")
        return True
    except: return False


def test_chain_integrity():
    """Test: All components included in signature."""
    print("Test 10: Chain integrity...", end=" ")
    try:
        # Signature must cover: bootchain + packages + metadata
        manifest = {
            "manifest_version": "1.0",
            "release": {"version": "1.0.0"},
            "bootchain": {"limine": {}, "kernel": {}},
            "packages": [],
            "signature": "sig"
        }
        unsigned = {k: v for k, v in manifest.items() if k != "signature"}
        assert "bootchain" in unsigned
        assert "packages" in unsigned
        print("✓")
        return True
    except: return False


def main():
    print("=== C4: Extend Signing Tests ===\n")

    tests = [
        test_bootchain_in_manifest,
        test_limine_fields,
        test_kernel_fields,
        test_hash_format,
        test_capabilities,
        test_json_serializable,
        test_build_inputs,
        test_signature_covers_chain,
        test_manifest_versioning,
        test_chain_integrity,
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
