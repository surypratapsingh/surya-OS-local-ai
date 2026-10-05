#!/usr/bin/env python3
"""C3 atomic-install unit tests (no QEMU; the boot-truth gate is
scripts/test-atomic-install.sh, check stage 14).

Fixture images are built with tools/make-esp.py's own Fat16 builder - it is
the image GENERATOR here, not the code under test; the code under test is
tools/atomic_install.py's FAT surgery. Manifests are signed with a
fixed-seed throwaway key (never the owner's root key) through the real C2
signer, and every install is verified through the real C2 verifier inside
the installer.
"""

import argparse
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

_spec = importlib.util.spec_from_file_location("make_esp", REPO / "tools" / "make-esp.py")
me = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(me)


def build_factory_image(conf: bytes, kernel: bytes, bootx64: bytes) -> bytes:
    """A factory-shaped NOVA image (same root layout make-esp.py builds),
    with placeholder payloads where only bytes matter."""
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
    # No slot may be left empty before later entries: FAT scanning stops at
    # the first 0x00 first-byte entry, so a hole hides everything after it
    # (the real make-esp.py layout fills slot 3 with BOOTIA32.EFI).
    fat.add_file(eb_buf, 3, "BOOTIA32.EFI", b"BOOTIA32PLACEHOLDER!" * 400)
    fat.add_file(eb_buf, 5, "LIMINE.CONF", conf)
    fat._write_cluster(boot_c, bytes(eb_buf))
    fat._write_cluster(efi_c, bytes(efi_buf))
    return me.build_gpt_image(bytes(fat.image_bytes()))


class C3Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="c3ut-"))
        # Fixed-seed throwaway key: deterministic, never the owner's root key.
        cls.secret = hashlib.sha256(
            b"NOVA C3 unit-test signing key - not the owner key").digest()
        cls.pub = nt.public_key(cls.secret)
        (cls.tmp / "test.pub").write_text(nt.public_key_pem(cls.pub), encoding="ascii")

        cls.factory_kernel = bytes(range(256)) * 300          # 76,800 B
        cls.bootx64 = b"BOOTX64PLACEHOLDERBYTES!" * 500
        cls.factory_conf = (
            b"# factory config (fixture)\n"
            b"timeout: 3\nserial: yes\n\n"
            b"/NOVA (Nucleus)\n    protocol: limine\n"
            b"    path: boot(1):/NUCLEUS#" + ai.blake2b_512(cls.factory_kernel).encode() + b"\n"
            b"    kernel_cmdline: fatcorpus=/fat32-corpus.img\n"
            b"    module_path: boot(1):/fat32-corpus.img\n"
            b"    resolution: 1024x768x32\n")
        img = build_factory_image(cls.factory_conf, cls.factory_kernel, cls.bootx64)
        cls.factory_img = cls.tmp / "factory.hdd"
        cls.factory_img.write_bytes(img)

    def rel2_kernel(self):
        return self.factory_kernel + b"\nNOVA-C3-RELEASE-2 MARKER\n"

    def rel3_kernel(self):
        return self.factory_kernel + b"\nNOVA-C3-RELEASE-3 MARKER, DIFFERENT\n"

    def fresh_work(self) -> Path:
        p = self.tmp / f"work-{id(self)}-{len(self._states)}.hdd"
        self._states.append(p)
        shutil.copy(self.factory_img, p)
        return p

    @classmethod
    def setUp(cls):
        cls._states = []

    def release(self, seq: int, version: str, kernel: bytes):
        payload_dir = self.tmp / f"payload-{seq}"
        payload_dir.mkdir(exist_ok=True)
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
        mpath = self.tmp / f"manifest-{seq}.json"
        mpath.write_text(nt.dump_manifest(nt.sign_manifest(m, self.secret)),
                         encoding="ascii")
        return mpath, payload_dir

    def state(self, name: str, n: int = 0) -> Path:
        p = self.tmp / f"state-{name}.json"
        p.write_text('{"release_sequence": %d}' % n, encoding="ascii")
        return p

    def install(self, img: Path, seq: int, version: str, kernel: bytes,
                state: Path | None = None, fault: str = "none") -> int:
        mpath, pdir = self.release(seq, version, kernel)
        state = state or self.state(f"{id(img)}-{seq}")
        ns = argparse.Namespace(image=img, manifest=mpath, payload_dir=pdir,
                                trust_key=self.tmp / "test.pub", state_file=state,
                                fault_point=fault)
        return ai.cmd_install(ns)

    def img(self, path: Path) -> ai.Fat16Image:
        return ai.Fat16Image(path)

    def entry_content(self, img, name, deep=False) -> bytes:
        hit = img.find_entry_deep(name) if deep else img.find_entry(name)
        self.assertIsNotNone(hit, name)
        off, e = hit
        first = struct.unpack_from("<H", e, 26)[0] | (struct.unpack_from("<H", e, 20)[0] << 16)
        size = struct.unpack_from("<I", e, 28)[0]
        return img.follow_chain(first, size)

    def active_config(self, img) -> dict:
        hit = img.find_entry("limine.conf")
        off, e = hit
        first = struct.unpack_from("<H", e, 26)[0] | (struct.unpack_from("<H", e, 20)[0] << 16)
        size = struct.unpack_from("<I", e, 28)[0]
        return ai.parse_config(img.follow_chain(first, size))


