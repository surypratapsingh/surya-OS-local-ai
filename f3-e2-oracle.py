"""F3.12 independent oracle for E2: a rejected shuttle import must leave the
proposal store byte-identical.

The oracle does not use novacore's own test helper (dir_snapshot compares
dicts). It walks the proposal-store directory itself and hashes a canonical
serialisation with hashlib.sha256: for every regular file, its POSIX-style
relative path, its length, and the SHA-256 of its bytes. A missing directory
hashes the literal "ABSENT"; an empty directory "EMPTY".

Expected outcomes come from the work order text, not from shuttle.py:
  * a rejected package raises ShuttleVerificationError and leaves the store
    digest and the file count unchanged;
  * an accepted package changes the digest and the new proposal says
    applied=false and status=proposed_unverified.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "novacore" / "src"))
sys.path.insert(0, str(ROOT / "tools"))

spec = importlib.util.spec_from_file_location("pack_shuttle", ROOT / "tools" / "pack-shuttle.py")
pack_shuttle = importlib.util.module_from_spec(spec)
sys.modules["pack_shuttle"] = pack_shuttle
spec.loader.exec_module(pack_shuttle)

import nova_trust as nt  # noqa: E402
from novacore.shuttle import ShuttleImporter  # noqa: E402
from novacore.updates import UpdateProposalStore  # noqa: E402

failures: list[str] = []
observations: list[str] = []


def store_digest(store: Path) -> str:
    if not store.exists():
        return "ABSENT"
    files = sorted(p for p in store.rglob("*") if p.is_file())
    if not files:
        return "EMPTY"
    h = hashlib.sha256()
    for p in files:
        rel = p.relative_to(store).as_posix().encode()
        data = p.read_bytes()
        h.update(len(rel).to_bytes(8, "big"))
        h.update(rel)
        h.update(len(data).to_bytes(8, "big"))
        h.update(hashlib.sha256(data).digest())
    return h.hexdigest()


def store_count(store: Path) -> int:
    return len([p for p in store.rglob("*") if p.is_file()]) if store.exists() else 0


def check(name: str, ok: bool, detail: str) -> None:
    observations.append(f"{'ok  ' if ok else 'FAIL'} {name}: {detail}")
    if not ok:
        failures.append(name)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        src = tmp / "src"
        src.mkdir()
        payload = src / "mathd.bin"
        payload.write_bytes(b"NOVA f3 oracle payload " * 40)

        key_dir = tmp / "keys"
        _priv, pub = pack_shuttle.ensure_test_key(key_dir)
        store = tmp / "proposals"
        state = tmp / "state.json"
        state.write_text('{"release_sequence": 2}\n', encoding="utf-8")
        importer = ShuttleImporter(UpdateProposalStore(store), pub, state_path=state)

        def make(seq: int, version: str, out: str) -> Path:
            d = tmp / out
            pack_shuttle.pack_shuttle(
                out_dir=d,
                packages=[("mathd", version, payload, ["compute:verify"])],
                sequence=seq,
                version=version,
                key_dir=key_dir,
            )
            return d

        def reject(name: str, pkg: Path) -> None:
            before, before_n = store_digest(store), store_count(store)
            try:
                importer.import_package(pkg)
                exc = "none"
            except Exception as error:  # noqa: BLE001 - the class is the observation
                exc = type(error).__name__
            after, after_n = store_digest(store), store_count(store)
            check(
                name,
                exc == "ShuttleVerificationError" and before == after and before_n == after_n,
                f"raised={exc} digest {before} -> {after} files {before_n} -> {after_n}",
            )

        # A. missing manifest: store must not even be created.
        empty = tmp / "empty"
        empty.mkdir()
        reject("A_missing_manifest", empty)

        # B. clean package: digest must change and the proposal must be unapplied.
        clean = make(3, "0.2.0", "pkg_clean")
        before = store_digest(store)
        update = importer.import_package(clean)
        after = store_digest(store)
        data = json.loads(Path(update).read_text(encoding="utf-8"))
        check(
            "B_clean_import",
            before != after and data.get("applied") is False
            and data.get("status") == "proposed_unverified" and store_count(store) == 1,
            f"digest {before} -> {after} applied={data.get('applied')} "
            f"status={data.get('status')} files={store_count(store)}",
        )

        # C. payload tamper: one byte flipped after signing.
        tampered = make(4, "0.3.0", "pkg_tamper")
        target = tampered / "mathd.bin"
        raw = bytearray(target.read_bytes())
        raw[9] ^= 0xFF
        target.write_bytes(bytes(raw))
        reject("C_payload_tamper", tampered)

        # D. replay: sequence equal to the last accepted one.
        reject("D_replay_equal_seq", make(2, "0.1.9", "pkg_replay"))

        # E. signed by a key that is not the trusted root.
        other = tmp / "other_keys"
        other.mkdir()
        seed = hashlib.sha256(b"F3 OTHER KEY").digest()
        (other / "TEST-root.priv").write_text(nt.private_key_pem(seed), encoding="ascii")
        (other / "TEST-root.pub").write_text(nt.public_key_pem(nt.public_key(seed)), encoding="ascii")
        foreign = tmp / "pkg_foreign"
        pack_shuttle.pack_shuttle(
            out_dir=foreign,
            packages=[("mathd", "0.4.0", payload, ["compute:verify"])],
            sequence=5,
            version="0.4.0",
            key_dir=other,
        )
        reject("E_untrusted_key", foreign)

        # F. manifest missing the release object.
        broken = make(6, "0.5.0", "pkg_broken")
        (broken / "release.manifest").write_text('{"packages": []}\n', encoding="utf-8")
        reject("F_broken_schema", broken)

        # G. verify_package reports the payload mismatch without writing anything.
        before = store_digest(store)
        _m, fails = importer.verify_package(tampered)
        after = store_digest(store)
        check(
            "G_verify_reports_sha",
            bool(fails) and "sha256" in "; ".join(fails).lower() and before == after,
            f"failures={fails!r} digest {before} -> {after}",
        )

    for line in observations:
        print(line)
    print(f"F3 E2 ORACLE: {len(failures)} failed of {len(observations)} checks")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
