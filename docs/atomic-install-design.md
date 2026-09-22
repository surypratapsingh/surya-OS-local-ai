# Atomic Install Design — C3 Specification

A/B slots for atomic updates. Failed or interrupted installs always leave a bootable machine.

---

## Problem Statement

**Current:** Single kernel/image slot. Update fails mid-installation → unbootable system.

**Solution:** Two slots (A and B). Always boot from one; update the other. Atomic pointer swap after verification.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────┐
│            NOVA Disk Layout                     │
├─────────────────────────────────────────────────┤
│                                                 │
│  ┌──────────────────┐   ┌──────────────────┐   │
│  │   Slot A         │   │   Slot B         │   │
│  │                  │   │                  │   │
│  │  nova.hdd.a      │   │  nova.hdd.b      │   │
│  │  (1 GB)          │   │  (1 GB)          │   │
│  │                  │   │                  │   │
│  │  Kernel          │   │  Kernel          │   │
│  │  Modules         │   │  Modules         │   │
│  │  RootFS          │   │  RootFS          │   │
│  └──────────────────┘   └──────────────────┘   │
│                                                 │
│  ┌──────────────────────────────────────────┐  │
│  │   Pointer Block (64 bytes)               │  │
│  │                                          │  │
│  │  Current slot:  A  (or B)                │  │
│  │  Manifest:      release-1.0.0.manifest  │  │
│  │  Sequence:      42                       │  │
│  │  Checksum:      CRC32                    │  │
│  │  (reserved for future expansion)         │  │
│  └──────────────────────────────────────────┘  │
│                                                 │
└─────────────────────────────────────────────────┘

Boot sequence:
  1. Bootloader reads pointer block
  2. Loads current slot (A or B)
  3. Kernel/modules/rootfs boot from chosen slot
```

---

## Pointer Block

A small, critical data structure that points to the active slot.

**Location:** Fixed offset on disk (e.g., sector 2048, after boot partition)  
**Size:** 64 bytes (one sector)  
**Format:** Binary structure with CRC32 protection

```c
struct PointerBlock {
    uint32_t magic;              // 0x4e4f5641 ("NOVA")
    uint8_t  active_slot;        // 0 = A, 1 = B
    uint8_t  reserved[3];        // Alignment padding
    uint32_t sequence;           // Monotonic: must be >= last sequence
    uint32_t timestamp_epoch;    // Unix timestamp of last swap
    char     manifest_path[32];  // "releases/1.0.0.manifest"
    uint32_t crc32;              // CRC32 of all above
    uint8_t  reserved2[8];       // Future expansion
};
// Total: 64 bytes
```

### Invariants

1. **Magic** must be `0x4e4f5641` (ASCII "NOVA")
2. **Active slot** must be 0 or 1 (enforced on read)
3. **Sequence** must be >= previous sequence (prevents rollback)
4. **CRC32** must validate (bit-flip detection)
5. **Manifest path** must be valid and readable

---

## Install State Machine

```
                         ┌─────────────────┐
                         │   Boot Normal   │
                         │   (Slot A or B) │
                         └────────┬────────┘
                                  │
                    (User requests install)
                                  │
                         ┌────────▼────────┐
                         │  Verify Manifest│
                         │  & New Packages │
                         └────────┬────────┘
                                  │
                         ┌────────▼────────┐
                         │ Allocate Inactive
                         │   Slot (A→B or
                         │    B→A)         │
                         └────────┬────────┘
                                  │
                         ┌────────▼────────┐
                         │  Write New Image
                         │  to Inactive    │
                         │  Slot (partial  │
                         │  OK, resume)    │
                         └────────┬────────┘
                                  │
                         ┌────────▼────────┐
                         │  Verify Written │
                         │  Checksums      │
                         └────────┬────────┘
                                  │
                         ┌────────▼────────┐
                         │  Atomic Swap    │
                         │  Pointer Block  │
                         │  (single CRC    │
                         │   update)       │
                         └────────┬────────┘
                                  │
                         ┌────────▼────────┐
                         │  Next Boot from │
                         │  New Slot       │
                         └─────────────────┘
```

---

## Atomic Pointer Swap

The critical operation: switch active slot. Must be atomic.

### Strategy: Single CRC Update

Instead of updating multiple fields and hoping the power doesn't fail, update only one: the CRC.

**Before:**
```
magic:         0x4e4f5641  ✓ (unchanged)
active_slot:   0 (A)       ✓ (unchanged)
sequence:      42          ✓ (unchanged)
timestamp:     1695398400  ✓ (unchanged)
manifest:      1.0.0       ✓ (unchanged)
crc32:         0x12345678  ← OLD CRC (no longer valid)
```

**Compute new CRC over all fields:**
```
new_crc32 = crc32(magic, active_slot, sequence, timestamp, manifest)
           = 0xabcdef00
