"""A narrow subprocess adapter for a locally installed model runner."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
import tempfile
from typing import Protocol

from .config import ModelConfig


class ModelBackend(Protocol):
    def complete(self, prompt: str) -> str:
        """Return a model response or raise a truthful runtime error."""


class ModelUnavailable(RuntimeError):
    """The owner has not supplied a usable local model runner."""


@dataclass(frozen=True)
class LocalCommandBackend:
    """Run owner-configured argv directly; a shell is never involved."""

    config: ModelConfig

    def complete(self, prompt: str) -> str:
        with tempfile.TemporaryDirectory(prefix="novacore-prompt-") as directory:
            prompt_file = Path(directory) / "prompt.txt"
            prompt_file.write_text(prompt, encoding="utf-8")
            argv = tuple(
                str(prompt_file) if argument == "{prompt_file}" else argument
                for argument in self.config.command
            )
            try:
                completed = subprocess.run(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.config.timeout_seconds,
                    check=False,
                    shell=False,
                )
            except FileNotFoundError as error:
                raise ModelUnavailable(f"local model runner not found: {argv[0]}") from error
            except subprocess.TimeoutExpired as error:
                raise ModelUnavailable(
                    f"local model runner timed out after {self.config.timeout_seconds} seconds"
                ) from error

        if completed.returncode != 0:
            detail = completed.stderr.strip()[-500:] or "no diagnostic output"
            raise ModelUnavailable(f"local model runner failed with exit {completed.returncode}: {detail}")
        reply = completed.stdout.strip()
        if not reply:
            raise ModelUnavailable("local model runner returned no text")
        if len(reply) > self.config.max_output_chars:
            reply = reply[: self.config.max_output_chars] + "\n[response truncated]"
        return reply
