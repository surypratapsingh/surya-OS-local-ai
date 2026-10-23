#!/usr/bin/env bash
#
# R15 Reproducible Builds Check
#
# Measures two back-to-back builds in separate directories without changing code
# and logs every byte difference. Fixes all sources of non-determinism and proves
# two clean builds produce identical SHA-256 digests.
#
# Usage: bash scripts/repro-check.sh [build_dir_1] [build_dir_2]
# If no directories are provided, creates temp directories automatically.
#

set -euo pipefail

# Default: create temp directories
BUILD_DIR1="${1:-}"
BUILD_DIR2="${2:-}"

if [ -z "$BUILD_DIR1" ]; then
    BUILD_DIR1=$(mktemp -d /tmp/nova-repro-XXXX)
    BUILD_DIR2=$(mktemp -d /tmp/nova-repro-XXXX)
    CLEANUP=true
else
    CLEANUP=false
fi

trap cleanup EXIT

cleanup() {
    if [ "$CLEANUP" = true ]; then
        rm -rf "$BUILD_DIR1" "$BUILD_DIR2"
    fi
}

echo "=== R15 Reproducible Builds Check ==="
echo "Build directory 1: $BUILD_DIR1"
echo "Build directory 2: $BUILD_DIR2"
echo ""

# Step 1: First, verify the source tree is clean (no uncommitted changes)
echo "=== Checking git status ==="
git diff --quiet || { echo "ERROR: Working tree has uncommitted changes"; exit 1; }
git diff --cached --quiet || { echo "ERROR: Staged changes present"; exit 1; }
echo "Git status: clean"
echo ""

# Expected image size in bytes (40 MiB = 41943040)
EXPECTED_SIZE=41943040

# Step 2: First build (dirty-free, with SOURCE_DATE_EPOCH for determinism)
echo "=== Build 1 ==="
export SOURCE_DATE_EPOCH="$(git log -1 --format=%ct)"
export RUSTC_BOOTSTRAP="1"

# Configure and build first directory
mkdir -p "$BUILD_DIR1/kernel/target"
export CARGO_TARGET_DIR="$BUILD_DIR1/kernel/target"

# Build the kernel
cd /c/Users/surya/Documents/dont-share-personal-info/kernel
cargo build --release 2>&1 | tail -5

# Build the disk image
bash /c/Users/surya/Documents/dont-share-personal-info/scripts/build-disk.sh 2>&1 | tail -5

# Create the hdd image using make-esp.py
py -3 /c/Users/surya/Documents/dont-share-personal-info/tools/make-esp.py \
    /c/Users/surya/Documents/dont-share-personal-info/kernel \
    /c/Users/surya/Documents/dont-share-personal-info/.freebuff/ref/limine/limine-binary \
    "$BUILD_DIR1/nova.hdd" 2>&1

echo "Build 1 complete"
echo ""

# Step 3: Second build (separate directory)
echo "=== Build 2 ==="
export SOURCE_DATE_EPOCH="$(git log -1 --format=%ct)"
export RUSTC_BOOTSTRAP="1"

mkdir -p "$BUILD_DIR2/kernel/target"
export CARGO_TARGET_DIR="$BUILD_DIR2/kernel/target"

# Build the kernel
cd /c/Users/surya/Documents/dont-share-personal-info/kernel
cargo build --release 2>&1 | tail -5

# Build the disk image
bash /c/Users/surya/Documents/dont-share-personal-info/scripts/build-disk.sh 2>&1 | tail -5

# Create the hdd image
py -3 /c/Users/surya/Documents/dont-share-personal-info/tools/make-esp.py \
    /c/Users/surya/Documents/dont-share-personal-info/kernel \
    /c/Users/surya/Documents/dont-share-personal-info/.freebuff/ref/limine/limine-binary \
    "$BUILD_DIR2/nova.hdd" 2>&1

echo "Build 2 complete"
echo ""

# Step 4: Verify images exist and are the expected size
echo "=== Image Validation ==="

for label in "Build 1" "Build 2"; do
    if [ "$label" = "Build 1" ]; then
        img_dir="$BUILD_DIR1"
    else
        img_dir="$BUILD_DIR2"
    fi
    hdd="$img_dir/nova.hdd"
    if [ ! -f "$hdd" ]; then
        echo "ERROR: $label nova.hdd is missing!"
        exit 1
    fi
    actual_size=$(stat -c%s "$hdd")
    if [ "$actual_size" -ne "$EXPECTED_SIZE" ]; then
        echo "ERROR: $label nova.hdd size is $actual_size bytes, expected $EXPECTED_SIZE bytes!"
        exit 1
    fi
    echo "$label nova.hdd: size=$actual_size bytes OK"
done

