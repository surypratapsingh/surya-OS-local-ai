from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from novacore.companion import Companion
from novacore.config import ConversationConfig
from novacore.llm import ModelUnavailable
from novacore.memory import MemoryStore


class RecordingBackend:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return "I am here with you."


class CompanionTests(unittest.TestCase):
    def test_conversation_does_not_persist_memory_implicitly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memories.jsonl"
            companion = Companion(
                RecordingBackend(), MemoryStore(path), ConversationConfig(history_turns=2, memory_results=2)
            )
            self.assertEqual("I am here with you.", companion.reply("My private detail"))
            self.assertFalse(path.exists())

    def test_explicit_memory_is_recalled_as_untrusted_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memories.jsonl"
            backend = RecordingBackend()
            companion = Companion(
                backend, MemoryStore(path), ConversationConfig(history_turns=2, memory_results=2)
            )
            companion.remember("The owner likes quiet study sessions.")
            companion.reply("What study setting do I like?")
            self.assertIn("quiet study sessions", backend.prompts[-1])
            self.assertIn("untrusted notes", backend.prompts[-1])

    def _companion(self, directory: str, backend, history_turns: int = 2) -> Companion:
        return Companion(
            backend,
            MemoryStore(Path(directory) / "memories.jsonl"),
            ConversationConfig(history_turns=history_turns, memory_results=2),
        )

    def test_blank_input_never_reaches_the_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            backend = RecordingBackend()
            companion = self._companion(directory, backend)
            self.assertEqual("Please say a little more so I can help.", companion.reply("  \n\t "))
            self.assertEqual([], backend.prompts)
            self.assertEqual([], companion.history)

    def test_model_unavailable_is_reported_not_invented(self) -> None:
        class Down:
            def complete(self, prompt: str) -> str:
                raise ModelUnavailable("runner missing")

        with tempfile.TemporaryDirectory() as directory:
            reply = self._companion(directory, Down()).reply("hello")
            self.assertEqual("NOVA cannot reply truthfully: runner missing", reply)

    def test_history_keeps_only_the_last_history_turns_exchanges(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            backend = RecordingBackend()
            companion = self._companion(directory, backend, history_turns=2)
            for text in ("first", "second", "third"):
                companion.reply(text)
            self.assertEqual(4, len(companion.history))
            self.assertEqual(["second", "I am here with you.", "third", "I am here with you."],
                             [turn.text for turn in companion.history])
            companion.reply("fourth")
            self.assertNotIn('"first"', backend.prompts[-1])  # dropped after the second exchange
            self.assertIn('"second"', backend.prompts[-1])
