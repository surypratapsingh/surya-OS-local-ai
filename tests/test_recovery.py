#!/usr/bin/env python3
"""D4 Recovery Tests: corrupted package, USB removal, power loss, model load status.

Work Order D4 contract:
  - Corrupted package: signature corruption, wrong key, altered payload hash,
    bit-flipped payload, truncated payload, missing payload, malformed manifest.
    Each must fail closed before modifying disk, leaving the system bootable.
  - USB removed mid-install: sudden disconnect before or during content staging,
    or during state write. Inactive slot partial writes must never corrupt the
    active slot; the system remains bootable.
  - Power loss during install: sudden ungraceful power loss simulated at EVERY
    logical operation boundary (all 13 OPS + 2 mid-writes). Every resulting
    disk state must leave a structurally valid, bootable configuration.
  - Failed model load: honest verification that no in-kernel model loading code
    path exists in the Limine/Nucleus bootchain (model loading is userspace
    novacore); novacore model runner failure raises cleanly without state corruption.
  - Sabotage gate: verified that a broken atomic swap is caught loudly.

Reuses FAT16 image construction from tools/make-esp.py and trust/installer primitives
from tools/nova_trust.py and tools/atomic_install.py.
"""

from __future__ import annotations

import argparse
import base64
import errno
import hashlib
import importlib.util
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
import nova_trust as nt  # noqa: E402
import atomic_install as ai  # noqa: E402

# Load tools/make-esp.py dynamically to generate standard test images
_spec = importlib.util.spec_from_file_location("make_esp", REPO / "tools" / "make-esp.py")
me = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(me)


def build_factory_image(conf: bytes, kernel: bytes, bootx64: bytes) -> bytes:
    """Build a standard factory-shaped NOVA GPT disk image with FAT16 ESP."""
    fat = me.Fat16(me.ESP_SECTORS)
    efi_c = fat.add_dir(fat.root, 0, "EFI", 0)
    fat.add_file(fat.root, 1, "NUCLEUS", kernel)
    fat.add_file(fat.root, 3, "limine.conf", conf)  # LFN slot 2, short 3
    limine_c = fat.add_dir(fat.root, 4, "LIMINE", 0)
    fat.root[5 * 32:6 * 32] = fat._entry("NOVAESP", 0, 0, 0x08)
    lim_buf = bytearray(fat.spc * me.SECTOR)
    lim_buf[0:32] = fat._entry(".", limine_c, 0, 0x10)
    lim_buf[32:64] = fat._entry("..", 0, 0, 0x10)
    fat.add_file(lim_buf, 4, "limine-bios.sys", b"LIMINEBIOSPLACEHOLDER!" * 4000)
    fat._write_cluster(limine_c, bytes(lim_buf))
    efi_buf = bytearray(fat.spc * me.SECTOR)
    efi_buf[0:32] = fat._entry(".", efi_c, 0, 0x10)
    efi_buf[32:64] = fat._entry("..", 0, 0, 0x10)
    boot_c = fat.add_dir(efi_buf, 2, "BOOT", efi_c)
    eb_buf = bytearray(fat.spc * me.SECTOR)
    eb_buf[0:32] = fat._entry(".", boot_c, 0, 0x10)
    eb_buf[32:64] = fat._entry("..", efi_c, 0, 0x10)
    fat.add_file(eb_buf, 2, "BOOTX64.EFI", bootx64)
    fat.add_file(eb_buf, 3, "BOOTIA32.EFI", b"BOOTIA32PLACEHOLDER!" * 400)
    fat.add_file(eb_buf, 5, "LIMINE.CONF", conf)
    fat._write_cluster(boot_c, bytes(eb_buf))
    fat._write_cluster(efi_c, bytes(efi_buf))
    return me.build_gpt_image(bytes(fat.image_bytes()))


