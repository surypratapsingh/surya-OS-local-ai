"""Conversation orchestration with explicit memory and no implicit tool execution."""

from __future__ import annotations

from .config import ConversationConfig
from .llm import ModelBackend, ModelUnavailable
from .memory import Memory, MemoryStore
from .prompts import Turn, build_prompt


class Companion:
    def __init__(
        self,
        backend: ModelBackend,
        memory: MemoryStore,
        config: ConversationConfig,
    ) -> None:
        self.backend = backend
        self.memory = memory
        self.config = config
        self.history: list[Turn] = []

    def reply(self, owner_text: str) -> str:
        owner_text = owner_text.strip()
        if not owner_text:
            return "Please say a little more so I can help."
        recalled = self.memory.search(owner_text, self.config.memory_results)
        prompt = build_prompt(memories=recalled, history=self.history, user_text=owner_text)
        try:
            reply = self.backend.complete(prompt)
        except ModelUnavailable as error:
            reply = f"NOVA cannot reply truthfully: {error}"
        self.history.extend((Turn("owner", owner_text), Turn("nova", reply)))
        maximum = self.config.history_turns * 2
        if len(self.history) > maximum:
            self.history = self.history[-maximum:]
        return reply

    def remember(self, text: str) -> Memory:
        """Persist text only when an explicit owner command calls this method."""
        return self.memory.add(text)
