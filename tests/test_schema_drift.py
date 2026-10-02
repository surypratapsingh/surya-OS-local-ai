#!/usr/bin/env python3
"""Schema drift check: docs/manifest-format.md vs tools/nova_trust.py.

Why this exists
---------------
docs/manifest-format.md says: "If this file and the code disagree, one of them
has a bug." In September 2026 that sentence stopped being true in practice:
df69025 changed nova_trust.py's top-level required set to demand `bootchain`
while the doc, build_manifest() and every content check in the file all treated
it as optional, and 15 of 24 tests in tests/test_trust.py sat red. Nobody
noticed for a week because no test compared the two documents.

Oracle design (AGENTS.md rule 1 and rule 2)
-------------------------------------------
There are three artefacts here and no two of them are derived from each other:

1. docs/manifest-format.md            the specification, written prose.
2. tests/fixtures/manifest-schema.json transcribed from that prose by hand.
3. tools/nova_trust.py                the implementation.

This file checks edges (1)-(2) and (2)-(3) separately:

* Edge A: a parser reads the doc's markdown field table and the bootchain JSON
  example, and asserts they describe exactly the fixture. If someone edits the
  doc without updating the fixture, this fails.
* Edge B: the fixture is compared against what nova_trust.py *does*, measured
  behaviourally. For each level the check builds a manifest that is structurally
  valid, deletes one field, and asks structure_errors() whether it complained.
  A field the code rejects when absent is required; one it tolerates is
  optional; one it tolerates when *added* is unknown-tolerant.

Edge B deliberately does NOT import nova_trust's _TOP / _RELEASE / _TRUST /
_PACKAGE / _BOOTCHAIN* constants and diff them against the fixture. Those
constants and structure_errors() come from the same file, so comparing them to
each other would only prove the file agrees with itself - it could not fail
even if the doc were rewritten to say something else entirely. Probing the
public behaviour can. That is the difference between a check and a decoration,
and it is why this file exists.

The one thing this file cannot check is the doc's prose rules (regexes, bounds,
"no wildcards"). It checks the shape: which fields exist, which are required,
which are optional, and whether unlisted fields are errors. Value rules are
test_trust.py's job.

Run: py -3 tests/test_schema_drift.py
"""
import copy
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))
import nova_trust as nt  # noqa: E402

DOC = ROOT / "docs" / "manifest-format.md"
SCHEMA = ROOT / "tests" / "fixtures" / "manifest-schema.json"

# Levels the behavioural probe knows how to reach. "packages[]" means "an
# element of the packages list"; the list itself is checked by test_trust.py.
LEVELS = [
    "manifest",
    "release",
    "trust",
    "packages[]",
    "bootchain",
    "bootchain.limine",
    "bootchain.kernel",
]

# Where structure_errors() prefixes its messages for each level, so the probe
# can tell "release: missing field" apart from "trust: missing field".
WHERE = {
    "manifest": "manifest",
    "release": "release",
    "trust": "trust",
    "packages[]": "packages[0]",
    "bootchain": "bootchain",
    "bootchain.limine": "bootchain.limine",
    "bootchain.kernel": "bootchain.kernel",
}

HEX64 = "0123456789abcdef" * 4  # 64 lowercase hex
FP = "sha256:" + HEX64


def where_level(m: dict, level: str) -> dict:
    """The object structure_errors() sees under `level`, or {} if absent."""
    if level == "manifest":
        return m
    if level == "packages[]":
        pkgs = m.get("packages")
        return pkgs[0] if isinstance(pkgs, list) and pkgs else {}
    if level in ("release", "trust"):
        return m.get(level) or {}
    # "bootchain", "bootchain.limine", "bootchain.kernel" all live under the
    # top-level "bootchain" key. Returning the live sub-object matters: the
    # probes below mutate what this returns, so handing back a throwaway {}
    # would make every bootchain probe silently vacuous.
    bc = m.get("bootchain") or {}
    if level == "bootchain":
        return bc
    return bc.get(level.split(".", 1)[1]) or {}