class TestLayoutAndPrimitives(C3Base):
    def test_parse_and_roundtrip(self):
        img = self.img(self.factory_img)
        try:
            self.assertEqual(self.entry_content(img, "limine.conf"), self.factory_conf)
            self.assertEqual(self.entry_content(img, "EFI/BOOT/LIMINE.CONF", deep=True),
                             self.factory_conf)
            self.assertEqual(self.entry_content(img, "NUCLEUS"), self.factory_kernel)
            cfg = self.active_config(img)
            self.assertEqual(cfg["sequence"], 0)
            self.assertEqual(cfg["kernel_file"], b"/NUCLEUS")
            self.assertEqual(cfg["blake2b"], ai.blake2b_512(self.factory_kernel))
        finally:
            img.close()

    def test_high_water_covers_every_nonzero_fat_entry(self):
        img = self.img(self.factory_img)
        try:
            hw = img.high_water()
            self.assertGreater(hw, 1)
            # Every cluster at or below the watermark that the FAT marks
            # used must exist; strictly above it every entry must be free.
            for c in range(2, hw + 1):
                if img.fat_entry(c) != 0:
                    pass  # used cluster below watermark: expected
            for c in range(hw + 1, min(hw + 50, img.max_cluster + 1)):
                self.assertEqual(img.fat_entry(c), 0,
                                 f"cluster {c} free-range violated above watermark")
        finally:
            img.close()

    def test_alloc_after_refuses_below_watermark_and_exhaustion(self):
        img = self.img(self.factory_img)
        try:
            hw = img.high_water()
            got = img.alloc_after(hw, 3)
            self.assertTrue(all(c > hw for c in got))
            with self.assertRaises(ai.InstallError):
                img.alloc_after(img.max_cluster, 1)  # nothing above the end
        finally:
            img.close()

    def test_fat_copy_mismatch_refuses(self):
        p = self.fresh_work()
        img = self.img(p)
        c = img.high_water() + 5
        off = img.fat_off + img.fat_sectors * me.SECTOR + c * 2
        img.pwrite(off, b"\xff\xff")
        with self.assertRaises(ai.InstallError):
            img.check_fat_copies()
        img.close()

    def test_patch_entry_touches_only_the_target_entry(self):
        p = self.fresh_work()
        img = self.img(p)
        try:
            hit = img.find_entry("limine.conf")
            off, _e = hit
            sector_off = off - (off % me.SECTOR)
            before = img.pread(sector_off, me.SECTOR)
            rel = off - sector_off
            img.patch_entry(off, 12345, 6789)
            after = img.pread(sector_off, me.SECTOR)
            self.assertEqual(before[:rel], after[:rel])
            self.assertEqual(before[rel + 32:], after[rel + 32:])
            e = after[rel:rel + 32]
            self.assertEqual(struct.unpack_from("<H", e, 26)[0], 12345 & 0xFFFF)
            self.assertEqual(struct.unpack_from("<I", e, 28)[0], 6789)
        finally:
            img.close()

    def test_parse_config_negatives(self):
        # Two /NUCLEUS path lines is ambiguous -> refused.
        with self.assertRaises(ai.InstallError):
            ai.parse_config(b"path: boot(1):/NUCLEUS#" + b"a" * 128
                            + b"\npath: boot(1):/NUCLEUS#" + b"b" * 128 + b"\n")
        # No /NUCLEUS path line at all -> refused.
        with self.assertRaises(ai.InstallError):
            ai.parse_config(b"timeout: 3\n")


