"""W4 item 4: the prompt/catalogue property test.

Substring pinning was a weak alarm: it could not see a capability that was
in `CATALOGUE` but not in the prompt (or the reverse), and it skipped names
starting with `_`. Replaced with two set properties plus the hard-rule pins:

1. Every capability in `CATALOGUE` is named in `SYSTEM_PROMPT` (property 1;
   see decisions.md 2026-10-12 for why it covers every entry, not only the
   confirmation-required ones).
2. `SYSTEM_PROMPT` names no capability-like dotted name outside the test's
   explicit vocabulary list (property 2 covers dotted names only), and every
   vocabulary name is a real entry in `CATALOGUE`.
3. The prompt still forbids network access, invented tools, unbacked memory
   claims, and direct hardware authority.
"""

from __future__ import annotations

import re
import unittest

from novacore.capabilities import CATALOGUE
from novacore.prompts import SYSTEM_PROMPT


# The capability-like names the prompt is allowed to use. Explicit list,
# not "whatever CATALOGUE contains": an addition on either side fails the
# suite here as well as in the property tests below.
VOCABULARY = frozenset(
    {
        "camera.enable",
        "microphone.enable",
        "system.reboot",
        "updates.apply",
        "system.status",
        "screen.say",
        "audio.speak",
    }
)

# Dotted capability-like tokens ("camera.enable", "usb2.mount", "system.reboot.force",
# "x.mount", "camera.enable-all", "system.x"):
# segments are lowercase words with optional digits and hyphens (>= 2 parts).
# A match is extracted if at least one segment is longer than 1 character,
# excluding standard Latin abbreviations like "e.g." or "i.e.".
DOTTED_NAME = re.compile(r"\b[a-z][a-z0-9_]*(?:\.[a-z](?:[a-z0-9_-]*[a-z0-9_])?)+")


def extract_dotted_names(text: str) -> set[str]:
    """Extract capability-like dotted names from text.

    Requires at least one dotted part to be longer than 1 character so that
    two-letter abbreviations like 'e.g.' and 'i.e.' are not treated as capability names.
    """
    return {
        m for m in DOTTED_NAME.findall(text)
        if any(len(p) > 1 for p in m.split("."))
    }


# Hard rules that must survive any prompt rewrite (W4 item 4, third bullet).
NETWORK_RULE = re.compile(r"never\s+request\s+internet")
INVENTED_TOOLS_RULE = (
    "do not invent tools, commands, memories, device state, successful actions, or"
)
MEMORY_RULE = "never claim a note is remembered"
DIRECT_AUTHORITY_RULE = "you have no direct shell"
UPDATE_PROPOSAL_RULE = re.compile(r"an\s+update\s+proposal\s+is\s+not\s+an\s+applied\s+update")


class PromptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.prompt = SYSTEM_PROMPT.lower()
        self.mentioned = extract_dotted_names(self.prompt)

    def test_every_catalogue_capability_is_named_in_the_prompt(self) -> None:
        # Property 1, full scope: a capability the prompt does not name is
        # one the model may only learn about some other way — the drift the
        # W4 probes exploit. Covers probe 1 (confirmation-required) and
        # probe 2 (requires_confirmation=False) identically.
        for name in CATALOGUE:
            self.assertIn(
                name,
                self.mentioned,
                f"CATALOGUE entry {name} is not named in SYSTEM_PROMPT",
            )
        # The confirmation-required subset is the load-bearing half; state
        # it separately so a failure names the risk class.
        for name, capability in CATALOGUE.items():
            if capability.requires_confirmation:
                self.assertIn(
                    name,
                    self.mentioned,
                    f"confirmation-required capability {name} "
                    "is not named in SYSTEM_PROMPT",
                )

    def test_prompt_names_no_capability_outside_the_vocabulary(self) -> None:
        # Property 2: nothing in the prompt may promise an action no
        # catalogue entry backs (probe 3's camera.record lands here).
        for name in self.mentioned:
            self.assertIn(
                name,
                VOCABULARY,
                f"SYSTEM_PROMPT names {name}, which is not in the "
                "test vocabulary (and may be absent from CATALOGUE)",
            )
        # Belt: every vocabulary name must be a real catalogue entry, so a
        # vocabulary that drifts ahead of the code fails too.
        for name in VOCABULARY:
            self.assertIn(
                name,
                CATALOGUE,
                f"vocabulary name {name} is not in CATALOGUE",
            )

    def test_prompt_still_carries_the_hard_rules(self) -> None:
        self.assertIsNotNone(
            NETWORK_RULE.search(self.prompt),
            "prompt must forbid requesting internet access",
        )
        self.assertIn(
            INVENTED_TOOLS_RULE,
            self.prompt,
            "prompt must forbid inventing tools and successful actions "
            "(probe 4 deletes this sentence)",
        )
        self.assertIn(MEMORY_RULE, self.prompt)
        self.assertIn(DIRECT_AUTHORITY_RULE, self.prompt)
        self.assertIsNotNone(
            UPDATE_PROPOSAL_RULE.search(self.prompt),
            "prompt must keep 'an update proposal is not an applied update'",
        )

    def test_dotted_name_extractor_on_probes(self) -> None:
        """Verify the dotted name extractor catches hyphens and single letters,

        while excluding Latin abbreviations like 'e.g.' and 'i.e.'.
        """
        probes: dict[str, set[str]] = {
            "system.reboot.force": {"system.reboot.force"},
            "x.mount": {"x.mount"},
            "camera.enable-all": {"camera.enable-all"},
            "system.x": {"system.x"},
            "usb2.mount": {"usb2.mount"},
            "e.g. this": set(),
            "i.e. that": set(),
            "at 9 a.m. today": set(),
            "version 1.2.3": set(),
            "end of sentence.": set(),
        }
        for sample, want in probes.items():
            self.assertEqual(
                extract_dotted_names(sample.lower()),
                want,
                f"sample {sample!r} did not extract {want!r}",
            )


if __name__ == "__main__":
    unittest.main()