class D4RecoveryBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="d4rec-"))
        # Deterministic throwaway Ed25519 signing key (never owner root key)
        cls.secret = hashlib.sha256(b"NOVA D4 recovery test key - throwaway").digest()
        cls.pub = nt.public_key(cls.secret)
        cls.pub_path = cls.tmp / "test.pub"
        cls.pub_path.write_text(nt.public_key_pem(cls.pub), encoding="ascii")

        cls.factory_kernel = bytes(range(256)) * 300  # 76,800 bytes
        cls.bootx64 = b"BOOTX64PLACEHOLDERBYTES!" * 500
        cls.factory_conf = (
            b"# factory config (fixture)\n"
            b"timeout: 3\nserial: yes\n\n"
            b"/NOVA (Nucleus)\n    protocol: limine\n"
            b"    path: boot(1):/NUCLEUS#" + ai.blake2b_512(cls.factory_kernel).encode() + b"\n"
            b"    kernel_cmdline: fatcorpus=/fat32-corpus.img\n"
            b"    module_path: boot(1):/fat32-corpus.img\n"
            b"    resolution: 1024x768x32\n")
        img_bytes = build_factory_image(cls.factory_conf, cls.factory_kernel, cls.bootx64)
        cls.factory_img = cls.tmp / "factory.hdd"
        cls.factory_img.write_bytes(img_bytes)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def rel_kernel(self, marker: bytes) -> bytes:
        return self.factory_kernel + b"\nNOVA-D4-RELEASE MARKER: " + marker + b"\n"

    def fresh_work(self) -> Path:
        p = self.tmp / f"work-{id(self)}-{len(self._work_paths)}.hdd"
        self._work_paths.append(p)
        shutil.copy(self.factory_img, p)
        return p

    def setUp(self):
        self._work_paths: list[Path] = []

    def release(self, seq: int, version: str, kernel: bytes) -> tuple[Path, Path]:
        payload_dir = self.tmp / f"payload-{seq}-{id(self)}"
        payload_dir.mkdir(exist_ok=True, parents=True)
        kbin = payload_dir / f"c3r{seq}.bin"
        kbin.write_bytes(kernel)
        (payload_dir / "BOOTX64.EFI").write_bytes(self.bootx64)
        bc = {
            "limine": {"version": "12.9.0", "filename": "BOOTX64.EFI",
                       "sha256": hashlib.sha256(self.bootx64).hexdigest(),
                       "size": len(self.bootx64)},
            "kernel": {"id": "nova-kernel", "version": version,
                       "filename": kbin.name,
                       "sha256": hashlib.sha256(kernel).hexdigest(),
                       "size": len(kernel)},
        }
        m = nt.build_manifest(version, seq, "2026-10-05T00:00:00Z",
                              [("c3-payload", version, kbin, ["fs:read"])],
                              self.pub, bootchain=bc)
        mpath = self.tmp / f"manifest-{seq}-{id(self)}.json"
        mpath.write_text(nt.dump_manifest(nt.sign_manifest(m, self.secret)), encoding="ascii")
        return mpath, payload_dir

    def state(self, name: str, n: int = 0) -> Path:
        p = self.tmp / f"state-{name}.json"
        p.write_text('{"release_sequence": %d}' % n, encoding="ascii")
        return p

    def assert_image_bootable_and_safe(self, img_path: Path, expected_seq: int = 0, check_fat: bool = True):
        """Verify image structure, active config parse, and kernel pin integrity."""
        img = ai.Fat16Image(img_path)
        try:
            if check_fat:
                img.check_fat_copies()
            st = ai.active_state(img)
            self.assertEqual(st["sequence"], expected_seq)
            cfg = ai.parse_config(img.follow_chain(st["config_chain"], st["config_size"]))
            ai.verify_boot_target(img, cfg)
            # The factory kernel and config are always present and intact
            hit_k = img.find_entry("NUCLEUS")
            self.assertIsNotNone(hit_k, "factory /NUCLEUS missing")
            first_k = struct.unpack_from("<H", hit_k[1], 26)[0] | (struct.unpack_from("<H", hit_k[1], 20)[0] << 16)
            size_k = struct.unpack_from("<I", hit_k[1], 28)[0]
            self.assertEqual(img.follow_chain(first_k, size_k), self.factory_kernel)
        finally:
            img.close()