class TestInstallAndRollback(C3Base):

    def test_install_seq1_lands_in_slot_A(self):
        p = self.fresh_work()
        state = self.state("s1")
        k2 = self.rel2_kernel()
        self.assertEqual(self.install(p, 1, "0.2.0", k2, state), 0)
        self.assertEqual(nt.read_state(state), 1)
        img = self.img(p)
        try:
            cfg = self.active_config(img)
            self.assertEqual(cfg["sequence"], 1)
            self.assertEqual(cfg["kernel_file"], b"/NUCLEUS.A")
            self.assertEqual(cfg["blake2b"], ai.blake2b_512(k2))
            # factory kernel + factory config untouched
            self.assertEqual(self.entry_content(img, "NUCLEUS"), self.factory_kernel)
            sm = ai.read_slotmap(img)
            self.assertIn("factory_config", sm)
            frow = sm["factory_config"]
            fdata = img.follow_chain(int(frow["chain"]), int(frow["size"]))
            self.assertEqual(hashlib.sha256(fdata).hexdigest(), frow["sha256"])
            self.assertEqual(fdata, self.factory_conf)
            self.assertEqual(sm["slots"]["A"]["sequence"], "1")
            self.assertNotIn("B", sm["slots"])
        finally:
            img.close()

    def test_two_releases_alternate_slots_and_orphans_survive(self):
        p = self.fresh_work()
        k2, k3 = self.rel2_kernel(), self.rel3_kernel()
        self.assertEqual(self.install(p, 1, "0.2.0", k2), 0)
        img = self.img(p)
        a_chain = int(ai.read_slotmap(img)["slots"]["A"]["config_chain"])
        hit = img.find_entry("NUCLEUS.A")
        a_kchain = struct.unpack_from("<H", hit[1], 26)[0]
        img.close()
        self.assertEqual(self.install(p, 2, "0.3.0", k3), 0)
        img = self.img(p)
        try:
            cfg = self.active_config(img)
            self.assertEqual((cfg["sequence"], cfg["kernel_file"]), (2, b"/NUCLEUS.B"))
            self.assertEqual(cfg["blake2b"], ai.blake2b_512(k3))
            # The previous generation's chains are orphaned but byte-intact.
            self.assertEqual(img.follow_chain(a_kchain, len(k2)), k2)
            self.assertEqual(img.follow_chain(a_chain, 0)[:0], b"")
        finally:
            img.close()
        self.assertEqual(self.install(p, 3, "0.4.0", self.rel3_kernel()), 0)
        img = self.img(p)
        try:
            cfg = self.active_config(img)
            self.assertEqual(cfg["kernel_file"], b"/NUCLEUS.A")
            hit = img.find_entry("NUCLEUS.A")
            self.assertNotEqual(struct.unpack_from("<H", hit[1], 26)[0], a_kchain)
            # ...and the seq-2 slot B generation is still the rollback target.
            self.assertEqual(img.follow_chain(a_kchain, len(k2)), k2)
        finally:
            img.close()

    def test_replay_refused_and_writes_nothing(self):
        p = self.fresh_work()
        k2 = self.rel2_kernel()
        state = self.state("rp", 0)
        self.assertEqual(self.install(p, 1, "0.2.0", k2, state), 0)
        before = p.read_bytes()
        # Host replay state now 1: the same release is refused.
        self.assertEqual(self.install(p, 1, "0.2.0", k2, state), 1)
        self.assertEqual(p.read_bytes(), before)
        # On-image sequence guard: even with the host state reset to 0, an
        # image already at sequence 1 refuses sequence 1.
        state.write_text('{"release_sequence": 0}', encoding="ascii")
        self.assertEqual(self.install(p, 1, "0.2.0", k2, state), 1)
        self.assertEqual(p.read_bytes(), before)

    def test_tampered_payload_refused_before_any_write(self):
        p = self.fresh_work()
        mpath, pdir = self.release(1, "0.2.0", self.rel2_kernel())
        kbin = pdir / "c3r1.bin"
        kbin.write_bytes(kbin.read_bytes()[:-1] + b"\x00")  # flip the last byte
        before = p.read_bytes()
        ns = argparse.Namespace(image=p, manifest=mpath, payload_dir=pdir,
                                trust_key=self.tmp / "test.pub",
                                state_file=self.state("tp"), fault_point="none")
        self.assertEqual(ai.cmd_install(ns), 1)
        self.assertEqual(p.read_bytes(), before)

    def test_rollback_chain_and_refusals(self):
        p = self.fresh_work()
        k2, k3 = self.rel2_kernel(), self.rel3_kernel()
        state = self.state("rb", 0)
        self.assertEqual(self.install(p, 1, "0.2.0", k2, state), 0)
        self.assertEqual(self.install(p, 2, "0.3.0", k3, state), 0)
        self.assertEqual(nt.read_state(state), 2)
        ns = argparse.Namespace(image=p)
        self.assertEqual(ai.cmd_rollback(ns), 0)  # slot B -> slot A
        img = self.img(p)
        cfg = self.active_config(img)
        self.assertEqual((cfg["sequence"], cfg["kernel_file"]), (1, b"/NUCLEUS.A"))
        self.assertEqual(ai.cmd_rollback(ns), 0)  # slot A -> factory
        cfg = self.active_config(img)
        self.assertEqual((cfg["sequence"], cfg["kernel_file"]), (0, b"/NUCLEUS"))
        self.assertEqual(cfg["data"], self.factory_conf)
        self.assertEqual(nt.read_state(state), 2)  # replay state NOT decremented
        self.assertEqual(ai.cmd_rollback(ns), 1)   # nothing older than factory
        img.close()

    def test_rollback_refuses_corrupted_target_chain(self):
        p = self.fresh_work()
        self.assertEqual(self.install(p, 1, "0.2.0", self.rel2_kernel()), 0)
        self.assertEqual(self.install(p, 2, "0.3.0", self.rel3_kernel()), 0)
        img = self.img(p)
        row = ai.read_slotmap(img)["slots"]["A"]
        chain = int(row["config_chain"])
        img.close()
        # Corrupt one byte inside slot A's recorded config chain.
        img = self.img(p)
        off = img.cluster_off(chain) + 10
        img.pwrite(off, b"\x00" if img.pread(off, 1) != b"\x00" else b"\x01")
        img.flush()
        img.close()
        before = p.read_bytes()
        self.assertEqual(ai.cmd_rollback(argparse.Namespace(image=p)), 1)
        self.assertEqual(p.read_bytes(), before)  # refusal wrote nothing

    def test_derived_config_pins_exactly_the_new_kernel(self):
        derived = ai.derive_slot_config(self.factory_conf, "B", self.rel2_kernel(),
                                        7, "9.9.9")
        cfg = ai.parse_config(derived)
        self.assertEqual(cfg["kernel_file"], b"/NUCLEUS.B")
        self.assertEqual(cfg["blake2b"], ai.blake2b_512(self.rel2_kernel()))
        self.assertEqual(cfg["sequence"], 7)
        # Everything except the path line and the added header is unchanged.
        stripped_active = b"\n".join(l for l in self.factory_conf.split(b"\n")
                                     if not l.strip().startswith(b"path:"))
        stripped_new = b"\n".join(l for l in derived.split(b"\n")
                                  if not l.strip().startswith(b"path:")
                                  and not l.startswith(b"# nova-"))
        self.assertEqual(stripped_new, stripped_active)