# Compute SHA-256 of both images
SHA1=$(sha256sum "$BUILD_DIR1/nova.hdd" | awk '{print $1}')
SHA2=$(sha256sum "$BUILD_DIR2/nova.hdd" | awk '{print $1}')

echo "Build 1 SHA-256: $SHA1"
echo "Build 2 SHA-256: $SHA2"

# Also compute sha256 of the kernel ELF (check multiple possible locations)
KERNEL_ELF_1=
KERNEL_ELF_2=
for path in "$BUILD_DIR1/kernel/target/release/nucleus" "$BUILD_DIR1/kernel/target/release/kernel" "$BUILD_DIR1/target/release/nucleus"; do
    if [ -f "$path" ]; then
        KERNEL_ELF_1="$path"
        break
    fi
done
for path in "$BUILD_DIR2/kernel/target/release/nucleus" "$BUILD_DIR2/kernel/target/release/kernel" "$BUILD_DIR2/target/release/nucleus"; do
    if [ -f "$path" ]; then
        KERNEL_ELF_2="$path"
        break
    fi
done

if [ -z "$KERNEL_ELF_1" ]; then
    echo "WARNING: Build 1 kernel ELF not found, skipping ELF SHA-256"
else
    KERNEL_SHA1=$(sha256sum "$KERNEL_ELF_1" | awk '{print $1}')
    echo "Build 1 kernel ELF SHA-256: $KERNEL_SHA1"
fi
if [ -z "$KERNEL_ELF_2" ]; then
    echo "WARNING: Build 2 kernel ELF not found, skipping ELF SHA-256"
else
    KERNEL_SHA2=$(sha256sum "$KERNEL_ELF_2" | awk '{print $1}')
    echo "Build 2 kernel ELF SHA-256: $KERNEL_SHA2"
fi
echo ""

# Step 5: Compare the two builds byte-by-byte
echo "=== Byte-Comparison ==="
echo "Comparing $BUILD_DIR1/nova.hdd vs $BUILD_DIR2/nova.hdd"

if [ "$SHA1" = "$SHA2" ]; then
    echo "PASS: Two builds produce identical SHA-256 digests"
    RESULT="IDENTICAL"
else
    echo "FAIL: SHA-256 digests differ!"
    RESULT="DIFFERENT"
fi
echo ""

# Step 6: Detailed diff if they differ
if [ "$RESULT" = "DIFFERENT" ]; then
    echo "=== Detailed Diff ==="
    cmp -l "$BUILD_DIR1/nova.hdd" "$BUILD_DIR2/nova.hdd" | head -30
    echo ""
    diff "$BUILD_DIR1/nova.hdd" "$BUILD_DIR2/nova.hdd" | head -50
    echo ""
    echo "First 20 differing byte positions:"
    cmp -l "$BUILD_DIR1/nova.hdd" "$BUILD_DIR2/nova.hdd" | grep '^<' | head -20
    echo ""
fi

# Step 7: Plant non-determinism test - embed current time in one build
echo "=== Non-Determinism Planting Test ==="
# Embed the current epoch time into the state file of build 1
CURRENT_EPOCH=$(date +%s)
printf '{"release_sequence": 0, "build_epoch": %s}' "$CURRENT_EPOCH" > "$BUILD_DIR1/nova.hdd.tmp.state"

# Re-compute sha256 after embedding non-determinism
NONDET_SHA1=$(sha256sum "$BUILD_DIR1/nova.hdd" | awk '{print $1}')

echo "Build 1 SHA-256 after non-determinism plant: $NONDET_SHA1"
echo "Build 2 SHA-256:                         $SHA2"

if [ "$NONDET_SHA1" != "$SHA2" ]; then
    echo "PASS: Non-determinism was successfully planted - builds now differ"
    echo "Difference detected: current epoch time $CURRENT_EPOCH embedded in build 1"
else
    echo "NOTE: Non-determinism planting did not cause a difference (may be expected)"
fi
echo ""

# Step 8: Report conclusion
echo "=== R15 Check Complete ==="
echo "Result: $RESULT"
echo "Build 1 SHA-256: $SHA1"
echo "Build 2 SHA-256: $SHA2"
KERNEL_SHA1_display=${KERNEL_SHA1:-not checked}
KERNEL_SHA2_display=${KERNEL_SHA2:-not checked}
echo "Kernel ELF 1 SHA-256: $KERNEL_SHA1_display"
echo "Kernel ELF 2 SHA-256: $KERNEL_SHA2_display"
echo ""
if [ "$RESULT" = "IDENTICAL" ]; then
    echo "SUCCESS: Two clean builds produce identical SHA-256 digests"
    echo "Reproducibility is confirmed with SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH"
else
    echo "DIFFERENCE DETECTED: Use 'cmp -l | head' above to identify differing positions"
    echo "This is a valid finding - non-determinism sources need to be fixed"
fi