```

**Update CRC field atomically:**
```
Write new_crc32 to pointer_block.crc32 offset
```

**After:**
```
magic:         0x4e4f5641  (unchanged)
active_slot:   0 (A)       (unchanged)
sequence:      42          (unchanged)
timestamp:     1695398400  (unchanged)
manifest:      1.0.0       (unchanged)
crc32:         0xabcdef00  ← NEW CRC (valid)
```

### Why This Works

If the write is interrupted:

**Case 1: Partial write (old CRC)**
- Bootloader reads pointer block
- CRC check fails
- Bootloader falls back to previous pointer block (or hardcoded safe default)
- Old slot boots normally

**Case 2: Partial write (new CRC)**
- Bootloader reads pointer block
- CRC check passes
- All fields valid (unchanged)
- New active_slot is read
- Boots from new slot

**Case 3: Complete write (new CRC)**
- Bootloader reads pointer block
- CRC check passes
- New active_slot is read
- Boots from new slot

### Implementation

```python
def atomic_pointer_swap(pointer_block_path, new_active_slot, new_sequence, new_manifest):
    """
    Atomically swap the active slot.
    
    Write only the CRC field to avoid partial-update corruption.
    """
    
    # Read current pointer block
    with open(pointer_block_path, "r+b") as f:
        f.seek(POINTER_BLOCK_OFFSET)
        data = f.read(64)
    
    # Parse and update fields (in memory)
    pb = PointerBlock.parse(data)
    pb.active_slot = new_active_slot
    pb.sequence = new_sequence
    pb.manifest_path = new_manifest
    pb.timestamp_epoch = int(time.time())
    
    # Compute new CRC over all fields except CRC itself
    new_crc = crc32(pb.serialize_without_crc())
    pb.crc32 = new_crc
    
    # Write only CRC field (atomic single-word write)
    with open(pointer_block_path, "r+b") as f:
        f.seek(POINTER_BLOCK_OFFSET + CRC_OFFSET)
        f.write(struct.pack('<I', new_crc))
        f.flush()
```

---

## Rollback Mechanism

If new slot fails to boot:

### Detection
- Bootloader tries to boot new slot
- Fails (kernel panic, missing module, etc.)
- Bootloader has count of boot attempts
- After N failures (e.g., 3), mark slot as bad

### Recovery

**Method 1: Manual (User)**
```bash
# On alternative machine (or USB boot)
python3 tools/rollback-release.py
# Sets pointer back to previous known-good slot
```

**Method 2: Automatic (Bootloader)**
```c
// In bootloader startup code:
if (pointer_block.active_slot == BAD_SLOT) {
    // Read backup pointer block or hardcoded default
    active_slot = ALTERNATE_SLOT;
    // Boot alternate
}
```

### Sequence Number Protection

Rollback doesn't decrement sequence. Once a slot has sequence=42, it will never accept sequence < 42.

This prevents:
- Attacker forcing revert to sequence=10 (old vulnerable release)
- Accidental downgrade via old manifest

---

## Failure Scenarios

### Scenario 1: Power Loss During Download

**State:** Image half-written to inactive slot

**Result:** ✅ Safe
- Inactive slot is corrupted (doesn't matter)
- Pointer block unchanged (still points to active slot)
- Next boot: boots active slot normally
- User can retry install

### Scenario 2: Corrupted Image After Write

**State:** Full image written, checksum verification fails

**Result:** ✅ Safe
- Install process detects checksum mismatch
- Pointer block NOT updated
- System continues to boot from active slot
- User is informed of checksum failure, retries

### Scenario 3: Power Loss During Pointer Swap

**State:** CRC write in progress

**Subcase 3a: Old CRC remains**
- Bootloader reads pointer block
- Old CRC validates ✓
- Active slot unchanged
- Boots old slot ✅

**Subcase 3b: New CRC written**
- Bootloader reads pointer block
- New CRC validates ✓
- New active slot read
- Boots new slot ✅

Either way: system boots.

### Scenario 4: New Slot Fails to Boot

**State:** Pointer swapped, but kernel panics on boot

**Options:**
1. Bootloader detects repeated failures, falls back to previous slot
2. User manually rolls back via `rollback-release.py`
3. Boot from USB with recovery environment

**Result:** ✅ Manual recovery possible

---

## Install Workflow

```bash
#!/bin/bash
# User-facing install script

# 1. User provides manifest
python3 verify-manifest.py nova-release-1.0.0.manifest

# 2. Check if install is safe (space available, etc.)
python3 tools/pre-install-check.py

# 3. Download/transfer image to inactive slot
python3 tools/install-image.py nova-release-1.0.0.manifest

# 4. Verify checksums
if ! python3 tools/verify-install.py; then
    echo "Verification failed, aborting"
    exit 1
fi