def base_manifest() -> dict:
    """A manifest structure_errors() accepts with zero errors, per the fixture.

    Values are chosen to satisfy every value rule in the doc so that the only
    errors the probe can provoke are about missing/unknown fields. The signature
    is a placeholder: this check is about shape, not cryptography, and
    structure_errors() only requires it to be a string.
    """
    return {
        "manifest_version": nt.MANIFEST_VERSION,
        "release": {
            "version": "1.0.0",
            "release_sequence": 7,
            "timestamp": "2026-09-25T10:00:00Z",
        },
        "trust": {"algorithm": "Ed25519", "key_fingerprint": FP},
        "packages": [{
            "id": "mathd",
            "version": "0.2.0",
            "filename": "mathd.bin",
            "sha256": HEX64,
            "size": 1048576,
            "sequence": 1,
            "capabilities": ["compute:expression"],
        }],
        "bootchain": {
            "limine": {
                "version": "12.9.0",
                "filename": "limine.bin",
                "sha256": HEX64,
                "size": 262144,
            },
            "kernel": {
                "id": "nova-kernel",
                "version": "0.1.0",
                "filename": "nova-kernel.bin",
                "sha256": HEX64,
                "size": 2097152,
            },
        },
        "signature": "AAAA",
    }


class TestDocMatchesFixture(unittest.TestCase):
    """Edge A: the fixture must still be a true transcription of the doc."""

    @classmethod
    def setUpClass(cls):
        cls.doc = DOC.read_text(encoding="utf-8")
        cls.schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        cls.levels = cls.schema["levels"]

    def test_files_exist(self):
        # AGENTS.md rule 4: every path this file names must exist.
        for p in (DOC, SCHEMA):
            self.assertTrue(p.is_file(), f"missing {p}")

    def doc_table_fields(self) -> dict:
        """Parse the '## Fields' markdown table: dotted path -> rule text.

        The table is the only normative list of fields in the doc. A row looks
        like | `release.description` | optional string |, so the first backtick
        span is the path. 'Every field is required unless marked optional' means
        a row is optional iff its rule text contains 'optional'.
        """
        out: dict[str, str] = {}
        for line in self.doc.splitlines():
            m = re.match(r"^\|\s*`([A-Za-z0-9_\[\].]+)`\s*\|(.*)\|\s*$", line)
            if m:
                out[m.group(1)] = m.group(2)
        return out

    def test_doc_table_covers_fixture_manifest_release_trust_packages(self):
        rows = self.doc_table_fields()
        self.assertIn("manifest_version", rows, "doc field table lost manifest_version")
        self.assertIn("signature", rows, "doc field table lost signature")

        # Top level: rows without a dotted prefix, plus the containers implied
        # by rows that have one. The table never has a bare `release` or `trust`
        # row - it lists release.version, trust.algorithm and so on - so the
        # objects themselves are named by their children.
        top_required = {n for n in rows if "." not in n}
        top_required |= {re.sub(r"\[.*\]", "", n.split(".", 1)[0]) for n in rows if "." in n}
        expected_top = set(self.levels["manifest"]["required"])
        self.assertEqual(
            top_required, expected_top,
            "manifest level disagrees with the doc field table",
        )

        for level, prefix in (("release", "release."),
                              ("trust", "trust.")):
            from_doc = {n[len(prefix):] for n in rows if n.startswith(prefix)}
            self.assertEqual(
                from_doc, set(self.levels[level]["required"]) | set(self.levels[level]["optional"]),
                f"{level} level disagrees with the doc field table",
            )

        pkg_prefix = "packages[i]."
        from_doc = {n[len(pkg_prefix):] for n in rows if n.startswith(pkg_prefix)}
        self.assertEqual(
            from_doc,
            set(self.levels["packages[]"]["required"]) | set(self.levels["packages[]"]["optional"]),
            "packages[] level disagrees with the doc field table",
        )

    def test_doc_optionality_matches_fixture(self):
        rows = self.doc_table_fields()
        for level, prefix in (("release", "release."),):
            for field in self.levels[level]["optional"]:
                rule = rows.get(prefix + field, "")
                self.assertIn(
                    "optional", rule.lower(),
                    f"{level}.{field} is optional in the fixture but the doc "
                    f"does not mark it optional (rule: {rule!r})",
                )
        # The only optional field the doc marks, anywhere, is
        # release.description. If that ever changes, the fixture must change.
        doc_optional = {n for n, r in rows.items() if "optional" in r.lower()}
        fixture_optional = {f"release.{f}" for f in self.levels["release"]["optional"]}
        self.assertEqual(
            doc_optional, fixture_optional,
            "the set of doc-marked-optional fields changed; update the fixture",
        )

    def test_doc_bootchain_example_matches_fixture(self):
        # The bootchain field list lives in a JSON example, not the table,
        # because the doc presents bootchain as its own section (C4).
        block = re.search(
            r'"bootchain":\s*(\{.*?\n\})\s*\n```',
            self.doc,
            re.S,
        )
        self.assertIsNotNone(block, "could not find the bootchain JSON example")
        bc = json.loads(block.group(1))

        self.assertEqual(
            set(bc), set(self.levels["bootchain"]["required"]),
            "bootchain members disagree with the doc example",
        )
        for comp in ("limine", "kernel"):
            self.assertEqual(
                set(bc[comp]),
                set(self.levels[f"bootchain.{comp}"]["required"]),
                f"bootchain.{comp} fields disagree with the doc example",
            )

    def test_doc_states_both_bootchain_members_required(self):
        self.assertIn(
            "Both `limine` and `kernel` are required",
            self.doc,
            "the doc no longer says both bootchain members are required; "
            "if that rule changed, nova_trust.py must change with it",
        )

    def test_fixture_has_a_level_for_every_level_we_probe(self):
        self.assertEqual(set(self.levels), set(LEVELS),
                         "fixture levels and probed levels disagree")


