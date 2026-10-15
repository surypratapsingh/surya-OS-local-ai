"""A narrow subprocess adapter for a locally installed model runner.

Hardening:
- Prefers stdin (the default) to avoid writing prompts to disk.
- When files are needed, places them in NOVA's state directory with restrictive
  permissions (mode 0o700 on the directory, 0o600 on the file via os.open with
  O_CREAT | O_EXCL). On Windows, POSIX permissions are not enforced by the OS;
  file security relies on user profile DACLs.
- Scrubs the subprocess environment to remove proxy variables and credentials.
- Reads model output incrementally via subprocess.Popen to bound memory,
  applying max_output_chars before buffering and terminating runaway processes.
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import threading
import time
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
        return self._run_command(self.config.command, stdin_data=prompt_bytes)

    def _complete_via_file(self, prompt_bytes: bytes) -> str:
        """Send prompt via a temporary file in the NOVA state directory.

        File is created with restrictive permissions (0o600 on file via os.open
        with O_CREAT | O_EXCL, 0o700 on directory) and deleted in a finally block
        to survive timeouts and crashes.
        """
        self.state_dir.mkdir(parents=True, exist_ok=True)
        prompt_dir = self.state_dir / "prompts"
        prompt_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

        prompt_file: Path | None = None
        try:
            unique_name = f"prompt-{uuid.uuid4().hex}.txt"
            prompt_file = prompt_dir / unique_name
            fd = os.open(str(prompt_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                total_written = 0
                while total_written < len(prompt_bytes):
                    written = os.write(fd, prompt_bytes[total_written:])
                    total_written += written
            finally:
                os.close(fd)

            argv = tuple(
                str(prompt_file) if argument == "{prompt_file}" else argument
                for argument in self.config.command
            )
            return self._run_command(argv, stdin_data=None)
        finally:
            if prompt_file and prompt_file.exists():
                try:
                    prompt_file.unlink()
                except OSError:
                    pass

    def _run_command(self, argv: tuple[str, ...], stdin_data: bytes | None) -> str:
        """Execute model runner command, streaming output with strict character bounding."""
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._scrubbed_env(),
                shell=False,
            )
        except FileNotFoundError as error:
            raise ModelUnavailable(f"local model runner not found: {argv[0]}") from error

        stdin_thread: threading.Thread | None = None
        if stdin_data is not None:
            def _feed_stdin() -> None:
                try:
                    if proc.stdin:
                        proc.stdin.write(stdin_data)
                        proc.stdin.close()
                except OSError:
                    pass

            stdin_thread = threading.Thread(target=_feed_stdin)
            stdin_thread.daemon = True
            stdin_thread.start()

        stderr_chunks: list[bytes] = []
        def _read_stderr() -> None:
            try:
                if proc.stderr:
                    total_err = 0
                    while chunk := proc.stderr.read(1024):
                        stderr_chunks.append(chunk)
                        total_err += len(chunk)
                        if total_err > 4096:
                            break
                    proc.stderr.close()
            except OSError:
                pass

        stderr_thread = threading.Thread(target=_read_stderr)
        stderr_thread.daemon = True
        stderr_thread.start()

        max_chars = self.config.max_output_chars
        stdout_chunks: list[str] = []
        char_count = 0
        truncated = False
        reader_done = threading.Event()

        def _read_stdout() -> None:
            nonlocal char_count, truncated
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            try:
                if proc.stdout:
                    while True:
                        chunk = proc.stdout.read(1024)
                        if not chunk:
                            final_text = decoder.decode(b"", final=True)
                            if final_text:
                                stdout_chunks.append(final_text)
                                char_count += len(final_text)
                            break
                        text = decoder.decode(chunk)
                        if text:
                            stdout_chunks.append(text)
                            char_count += len(text)
                            if char_count >= max_chars:
                                truncated = True
                                break
                    try:
                        proc.stdout.close()
                    except OSError:
                        pass
            except OSError:
                pass
            finally:
                reader_done.set()

        stdout_thread = threading.Thread(target=_read_stdout)
        stdout_thread.daemon = True
        stdout_thread.start()

        deadline = time.monotonic() + self.config.timeout_seconds
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not reader_done.wait(timeout=max(0.0, remaining)):
            self._terminate_process(proc)
            raise ModelUnavailable(
                f"local model runner timed out after {self.config.timeout_seconds} seconds"
            )

        if truncated:
            self._terminate_process(proc)
        else:
            remaining = deadline - time.monotonic()
            try:
                proc.wait(timeout=max(0.0, remaining))
            except subprocess.TimeoutExpired as error:
                self._terminate_process(proc)
                raise ModelUnavailable(
                    f"local model runner timed out after {self.config.timeout_seconds} seconds"
                ) from error

        stderr_thread.join(timeout=0.5)
        if stdin_thread is not None:
            stdin_thread.join(timeout=0.5)

        if not truncated and proc.returncode != 0:
            detail = b"".join(stderr_chunks).decode("utf-8", errors="replace").strip()[-500:]
            detail = detail or "no diagnostic output"
            raise ModelUnavailable(f"local model runner failed with exit {proc.returncode}: {detail}")

        full_reply = "".join(stdout_chunks).strip()
        if not full_reply:
            raise ModelUnavailable("local model runner returned no text")

        if len(full_reply) > max_chars or truncated:
            reply = full_reply[:max_chars] + "\n[response truncated]"
        else:
            reply = full_reply

        return reply

    @staticmethod
    def _terminate_process(proc: subprocess.Popen) -> None:
        """Safely terminate a subprocess, falling back to kill if it does not exit."""
        try:
            proc.terminate()
            proc.wait(timeout=0.5)
        except (subprocess.TimeoutExpired, OSError):
            try:
                proc.kill()
                proc.wait(timeout=0.5)
            except OSError:
                pass

    @staticmethod
    def _scrubbed_env() -> dict[str, str]:
        """Return a minimal environment for the model runner.

        Removes proxy variables, credentials, and other potentially sensitive
        environment variables. Inheriting ambient environment into a process
        that is supposed to have no network authority is a violation of explicit
        capabilities.

        Documented kept variables:
        - PATH: Locates binary executables and dependent dynamic libraries needed
          by the model runner process.
        - HOME: User home directory on POSIX systems; required by runtime linkers,
          compilers, and local model runner configurations.
        - USER: Current username on POSIX systems, inspected by some CLI runtimes.
        - LANG: Default system locale and character encoding (UTF-8).
        - LC_ALL: Comprehensive locale override ensuring UTF-8 encoding across
          all standard input/output streams.

        Windows-specific kept variables (os.name == 'nt'):
        - USERPROFILE: Windows user directory root; required by runtimes to locate
          user cache and local tool installations.
        - APPDATA: Roaming application data path; required by system runtime libraries.
        - LOCALAPPDATA: Local application data path; required by GPU drivers (DirectML,
          CUDA cache) and local tools.
        - SYSTEMROOT: Core Windows directory (e.g. C:\\Windows); required by kernel32
          and core system DLLs to resolve dependencies and launch processes.
        - WINDIR: Legacy alias for SYSTEMROOT; required by certain third-party Windows
          tools and CLI wrappers.
        """
        keep_vars = {"PATH", "HOME", "USER", "LANG", "LC_ALL"}
        if os.name == "nt":
            keep_vars.update({"USERPROFILE", "APPDATA", "LOCALAPPDATA", "SYSTEMROOT", "WINDIR"})

        scrubbed = {}
        for key in keep_vars:
            if key in os.environ:
                scrubbed[key] = os.environ[key]
        return scrubbed