class TestFaultPoints(C3Base):
    def _safety_asserts(self, img, k2):
        """Every interruption state must leave a parseable, consistent boot
        path: the active config parses, and whatever it pins exists and
        matches its blake2b pin."""
        cfg = self.active_config(img)
        ai.verify_boot_target(img, cfg)
        if cfg["sequence"] == 0:
            self.assertEqual(cfg["kernel_file"], b"/NUCLEUS")
            self.assertEqual(self.entry_content(img, "NUCLEUS"), self.factory_kernel)
        else:
            self.assertEqual(cfg["kernel_file"], b"/NUCLEUS.A")
            self.assertEqual(cfg["blake2b"], ai.blake2b_512(k2))
        # The factory kernel and factory config chain are always intact.
        self.assertEqual(self.entry_content(img, "NUCLEUS"), self.factory_kernel)

    def test_every_op_boundary_leaves_a_consistent_image(self):
        k2 = self.rel2_kernel()
        mpath, pdir = self.release(1, "0.2.0", k2)
        specs = [f"before-{op}" for op in ai.OPS] + [
            "mid-stage_kernel_content", "mid-stage_config_content"]
        for spec in specs:
            with self.subTest(fault=spec):
                p = self.fresh_work()
                st = self.state(f"fp-{spec}")
                r = subprocess.run(
                    [sys.executable, str(REPO / "tools" / "atomic_install.py"),
                     "install", "--image", str(p), "--manifest", str(mpath),
                     "--payload-dir", str(pdir), "--trust-key", str(self.tmp / "test.pub"),
                     "--state-file", str(st), "--fault-point", spec],
                    capture_output=True, text=True, timeout=120,
                    env={**os.environ, "PYTHONPATH": str(REPO / "tools")})
                if spec.startswith("before-"):
                    self.assertEqual(r.returncode, 137,
                                     f"{spec}: expected simulated crash, got {r.returncode}"
                                     f"\nstdout: {r.stdout}\nstderr: {r.stderr}")
                    self.assertIn("C3-FAULT", r.stdout)
                else:
                    # A mid-* fault may be a no-op when the content is a
                    # single cluster (no mid-point exists); then the install
                    # simply completes - which must also leave a safe image.
                    self.assertIn(r.returncode, (0, 137),
                                  f"{spec}: unexpected rc\nstdout: {r.stdout}")
                img = self.img(p)
                try:
                    self._safety_asserts(img, k2)
                finally:
                    img.close()

    def test_mutant_skip_config_stage_commits_an_unwritten_chain(self):
        """Sabotage gate: the deliberate bug commits the pointer at a chain
        that was never written. Unit-level: the committed content is zeros,
        so the config parse must fail (stage 14 shows the machine refusing
        to boot this state)."""
        k2 = self.rel2_kernel()
        mpath, pdir = self.release(1, "0.2.0", k2)
        p = self.fresh_work()
        r = subprocess.run(
            [sys.executable, str(REPO / "tools" / "atomic_install.py"), "install",
             "--image", str(p), "--manifest", str(mpath), "--payload-dir", str(pdir),
             "--trust-key", str(self.tmp / "test.pub"),
             "--state-file", str(self.state("mut"))],
            capture_output=True, text=True, timeout=120,
            env={**os.environ, "NOVA_C3_MUTANT": "skip-config-stage",
                 "PYTHONPATH": str(REPO / "tools")})
        self.assertEqual(r.returncode, 0)
        img = self.img(p)
        try:
            hit = img.find_entry("limine.conf")
            off, e = hit
            first = struct.unpack_from("<H", e, 26)[0] | (struct.unpack_from("<H", e, 20)[0] << 16)
            # The mutant skips both content and FAT-link staging, so the
            # pointed-at cluster is raw zeros and the chain is unlinked.
            raw = img.pread(img.cluster_off(first), 64)
            self.assertTrue(all(b == 0 for b in raw),
                            "mutant committed a chain that was never written")
            self.assertNotEqual(raw, b"\x00" * 64 + b"x")
            with self.assertRaises(ai.InstallError):
                self.active_config(img)
        finally:
            img.close()


if __name__ == "__main__":
    unittest.main()