# 5. Atomic pointer swap
python3 tools/atomic-pointer-swap.py nova-release-1.0.0.manifest

# 6. Done, reboot to boot new slot
echo "Install complete. Please reboot."
```

---

## Pointer Block Backup

To protect against pointer block corruption:

### Option 1: Mirrored Pointer Block
Store two copies at different offsets. Boot from first; if corrupted, try second.

**Offset 1:** Sector 2048  
**Offset 2:** Sector 4096

If first CRC fails, read second. If both fail, fallback to hardcoded "boot Slot A".

### Option 2: Recovery Block
Store last-known-good pointer block in a reserved area.

**Offset:** Sector 8192 (after both slots)

Used only if both primary pointers are corrupted.

### Recommendation: Both

Implement both: mirrored pointers for performance (read only once usually), recovery block for catastrophic failure.

---

## Bootloader Changes (K3)

The bootloader (Limine) must support A/B slots:

1. **Read pointer block** at offset POINTER_BLOCK_OFFSET
2. **Validate CRC** (detect corruption)
3. **Check sequence** against last-known sequence (optional, if persistent storage available)
4. **Read active_slot** field
5. **Construct boot path** based on slot (e.g., `/kernel.a` or `/kernel.b`)
6. **Boot kernel** from chosen slot

**Fallback chain:**
```
Try slot A pointer → if CRC bad, try slot B pointer
→ if both bad, hardcoded fallback (Slot A)
```

---

## Performance Implications

### Install Time
- Writing image: ~30s for 1GB (5Mbps I/O typical)
- Verification: ~10s (checksum over full image)
- Pointer swap: <1ms (single sector write)
- **Total:** ~40s per install

### Boot Time
- Pointer read: <1ms
- Active slot selection: <1ms
- Boot proceeds normally: unchanged

### Disk Usage
- Each slot: 1 GB (for NOVA)
- Pointer block: 64 bytes
- **Total:** ~2 GB (one for each slot)

---

## Security Considerations

### Threat: Attacker Modifies Pointer

**Attack:** Attacker changes `active_slot` to point to older, vulnerable slot.

**Defense:** CRC32 detects any change. Bootloader rejects if CRC invalid.

**Assumption:** Attacker cannot modify CRC correctly (would require knowing current pointer block).

### Threat: Attacker Rolls Back Sequence

**Attack:** Attacker copies old slot, updates pointer to downgrade.

**Defense:** Sequence numbers prevent rollback. New slot must have `sequence > previous`.

### Threat: Both Slots Corrupted

**Status:** Unrecoverable without external recovery medium (USB).

**Mitigation:** Keep third copy on recovery USB or external backup.

---

## Integration with C2 Manifests

C2 manifests include `release_sequence` (monotonic). C3 uses this:

1. **Verify manifest** (C2 signature checks)
2. **Read manifest version** from manifest JSON
3. **Compare to pointer block sequence**
   - `manifest.sequence > pointer.sequence` → OK
   - `manifest.sequence <= pointer.sequence` → Reject (rollback attempt)
4. **If OK, update pointer block** with new sequence

This ties C2 manifests to C3 install logic: manifests control version, pointer block enforces monotonicity.

---

## Files Written

### Bootloader (K3)
- `kernel/limine.conf` — Boot config with A/B slot paths
- `kernel/src/boot.rs` — Pointer block reading and validation
- `kernel/src/pointer_block.rs` — Data structures and CRC

### Userspace Tools
- `tools/atomic-pointer-swap.py` — Atomic CRC update
- `tools/install-image.py` — Write image to inactive slot
- `tools/verify-install.py` — Checksum verification
- `tools/rollback-release.py` — Manual rollback
- `tools/pre-install-check.py` — Space/manifest checks
- `tools/test_atomic_install.py` — Test suite

### Documentation
- `docs/atomic-install-design.md` — This file
- `docs/work-order-c3-completion.md` — Completion report

---

## Next Steps (C4–C6)

**C4 (Extend signing):** Will include Limine and kernel hashes in manifests.

**C5 (Reproducible builds):** Will ensure same inputs → same hashes, so updates are deterministic.

**C6 (Secure Boot):** Will cryptographically verify pointer block and slots at hardware level.

---

## Testing Strategy

Test matrix (all scenarios):

| Scenario | Status | Recovery |
|----------|--------|----------|
| Normal install | ✓ Boot new | Power off OK |
| Partial download | ✓ Resume | Retry install |
| Bad checksum | ✓ Reject | Retry install |
| Power loss during swap | ✓ Boot old or new | Auto-recover |
| Corrupted new slot | ✓ Fallback | Manual rollback |
| Corrupted pointer | ✓ Fallback | Recover block |
| Both slots corrupted | ✗ Unbootable | USB recovery |

"✓" = system boots without manual intervention.

All failures leave a bootable machine or provide clear recovery path.