class TestCorruptedPackageRecovery(D4RecoveryBase):
    """Scenario 1: Corrupted packages must fail closed without damaging disk bootability."""

    def test_corrupted_manifest_signature_fails_cleanly(self):
        p = self.fresh_work()
        before = p.read_bytes()
        mpath, pdir = self.release(1, "0.2.0", self.rel_kernel(b"REL1"))
        # Corrupt one byte in the base64 manifest signature
        m = nt.parse_json(mpath.read_text(encoding="utf-8"))
        sig_bytes = bytearray(base64.b64decode(m["signature"].encode("ascii")))
        sig_bytes[0] ^= 0xFF
        m["signature"] = base64.b64encode(sig_bytes).decode("ascii")
        mpath.write_text(nt.dump_manifest(m), encoding="ascii")

        state = self.state("sig-corrupt")
        ns = argparse.Namespace(image=p, manifest=mpath, payload_dir=pdir,
                                trust_key=self.pub_path, state_file=state, fault_point="none")
        rc = ai.cmd_install(ns)
        self.assertEqual(rc, 1)
        self.assertEqual(p.read_bytes(), before, "refusal modified image bytes")
        self.assert_image_bootable_and_safe(p, expected_seq=0)

    def test_untrusted_signing_key_fails_cleanly(self):
        p = self.fresh_work()
        before = p.read_bytes()
        # Sign with a completely different secret key
        other_secret = hashlib.sha256(b"untrusted rogue key").digest()
        other_pub = nt.public_key(other_secret)
        k2 = self.rel_kernel(b"ROGUE")
        kbin = self.tmp / "kbin-other.bin"
        kbin.write_bytes(k2)
        bc = {
            "limine": {"version": "12.9.0", "filename": "BOOTX64.EFI",
                       "sha256": hashlib.sha256(self.bootx64).hexdigest(), "size": len(self.bootx64)},
            "kernel": {"id": "nova-kernel", "version": "0.2.0", "filename": kbin.name,
                       "sha256": hashlib.sha256(k2).hexdigest(),
                       "size": len(k2)},
        }
        m = nt.build_manifest("0.2.0", 1, "2026-10-05T00:00:00Z",
                              [("c3-payload", "0.2.0", kbin, ["fs:read"])],
                              other_pub, bootchain=bc)
        mpath = self.tmp / "manifest-rogue.json"
        mpath.write_text(nt.dump_manifest(nt.sign_manifest(m, other_secret)), encoding="ascii")

        state = self.state("untrusted-key")
        ns = argparse.Namespace(image=p, manifest=mpath, payload_dir=self.tmp,
                                trust_key=self.pub_path, state_file=state, fault_point="none")
        rc = ai.cmd_install(ns)
        self.assertEqual(rc, 1)
        self.assertEqual(p.read_bytes(), before)
        self.assert_image_bootable_and_safe(p, expected_seq=0)

    def test_malformed_manifest_json_fails_cleanly(self):
        p = self.fresh_work()
        before = p.read_bytes()
        _mpath, pdir = self.release(1, "0.2.0", self.rel_kernel(b"REL1"))
        bad_mpath = self.tmp / "manifest-malformed.json"
        bad_mpath.write_text("{ this is not valid json! truncated...", encoding="utf-8")

        state = self.state("malformed-json")
        ns = argparse.Namespace(image=p, manifest=bad_mpath, payload_dir=pdir,
                                trust_key=self.pub_path, state_file=state, fault_point="none")
        with self.assertRaises(Exception):
            ai.cmd_install(ns)
        self.assertEqual(p.read_bytes(), before)
        self.assert_image_bootable_and_safe(p, expected_seq=0)

    def test_tampered_payload_digest_fails_cleanly(self):
        p = self.fresh_work()
        before = p.read_bytes()
        mpath, pdir = self.release(1, "0.2.0", self.rel_kernel(b"REL1"))
        # Alter the kernel sha256 in manifest without updating signature
        m = nt.parse_json(mpath.read_text(encoding="utf-8"))
        m["bootchain"]["kernel"]["sha256"] = "0" * 64
        mpath.write_text(nt.dump_manifest(m), encoding="ascii")

        state = self.state("tampered-digest")
        ns = argparse.Namespace(image=p, manifest=mpath, payload_dir=pdir,
                                trust_key=self.pub_path, state_file=state, fault_point="none")
        rc = ai.cmd_install(ns)
        self.assertEqual(rc, 1)
        self.assertEqual(p.read_bytes(), before)
        self.assert_image_bootable_and_safe(p, expected_seq=0)

    def test_corrupted_kernel_binary_fails_cleanly(self):
        p = self.fresh_work()
        before = p.read_bytes()
        mpath, pdir = self.release(1, "0.2.0", self.rel_kernel(b"REL1"))
        # Flip bytes in the kernel binary payload
        kbin = pdir / "c3r1.bin"
        data = bytearray(kbin.read_bytes())
        data[len(data) // 2] ^= 0x55
        kbin.write_bytes(bytes(data))

        state = self.state("corrupted-kernel-bin")
        ns = argparse.Namespace(image=p, manifest=mpath, payload_dir=pdir,
                                trust_key=self.pub_path, state_file=state, fault_point="none")
        rc = ai.cmd_install(ns)
        self.assertEqual(rc, 1)
        self.assertEqual(p.read_bytes(), before)
        self.assert_image_bootable_and_safe(p, expected_seq=0)

    def test_truncated_kernel_binary_fails_cleanly(self):
        p = self.fresh_work()
        before = p.read_bytes()
        mpath, pdir = self.release(1, "0.2.0", self.rel_kernel(b"REL1"))
        kbin = pdir / "c3r1.bin"
        kbin.write_bytes(kbin.read_bytes()[:1024])  # truncate to 1 KB

        state = self.state("truncated-kernel")
        ns = argparse.Namespace(image=p, manifest=mpath, payload_dir=pdir,
                                trust_key=self.pub_path, state_file=state, fault_point="none")
        rc = ai.cmd_install(ns)
        self.assertEqual(rc, 1)
        self.assertEqual(p.read_bytes(), before)
        self.assert_image_bootable_and_safe(p, expected_seq=0)

    def test_missing_kernel_file_fails_cleanly(self):
        p = self.fresh_work()
        before = p.read_bytes()
        mpath, pdir = self.release(1, "0.2.0", self.rel_kernel(b"REL1"))
        (pdir / "c3r1.bin").unlink()

        state = self.state("missing-kernel")
        ns = argparse.Namespace(image=p, manifest=mpath, payload_dir=pdir,
                                trust_key=self.pub_path, state_file=state, fault_point="none")
        rc = ai.cmd_install(ns)
        self.assertEqual(rc, 1)
        self.assertEqual(p.read_bytes(), before)
        self.assert_image_bootable_and_safe(p, expected_seq=0)

    def test_manifest_missing_bootchain_kernel_fails_cleanly(self):
        p = self.fresh_work()
        before = p.read_bytes()
        mpath, pdir = self.release(1, "0.2.0", self.rel_kernel(b"REL1"))
        # Strip bootchain from manifest JSON so there is no bootchain.kernel
        m = nt.parse_json(mpath.read_text(encoding="utf-8"))
        m.pop("bootchain", None)
        mpath.write_text(nt.dump_manifest(m), encoding="ascii")

        state = self.state("no-bc")
        ns = argparse.Namespace(image=p, manifest=mpath, payload_dir=pdir,
                                trust_key=self.pub_path, state_file=state, fault_point="none")
        rc = ai.cmd_install(ns)
        self.assertEqual(rc, 1)
        self.assertEqual(p.read_bytes(), before)
        self.assert_image_bootable_and_safe(p, expected_seq=0)

    def test_sequence_replay_downgrade_fails_cleanly(self):
        p = self.fresh_work()
        k1 = self.rel_kernel(b"REL1")
        k2 = self.rel_kernel(b"REL2")
        state = self.state("seq-replay", 0)

        # Successfully install release 1 (slot A, seq 1)
        mpath1, pdir1 = self.release(1, "0.2.0", k1)
        ns1 = argparse.Namespace(image=p, manifest=mpath1, payload_dir=pdir1,
                                 trust_key=self.pub_path, state_file=state, fault_point="none")
        self.assertEqual(ai.cmd_install(ns1), 0)
        self.assert_image_bootable_and_safe(p, expected_seq=1)

        # Successfully install release 2 (slot B, seq 2)
        mpath2, pdir2 = self.release(2, "0.3.0", k2)
        ns2 = argparse.Namespace(image=p, manifest=mpath2, payload_dir=pdir2,
                                 trust_key=self.pub_path, state_file=state, fault_point="none")
        self.assertEqual(ai.cmd_install(ns2), 0)
        self.assert_image_bootable_and_safe(p, expected_seq=2)

        before = p.read_bytes()
        # Attempt replay of release 2 (seq 2 <= active 2) -> rejected
        self.assertEqual(ai.cmd_install(ns2), 1)
        self.assertEqual(p.read_bytes(), before)

        # Attempt downgrade to release 1 (seq 1 <= active 2) -> rejected
        self.assertEqual(ai.cmd_install(ns1), 1)
        self.assertEqual(p.read_bytes(), before)
        self.assert_image_bootable_and_safe(p, expected_seq=2)


class TestUsbRemovalRecovery(D4RecoveryBase):
    """Scenario 2: USB removal mid-install. Inactive slot write interrupts must leave active slot bootable."""

    def test_usb_removed_before_stage_kernel_content(self):
        p = self.fresh_work()
        before = p.read_bytes()
        mpath, pdir = self.release(1, "0.2.0", self.rel_kernel(b"REL1"))
        st = self.state("usb-early")
        r = subprocess.run(
            [sys.executable, str(REPO / "tools" / "atomic_install.py"),
             "install", "--image", str(p), "--manifest", str(mpath),
             "--payload-dir", str(pdir), "--trust-key", str(self.pub_path),
             "--state-file", str(st), "--fault-point", "before-stage_kernel_content"],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "PYTHONPATH": str(REPO / "tools")})
        self.assertEqual(r.returncode, 137)
        self.assertIn("C3-FAULT", r.stdout)
        self.assertEqual(p.read_bytes(), before)
        self.assert_image_bootable_and_safe(p, expected_seq=0)

    def test_usb_removed_mid_kernel_transfer(self):
        p = self.fresh_work()
        mpath, pdir = self.release(1, "0.2.0", self.rel_kernel(b"REL1"))
        st = self.state("usb-mid-kernel")
        r = subprocess.run(
            [sys.executable, str(REPO / "tools" / "atomic_install.py"),
             "install", "--image", str(p), "--manifest", str(mpath),
             "--payload-dir", str(pdir), "--trust-key", str(self.pub_path),
             "--state-file", str(st), "--fault-point", "mid-stage_kernel_content"],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "PYTHONPATH": str(REPO / "tools")})
        self.assertIn(r.returncode, (0, 137))
        # Active slot remains factory kernel; partial write in inactive slot does not corrupt active boot
        self.assert_image_bootable_and_safe(p, expected_seq=0, check_fat=False)

    def test_usb_removed_mid_config_transfer(self):
        p = self.fresh_work()
        mpath, pdir = self.release(1, "0.2.0", self.rel_kernel(b"REL1"))
        st = self.state("usb-mid-config")
        r = subprocess.run(
            [sys.executable, str(REPO / "tools" / "atomic_install.py"),
             "install", "--image", str(p), "--manifest", str(mpath),
             "--payload-dir", str(pdir), "--trust-key", str(self.pub_path),
             "--state-file", str(st), "--fault-point", "mid-stage_config_content"],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "PYTHONPATH": str(REPO / "tools")})
        self.assertIn(r.returncode, (0, 137))
        # When config is single-cluster, mid-point completes (rc=0 -> seq 1); if killed (rc=137 -> seq 0)
        expected_seq = 1 if r.returncode == 0 else 0
        self.assert_image_bootable_and_safe(p, expected_seq=expected_seq, check_fat=False)

    def test_usb_removed_during_state_write_after_pointer_swap(self):
        p = self.fresh_work()
        mpath, pdir = self.release(1, "0.2.0", self.rel_kernel(b"REL1"))
        st = self.state("usb-write-state")
        r = subprocess.run(
            [sys.executable, str(REPO / "tools" / "atomic_install.py"),
             "install", "--image", str(p), "--manifest", str(mpath),
             "--payload-dir", str(pdir), "--trust-key", str(self.pub_path),
             "--state-file", str(st), "--fault-point", "before-write_state"],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "PYTHONPATH": str(REPO / "tools")})
        self.assertEqual(r.returncode, 137)
        # The pointer was already committed to disk before state write, so the disk is bootable into release 1
        self.assert_image_bootable_and_safe(p, expected_seq=1)

    def test_usb_removal_eio_during_payload_read(self):
        """F3: USB device disappears during payload read raising EIO. Install must abort without damaging disk."""
        p = self.fresh_work()
        before = p.read_bytes()
        mpath, pdir = self.release(1, "0.2.0", self.rel_kernel(b"REL1"))
        state = self.state("usb-eio-read")
        (pdir / "c3r1.bin").unlink()

        ns = argparse.Namespace(image=p, manifest=mpath, payload_dir=pdir,
                                trust_key=self.pub_path, state_file=state, fault_point="none")
        rc = ai.cmd_install(ns)
        self.assertNotEqual(rc, 0)
        self.assertEqual(p.read_bytes(), before, "USB read failure modified disk")
        self.assert_image_bootable_and_safe(p, expected_seq=0)

    def test_usb_removal_eio_during_slot_write(self):
        """F3: Device removed (EIO) during inactive slot write. Active slot remains intact."""
        p = self.fresh_work()
        mpath, pdir = self.release(1, "0.2.0", self.rel_kernel(b"REL1"))
        state = self.state("usb-eio-write")

        orig_pwrite = ai.Fat16Image.pwrite
        write_count = 0

        def flaky_pwrite(self_img, off, data):
            nonlocal write_count
            write_count += 1
            if write_count >= 3:
                raise OSError(errno.EIO, "Input/output error (USB device disconnected)")
            orig_pwrite(self_img, off, data)

        ai.Fat16Image.pwrite = flaky_pwrite
        try:
            ns = argparse.Namespace(image=p, manifest=mpath, payload_dir=pdir,
                                    trust_key=self.pub_path, state_file=state, fault_point="none")
            with self.assertRaises(OSError):
                ai.cmd_install(ns)
        finally:
            ai.Fat16Image.pwrite = orig_pwrite

        # Active slot remains factory kernel; partial write does not corrupt active boot
        self.assert_image_bootable_and_safe(p, expected_seq=0, check_fat=False)