class TestCodeMatchesFixture(unittest.TestCase):
    """Edge B: what nova_trust.py actually does, measured by probing it."""

    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        cls.levels = cls.schema["levels"]

    def required_in_code(self, level: str, field: str) -> bool:
        """Delete `field` and see whether structure_errors() objects.

        True means the code treats the field as required. This is the only way
        this file learns the code's rules: it never reads nova_trust's set
        constants, because those and the checker share an author and could
        only ever agree with each other.
        """
        m = base_manifest()
        where_level(m, level).pop(field, None)
        errs = nt.structure_errors(m)
        marker = f"{WHERE[level]}: missing field '{field}'"
        return any(e.startswith(marker) or marker in e for e in errs)

    def test_base_manifest_is_accepted(self):
        # Precondition for every probe below: with no field removed, the probe
        # object must produce no errors. If it does not, the values in
        # base_manifest() are wrong and the probes below would prove nothing.
        self.assertEqual(nt.structure_errors(base_manifest()), [],
                         "base_manifest() is not structurally valid, so the "
                         "omission probes below cannot be trusted")

    def test_required_fields_match_fixture(self):
        for level in LEVELS:
            for field in self.levels[level]["required"]:
                with self.subTest(level=level, field=field):
                    self.assertTrue(
                        self.required_in_code(level, field),
                        f"the fixture (from docs/manifest-format.md) requires "
                        f"{level}.{field}, but nova_trust.py accepts a "
                        f"manifest without it",
                    )

    def test_optional_fields_are_actually_optional(self):
        for level in LEVELS:
            for field in self.levels[level]["optional"]:
                with self.subTest(level=level, field=field):
                    self.assertFalse(
                        self.required_in_code(level, field),
                        f"the fixture (from docs/manifest-format.md) marks "
                        f"{level}.{field} optional, but nova_trust.py refuses "
                        f"a manifest without it",
                    )

    def test_unknown_fields_are_rejected_at_every_level(self):
        # "A field not listed here is an error anywhere in the manifest. The
        # verifier fails closed." Probe each level with a field nobody lists.
        probes = {
            "manifest": "_zz_not_a_field",
            "release": "_zz_not_a_field",
            "trust": "_zz_not_a_field",
            "packages[]": "_zz_not_a_field",
            "bootchain.limine": "_zz_not_a_field",
            "bootchain.kernel": "_zz_not_a_field",
        }
        for level, bogus in probes.items():
            with self.subTest(level=level):
                m = base_manifest()
                where_level(m, level)[bogus] = "x"
                errs = nt.structure_errors(m)
                self.assertTrue(
                    any("unknown field" in e and bogus in e for e in errs),
                    f"nova_trust.py accepted the undocumented field "
                    f"{bogus} at {level}; the doc says unlisted fields are "
                    f"errors everywhere",
                )

    def test_unknown_bootchain_component_is_rejected(self):
        m = base_manifest()
        m["bootchain"]["_zz_not_a_component"] = {"version": "1.0.0"}
        errs = nt.structure_errors(m)
        self.assertTrue(
            any("unknown component" in e for e in errs),
            "nova_trust.py accepted an unlisted bootchain component; the doc "
            "names only limine and kernel",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)