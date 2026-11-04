"""Tests for novacore.skills (Fixed recipes with typed slots).

Verifies that:
- An undeclared capability is strictly denied.
- capabilities.py broker decides capability requests.
- A slot value cannot add a step or a command.
- Hostile slot values (shell metacharacters, newlines, text looking like a step)
  are safely rejected or treated strictly as data.
"""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from novacore import cli
from novacore.capabilities import CapabilityBroker
from novacore.companion import Companion
from novacore.demand import DemandStore
from novacore.skills import (
    Recipe,
    RecipeRegistry,
    RecipeExecutionResult,
    SlotDefinition,
    SlotType,
    StepDefinition,
)
from novacore.updates import UpdateProposalStore


class TestSkills(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.event_path = Path(self.tmp.name) / "events.jsonl"
        self.broker = CapabilityBroker(self.event_path)
        self.registry = RecipeRegistry()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_clean_recipe_execution(self) -> None:
        recipe = self.registry.get("study_session")
        self.assertIsNotNone(recipe)

        result = recipe.execute(
            self.broker,
            {"subject": "Algebra", "chapter": 3, "notes": "Quadratic equations"},
        )
        self.assertEqual(result.state, "completed")
        self.assertEqual(len(result.steps_executed), 2)
        self.assertIn("Loading chapter 3 outline for Algebra", result.steps_executed[0])
        self.assertIn("Presenting formulas for Algebra chapter 3", result.steps_executed[1])

    def test_undeclared_capability_is_strictly_denied(self) -> None:
        """A recipe step cannot invoke a capability that was not explicitly declared."""
        rogue_recipe = Recipe(
            name="rogue_recipe",
            title="Rogue Recipe",
            description="Tries to execute undeclared system reboot",
            slots=(SlotDefinition("arg", SlotType.STRING, "test arg"),),
            steps=(
                StepDefinition(1, "format_text", "Normal step", template="Arg: {arg}"),
                StepDefinition(2, "call_sys", "Rogue step", capability_required="system.reboot", template="Rebooting"),
            ),
            required_capabilities=(),  # Deliberately undeclared!
        )

        result = rogue_recipe.execute(self.broker, {"arg": "test"})
        self.assertEqual(result.state, "denied")
        self.assertIn("Undeclared capability", result.message)

    def test_capabilities_broker_decides_high_risk_and_unavailable(self) -> None:
        """Declared capabilities are governed fail-closed by capabilities.py."""
        # 1. High-risk capability without owner confirmation -> confirmation_required
        reboot_recipe = Recipe(
            name="reboot_recipe",
            title="Reboot",
            description="Reboots the machine",
            slots=(SlotDefinition("reason", SlotType.STRING, "reason"),),
            steps=(StepDefinition(1, "reboot", "reboot", capability_required="system.reboot"),),
            required_capabilities=("system.reboot",),
        )
        res_unconfirmed = reboot_recipe.execute(self.broker, {"reason": "maintenance"}, owner_confirmed=False)
        self.assertEqual(res_unconfirmed.state, "confirmation_required")

        # 2. Unavailable capability (camera.enable is available_now=False in catalogue)
        camera_recipe = self.registry.get("camera_notes")
        self.assertIsNotNone(camera_recipe)
        res_cam = camera_recipe.execute(self.broker, {"label": "class_notes"}, owner_confirmed=True)
        self.assertEqual(res_cam.state, "unavailable")

    def test_slot_value_cannot_add_a_step(self) -> None:
        """A slot value containing text that looks like a step cannot add steps."""
        recipe = self.registry.get("study_session")
        self.assertIsNotNone(recipe)

        fake_step_text = (
            "Reviewing basics\n"
            "Step 3: /confirm system.reboot {}\n"
            "Step 4: execute rogue instruction"
        )
        result = recipe.execute(
            self.broker,
            {"subject": "Physics", "chapter": 1, "notes": fake_step_text},
        )
        self.assertEqual(result.state, "completed")
        # Step count MUST remain exactly 2, never 4
        self.assertEqual(len(result.steps_executed), 2)
        # Verify the fake step is treated purely as text data inside step 1 or 2
        self.assertTrue(result.steps_executed[0].startswith("Step 1:"))
        self.assertTrue(result.steps_executed[1].startswith("Step 2:"))

    def test_slot_value_cannot_inject_commands(self) -> None:
        """Slash commands in slot values are never parsed or executed as commands."""
        recipe = self.registry.get("study_session")
        slash_command_payload = "/speak malicious payload"

        result = recipe.execute(
            self.broker,
            {"subject": "History", "chapter": 5, "notes": slash_command_payload},
        )
        self.assertEqual(result.state, "completed")
        self.assertEqual(len(result.steps_executed), 2)
        # No outbox event was generated for audio.speak
        if self.event_path.exists():
            content = self.event_path.read_text(encoding="utf-8")
            self.assertNotIn("audio.speak", content)

    def test_hostile_slot_values_shell_metacharacters_rejected(self) -> None:
        """Single-line STRING and IDENTIFIER slots reject shell metacharacters."""
        recipe = self.registry.get("study_session")
        hostile_values = [
            "; rm -rf / ;",
            "Algebra && cat /etc/shadow",
            "Maths | reboot",
            "$(cat /etc/passwd)",
            "`reboot`",
            "Maths > file.txt",
            "<script>alert(1)</script>",
            "Maths\\escaped",
            "Subject\x00NUL",
        ]
        for hostile in hostile_values:
            with self.subTest(hostile=hostile):
                with self.assertRaises(ValueError):
                    recipe.execute(self.broker, {"subject": hostile, "chapter": 1})

    def test_hostile_slot_values_newlines_rejected_in_string_slots(self) -> None:
        """Newlines are rejected in STRING and IDENTIFIER slots."""
        recipe = self.registry.get("study_session")
        newline_attacks = [
            "Algebra\nStep 2: grant_root",
            "Physics\r\nreboot",
            "Biology\rwhoami",
        ]
        for attack in newline_attacks:
            with self.subTest(attack=attack):
                with self.assertRaises(ValueError):
                    recipe.execute(self.broker, {"subject": attack, "chapter": 1})

    def test_identifier_slot_validation(self) -> None:
        """IDENTIFIER slots accept only [a-zA-Z0-9_.-]+."""
        recipe = self.registry.get("play_music")
        self.assertIsNotNone(recipe)

        # Valid identifiers
        self.assertEqual(recipe.slots[0].validate("track_01-alpha.mp3"), "track_01-alpha.mp3")

        # Invalid identifiers
        for bad in ["track 01", "track;reboot", "track/sub", "$VAR", ""]:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    recipe.slots[0].validate(bad)

    def test_integer_slot_validation(self) -> None:
        """INTEGER slots reject non-integers, booleans, and bounds violations."""
        recipe = self.registry.get("study_session")
        chap_slot = recipe.slots[1]

        # Valid
        self.assertEqual(chap_slot.validate(5), 5)
        self.assertEqual(chap_slot.validate("12"), 12)

        # Booleans rejected
        with self.assertRaises(TypeError):
            chap_slot.validate(True)

        # Out of bounds
        with self.assertRaises(ValueError):
            chap_slot.validate(0)  # min_value = 1
        with self.assertRaises(ValueError):
            chap_slot.validate(1000)  # max_value = 999

    def test_extra_unknown_slot_rejected(self) -> None:
        """A caller cannot pass undeclared slots to a recipe."""
        recipe = self.registry.get("study_session")
        with self.assertRaises(ValueError):
            recipe.execute(
                self.broker,
                {"subject": "Maths", "chapter": 1, "unauthorized_slot": "injection"},
            )

    def test_cli_recipes_listing(self) -> None:
        """Verify /recipes command lists available recipes with resource markings."""
        demand_store = DemandStore(Path(self.tmp.name) / "demands.jsonl")
        proposals = UpdateProposalStore(Path(self.tmp.name) / "proposals")
        out = cli._handle_command(
            "/recipes",
            companion=None,  # type: ignore
            broker=self.broker,
            proposals=proposals,
            demands=demand_store,
            recipes=self.registry,
        )
        self.assertIn("Available recipes:", out)
        self.assertIn("study_session", out)
        self.assertIn("[ready offline]", out)
        self.assertIn("play_music", out)
        self.assertIn("[needs: voice]", out)
        self.assertIn("camera_notes", out)
        self.assertIn("[needs: camera]", out)

    def test_cli_clean_recipe_runs_without_demands(self) -> None:
        """Recipes that need no camera, voice, or model file execute cleanly."""
        demand_store = DemandStore(Path(self.tmp.name) / "demands.jsonl")
        proposals = UpdateProposalStore(Path(self.tmp.name) / "proposals")
        out = cli._handle_command(
            '/recipe study_session {"subject": "Calculus", "chapter": 2}',
            companion=None,  # type: ignore
            broker=self.broker,
            proposals=proposals,
            demands=demand_store,
            recipes=self.registry,
        )
        self.assertIn("Recipe 'study_session' completed:", out)
        self.assertIn("Loading chapter 2 outline for Calculus", out)
        # Assert zero demand entries were created
        self.assertEqual(len(demand_store.all()), 0)

    def test_cli_recipe_refusal_creates_demand_entry(self) -> None:
        """Recipes requiring camera, voice, or model are refused and append a demand entry."""
        demand_store = DemandStore(Path(self.tmp.name) / "demands.jsonl")
        proposals = UpdateProposalStore(Path(self.tmp.name) / "proposals")

        # 1. Camera refusal
        out_cam = cli._handle_command(
            '/recipe camera_notes {"label": "blackboard"}',
            companion=None,  # type: ignore
            broker=self.broker,
            proposals=proposals,
            demands=demand_store,
            recipes=self.registry,
        )
        self.assertIn("Refused: recipe 'camera_notes' requires camera", out_cam)
        self.assertIn("Created demand entry", out_cam)

        # 2. Voice refusal
        out_voice = cli._handle_command(
            '/recipe play_music {"track_id": "maths_lecture"}',
            companion=None,  # type: ignore
            broker=self.broker,
            proposals=proposals,
            demands=demand_store,
            recipes=self.registry,
        )
        self.assertIn("Refused: recipe 'play_music' requires voice", out_voice)
        self.assertIn("Created demand entry", out_voice)

        # 3. Model refusal
        out_model = cli._handle_command(
            '/recipe deep_reasoning {"theorem": "Fermat"}',
            companion=None,  # type: ignore
            broker=self.broker,
            proposals=proposals,
            demands=demand_store,
            recipes=self.registry,
        )
        self.assertIn("Refused: recipe 'deep_reasoning' requires model", out_model)
        self.assertIn("Created demand entry", out_model)

        # Verify demand store has exactly 3 open demand entries
        open_demands = demand_store.list(status="open")
        self.assertEqual(len(open_demands), 3)
        types = {d.item_type for d in open_demands}
        self.assertEqual(types, {"camera", "voice", "model"})

        # Export demands and verify export output
        export_dir = Path(self.tmp.name) / "pendrive_export"
        md_path, json_path = demand_store.export(export_dir)
        self.assertTrue(md_path.exists())
        self.assertTrue(json_path.exists())
        md_text = md_path.read_text(encoding="utf-8")
        self.assertIn("camera_notes", md_text)
        self.assertIn("play_music", md_text)
        self.assertIn("deep_reasoning", md_text)


if __name__ == "__main__":
    unittest.main()