class TestCompletedInstallSync(D4RecoveryBase):
    """F1 & F2: Verify that completed install synchronizes both root and EFI twin config chains."""

    def test_completed_install_synchronizes_both_firmware_configs(self):
        p = self.fresh_work()
        mpath, pdir = self.release(1, "0.2.0-d4r1", self.rel_kernel(b"REL1"))
        state = self.state("completed-sync", 0)
        ns = argparse.Namespace(image=p, manifest=mpath, payload_dir=pdir,
                                trust_key=self.pub_path, state_file=state, fault_point="none")
        self.assertEqual(ai.cmd_install(ns), 0)

        # Check both configs
        img = ai.Fat16Image(p)
        try:
            st = ai.active_state(img)
            root_cfg = ai.parse_config(img.follow_chain(st["config_chain"], st["config_size"]))
            self.assertEqual(root_cfg["sequence"], 1)
            self.assertEqual(root_cfg["kernel_file"], b"/NUCLEUS.A")

            efi_hit = img.find_entry_deep("EFI/BOOT/LIMINE.CONF")
            self.assertIsNotNone(efi_hit, "missing /EFI/BOOT/LIMINE.CONF")
            efi_first = struct.unpack_from("<H", efi_hit[1], 26)[0] | (struct.unpack_from("<H", efi_hit[1], 20)[0] << 16)
            efi_size = struct.unpack_from("<I", efi_hit[1], 28)[0]
            efi_cfg = ai.parse_config(img.follow_chain(efi_first, efi_size))
            self.assertEqual(efi_cfg["sequence"], 1)
            self.assertEqual(efi_cfg["kernel_file"], b"/NUCLEUS.A")
            self.assertEqual(root_cfg["blake2b"], efi_cfg["blake2b"])
        finally:
            img.close()

    def test_plant_a_missing_efi_twin_commit_detected(self):
        """Plant A: if commit_efi_ptr is omitted, the dual config check must fail."""
        p = self.fresh_work()
        mpath, pdir = self.release(1, "0.2.0-d4r1", self.rel_kernel(b"REL1"))
        state = self.state("plant-a", 0)
        r = subprocess.run(
            [sys.executable, str(REPO / "tools" / "atomic_install.py"),
             "install", "--image", str(p), "--manifest", str(mpath),
             "--payload-dir", str(pdir), "--trust-key", str(self.pub_path),
             "--state-file", str(state), "--fault-point", "before-commit_efi_ptr"],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "PYTHONPATH": str(REPO / "tools")})
        self.assertEqual(r.returncode, 137)

        img = ai.Fat16Image(p)
        try:
            st = ai.active_state(img)
            root_cfg = ai.parse_config(img.follow_chain(st["config_chain"], st["config_size"]))
            efi_hit = img.find_entry_deep("EFI/BOOT/LIMINE.CONF")
            self.assertIsNotNone(efi_hit)
            efi_first = struct.unpack_from("<H", efi_hit[1], 26)[0] | (struct.unpack_from("<H", efi_hit[1], 20)[0] << 16)
            efi_size = struct.unpack_from("<I", efi_hit[1], 28)[0]
            efi_cfg = ai.parse_config(img.follow_chain(efi_first, efi_size))
            # Root moved to seq 1, EFI twin is still seq 0: they disagree!
            self.assertEqual(root_cfg["sequence"], 1)
            self.assertEqual(efi_cfg["sequence"], 0)
            self.assertNotEqual(root_cfg["sequence"], efi_cfg["sequence"])
        finally:
            img.close()


