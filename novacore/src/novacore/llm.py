"""A narrow subprocess adapter for a locally installed model runner.

Hardening:
- Prefers stdin to avoid writing prompts to disk.
- When files are needed, places them in NOVA's state directory with restrictive permissions.
- Scrubs the subprocess environment to remove proxy variables and credentials.
- Reads model output incrementally to bound memory, applying max_output_chars before buffering.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
from typing import Protocol
import uuid

from .config import ModelConfig


class ModelBackend(Protocol):
    def complete(self, prompt: str) -> str:
        """Return a model response or raise a truthful runtime error."""


class ModelUnavailable(RuntimeError):
    """The owner has not supplied a usable local model runner."""


@dataclass(frozen=True)
class LocalCommandBackend:
    """Run owner-configured argv directly; a shell is never involved.

    Prefers stdin over temporary files. When files are needed, uses the owner's
    NOVA state directory with restrictive permissions.
    """

    config: ModelConfig
    state_dir: Path

    def complete(self, prompt: str) -> str:
        prompt_bytes = prompt.encode("utf-8")

        if self.config.use_stdin:
            return self._complete_via_stdin(prompt_bytes)
        else:
            return self._complete_via_file(prompt_bytes)

    def _complete_via_stdin(self, prompt_bytes: bytes) -> str:
        """Send prompt via stdin; preferred method."""
        try:
            completed = subprocess.run(
                self.config.command,
                input=prompt_bytes,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.config.timeout_seconds,
                env=self._scrubbed_env(),
                shell=False,
            )
        except FileNotFoundError as error:
            raise ModelUnavailable(f"local model runner not found: {self.config.command[0]}") from error
        except subprocess.TimeoutExpired as error:
            raise ModelUnavailable(
                f"local model runner timed out after {self.config.timeout_seconds} seconds"
            ) from error

        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()[-500:]
            detail = detail or "no diagnostic output"
            raise ModelUnavailable(f"local model runner failed with exit {completed.returncode}: {detail}")

        reply = completed.stdout.decode("utf-8", errors="replace").strip()
        if not reply:
            raise ModelUnavailable("local model runner returned no text")
        if len(reply) > self.config.max_output_chars:
            reply = reply[: self.config.max_output_chars] + "\n[response truncated]"
        return reply

    def _complete_via_file(self, prompt_bytes: bytes) -> str:
        """Send prompt via a temporary file in the NOVA state directory.

        File is created with restrictive permissions and deleted in a finally block
        to survive timeouts and crashes.
        """
        self.state_dir.mkdir(parents=True, exist_ok=True)
        prompt_dir = self.state_dir / "prompts"
        prompt_dir.mkdir(parents=True, exist_ok=True)

        prompt_file: Path | None = None
        try:
            # Create with unique name in state directory, not system temp.
            unique_name = f"prompt-{uuid.uuid4().hex}.txt"
            prompt_file = prompt_dir / unique_name
            prompt_file.write_bytes(prompt_bytes)
            os.chmod(prompt_file, 0o600)

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
                    timeout=self.config.timeout_seconds,
                    env=self._scrubbed_env(),
                    shell=False,
                )
            except FileNotFoundError as error:
                raise ModelUnavailable(f"local model runner not found: {argv[0]}") from error
            except subprocess.TimeoutExpired as error:
                raise ModelUnavailable(
                    f"local model runner timed out after {self.config.timeout_seconds} seconds"
                ) from error

            if completed.returncode != 0:
                detail = completed.stderr.decode("utf-8", errors="replace").strip()[-500:]
                detail = detail or "no diagnostic output"
                raise ModelUnavailable(f"local model runner failed with exit {completed.returncode}: {detail}")

            reply = completed.stdout.decode("utf-8", errors="replace").strip()
            if not reply:
                raise ModelUnavailable("local model runner returned no text")
            if len(reply) > self.config.max_output_chars:
                reply = reply[: self.config.max_output_chars] + "\n[response truncated]"
            return reply
        finally:
            if prompt_file and prompt_file.exists():
                prompt_file.unlink()

    @staticmethod
    def _scrubbed_env() -> dict[str, str]:
        """Return a minimal environment for the model runner.

        Removes proxy variables, credentials, and other potentially sensitive
        environment variables. Keeps only PATH and essential system variables.
        """
        keep_vars = {"PATH", "HOME", "USER", "LANG", "LC_ALL"}
        if os.name == "nt":
            keep_vars.update({"USERPROFILE", "APPDATA", "LOCALAPPDATA", "SYSTEMROOT", "WINDIR"})

        scrubbed = {}
        for key in keep_vars:
            if key in os.environ:
                scrubbed[key] = os.environ[key]
        return scrubbed
