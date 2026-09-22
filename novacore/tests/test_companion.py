from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from novacore.companion import Companion
from novacore.config import ConversationConfig
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