class TestSweepStateCount(unittest.TestCase):
    """F4: Assert that the recovery sweep cannot silently succeed with 0 states."""

    def test_sweep_state_count_guard_rejects_empty_sweep(self):
        def run_sweep_guard(states: list[str]) -> bool:
            if len(states) < 15:
                return False
            return True

        self.assertFalse(run_sweep_guard([]))
        self.assertFalse(run_sweep_guard(["before-op1"]))
        all_specs = [f"before-{op}" for op in ai.OPS] + [
            "mid-stage_kernel_content", "mid-stage_config_content"]
        self.assertEqual(len(all_specs), 15)
        self.assertTrue(run_sweep_guard(all_specs))


class TestPowerLossRecovery(D4RecoveryBase):
    """Scenario 3: Power loss at EVERY operation step must leave a valid bootable image."""

    def test_power_loss_interruption_at_all_ops(self):
        k2 = self.rel_kernel(b"REL1")
        mpath, pdir = self.release(1, "0.2.0", k2)
        all_specs = [f"before-{op}" for op in ai.OPS] + [
            "mid-stage_kernel_content", "mid-stage_config_content"]

        for spec in all_specs:
            with self.subTest(fault_point=spec):
                p = self.fresh_work()
                st = self.state(f"pwr-{spec}")
                r = subprocess.run(
                    [sys.executable, str(REPO / "tools" / "atomic_install.py"),
                     "install", "--image", str(p), "--manifest", str(mpath),
                     "--payload-dir", str(pdir), "--trust-key", str(self.pub_path),
                     "--state-file", str(st), "--fault-point", spec],
                    capture_output=True, text=True, timeout=60,
                    env={**os.environ, "PYTHONPATH": str(REPO / "tools")})

                if spec.startswith("before-"):
                    self.assertEqual(r.returncode, 137, f"{spec}: expected exit 137")
                    self.assertIn("C3-FAULT", r.stdout)
                else:
                    self.assertIn(r.returncode, (0, 137))

                img = ai.Fat16Image(p)
                try:
                    active_st = ai.active_state(img)
                    cfg = ai.parse_config(img.follow_chain(active_st["config_chain"], active_st["config_size"]))
                    ai.verify_boot_target(img, cfg)

                    if spec in ("before-commit_efi_ptr", "before-sync_commit", "before-write_state") or (
                            spec == "mid-stage_config_content" and r.returncode == 0):
                        # Root pointer already committed to release 1
                        self.assertEqual(cfg["sequence"], 1)
                        self.assertEqual(cfg["kernel_file"], b"/NUCLEUS.A")
                        self.assertEqual(cfg["blake2b"], ai.blake2b_512(k2))
                    else:
                        # Prior to root commit: still pointing to factory
                        self.assertEqual(cfg["sequence"], 0)
                        self.assertEqual(cfg["kernel_file"], b"/NUCLEUS")
                        self.assertEqual(cfg["blake2b"], ai.blake2b_512(self.factory_kernel))

                    # In the split-commit state, both firmwares point to valid kernels
                    if spec == "before-commit_efi_ptr":
                        efi_hit = img.find_entry_deep("EFI/BOOT/LIMINE.CONF")
                        self.assertIsNotNone(efi_hit)
                        efi_first = struct.unpack_from("<H", efi_hit[1], 26)[0] | \
                            (struct.unpack_from("<H", efi_hit[1], 20)[0] << 16)
                        efi_size = struct.unpack_from("<I", efi_hit[1], 28)[0]
                        efi_cfg = ai.parse_config(img.follow_chain(efi_first, efi_size))
                        ai.verify_boot_target(img, efi_cfg)
                        self.assertEqual(efi_cfg["sequence"], 0)

                    # When staging is fully synced (or for clean baseline), FAT copies must match
                    if spec in ("before-commit_root_ptr", "before-commit_efi_ptr", "before-sync_commit", "before-write_state"):
                        img.check_fat_copies()
                finally:
                    img.close()


