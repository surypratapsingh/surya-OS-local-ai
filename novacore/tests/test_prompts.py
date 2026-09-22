from __future__ import annotations

import re
import unittest

from novacore.capabilities import CATALOGUE
from novacore.prompts import SYSTEM_PROMPT


class PromptTests(unittest.TestCase):
    def test_prompt_and_catalogue_are_consistent(self) -> None:
        prompt_lower = SYSTEM_PROMPT.lower()

        # Extract capability names from prompt (dotted names like "system.status").
        capability_pattern = re.compile(r'\b[a-z_]+\.[a-z_]+\b')
        mentioned = set(capability_pattern.findall(prompt_lower))

        # Capabilities that require confirmation must be named in the prompt.
        # (Exclude test-only capabilities starting with underscore.)
        for name, cap in CATALOGUE.items():
            if cap.requires_confirmation and not name.startswith("_"):
                self.assertIn(
                    name.lower(),
                    mentioned,
                    f"Capability {name} requires confirmation but is not mentioned in the prompt",
                )

        # The prompt must not name capabilities that don't exist or mention
        # capability-like actions absent from the catalogue.
        # Check for standalone action verbs (word boundaries), not parts of other words.
        action_to_capability = {
            r"\breboot\b": "system.reboot",
            r"\berase\b": None,
            r"\bformat\b": None,
            r"\bwipe\b": None,
            r"\buninstall\b": None,
            r"\binstall\b": None,
            r"\bupdate\b": "updates.apply",
            r"\bupgrade\b": None,
        }
        for action_pattern, expected_capability in action_to_capability.items():
            if re.search(action_pattern, prompt_lower):
                if expected_capability is None or expected_capability not in CATALOGUE:
                    action_name = action_pattern.strip(r"\b")
                    self.fail(
                        f"Prompt mentions '{action_name}' "
                        f"but no capability implements it"
                    )

        # Core hard rules: network, memory, hardware.
        # Check for these rules, allowing for whitespace variations.
        self.assertTrue(
            re.search(r"never\s+request\s+internet", prompt_lower),
            "Prompt must forbid requesting internet access",
        )
        self.assertIn("never claim a note is remembered", prompt_lower)
        self.assertIn("no direct", prompt_lower)