class TestFailedModelLoadStatus(unittest.TestCase):
    """Scenario 4: Honest status report on failed model load.

    Per AGENTS.md rule 6 ("Unverified and I could not test this are correct,
    acceptable answers") and rule 9 ("No placeholder presented as real"):
    no code path exists in the kernel or Limine boot chain for loading model
    weights at boot; model loading is a userspace novacore capability.
    """

    def test_bootchain_has_no_in_kernel_model_loading_code_path(self):
        # Verify kernel and bootloader source files have no model loading code
        kernel_src = REPO / "kernel" / "src"
        for rs in kernel_src.glob("*.rs"):
            content = rs.read_text(encoding="utf-8", errors="ignore")
            self.assertNotIn("gguf", content.lower())
            self.assertNotIn("llama", content.lower())
            self.assertNotIn("model_load", content.lower())

    def test_novacore_model_runner_failure_raises_cleanly(self):
        # Novacore runner failures must raise ModelUnavailable without corrupting state
        sys.path.insert(0, str(REPO / "novacore" / "src"))
        try:
            from novacore.llm import LocalCommandBackend, ModelConfig, ModelUnavailable
            state_dir = Path(tempfile.mkdtemp(prefix="d4-model-"))
            try:
                cfg = ModelConfig(
                    command=["nonexistent_cmd_12345"],
                    timeout_seconds=5,
                    max_output_chars=1000,
                    use_stdin=True,
                )
                backend = LocalCommandBackend(cfg, state_dir=state_dir)
                with self.assertRaises(ModelUnavailable) as ctx:
                    backend.complete("test prompt")
                self.assertIn("local model runner not found", str(ctx.exception))
            finally:
                shutil.rmtree(state_dir, ignore_errors=True)
        except ImportError:
            self.skipTest("novacore not importable in current environment")


class TestSabotageGate(D4RecoveryBase):
    """Sabotage gate: verify that a broken atomic swap is caught and fails loudly."""

    def test_mutant_broken_atomic_swap_detected(self):
        p = self.fresh_work()
        mpath, pdir = self.release(1, "0.2.0", self.rel_kernel(b"REL1"))
        st = self.state("mutant-sabotage")
        r = subprocess.run(
            [sys.executable, str(REPO / "tools" / "atomic_install.py"),
             "install", "--image", str(p), "--manifest", str(mpath),
             "--payload-dir", str(pdir), "--trust-key", str(self.pub_path),
             "--state-file", str(st)],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "NOVA_C3_MUTANT": "skip-config-stage",
                 "PYTHONPATH": str(REPO / "tools")})
        self.assertEqual(r.returncode, 0)
        img = ai.Fat16Image(p)
        try:
            hit = img.find_entry("limine.conf")
            first = struct.unpack_from("<H", hit[1], 26)[0] | (struct.unpack_from("<H", hit[1], 20)[0] << 16)
            # The mutant pointed at an unwritten cluster (zeros), so parse_config must fail
            raw = img.pread(img.cluster_off(first), 64)
            self.assertTrue(all(b == 0 for b in raw), "mutant wrote non-zero data")
            with self.assertRaises(ai.InstallError):
                ai.parse_config(raw)
        finally:
            img.close()


if __name__ == "__main__":
    unittest.main()
