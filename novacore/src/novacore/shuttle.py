"""NOVA Shuttle importer (work order E2).

Verifies offline shuttle packages transported via pendrive before creating an
update proposal in UpdateProposalStore. Fails closed: an unverified or tampered
package writes nothing to the proposal store, leaving the store byte-unchanged.
Proposals remain unverified and unapplied (applied = False) until the owner explicitly
confirms and the kernel capability boundary installs them.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

# Locate tools/nova_trust.py if not already in sys.path
_tools_dir = Path(__file__).resolve().parent.parent.parent.parent / "tools"
if _tools_dir.is_dir() and str(_tools_dir) not in sys.path:
    sys.path.insert(0, str(_tools_dir))

import nova_trust as nt  # noqa: E402

from .updates import UpdateProposalStore


class ShuttleVerificationError(ValueError):
    """Raised when a shuttle package fails verification."""

    def __init__(self, message: str, failures: list[str] | None = None) -> None:
        super().__init__(message)
        self.failures = failures or []


class ShuttleImporter:
    """Verifies and imports shuttle packages into UpdateProposalStore."""

    def __init__(
        self,
        proposal_store: UpdateProposalStore,
        trusted_public_key: bytes | Path,
        state_path: Path | None = None,
    ) -> None:
        self.proposal_store = proposal_store
        self.state_path = Path(state_path) if state_path is not None else None

        if isinstance(trusted_public_key, Path):
            self.trusted_key = nt.parse_public_key_pem(trusted_public_key.read_text(encoding="ascii"))
        elif isinstance(trusted_public_key, (bytes, bytearray)):
            self.trusted_key = bytes(trusted_public_key)
        else:
            raise TypeError(f"trusted_public_key must be bytes or Path, got {type(trusted_public_key)}")

    def verify_package(
        self,
        package_dir: Path,
        manifest_name: str = "release.manifest",
    ) -> tuple[dict[str, Any], list[str]]:
        """Verify package against trusted root key, replay state, and file hashes.

        Returns (parsed_manifest, failures_list).
        """
        package_dir = Path(package_dir)
        manifest_path = package_dir / manifest_name
        if not manifest_path.is_file():
            return {}, [f"manifest not found: {manifest_path}"]

        try:
            manifest_text = manifest_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            return {}, [f"cannot read manifest: {e}"]

        try:
            manifest = nt.parse_json(manifest_text)
        except Exception as e:
            return {}, [f"manifest json error: {e}"]

        last_seq = None
        if self.state_path is not None and self.state_path.exists():
            try:
                last_seq = nt.read_state(self.state_path)
            except Exception as e:
                return manifest, [f"cannot read replay state: {e}"]

        failures = nt.verify_manifest(
            m=manifest,
            trusted_public=self.trusted_key,
            last_accepted_sequence=last_seq,
            files_dir=package_dir,
        )
        return manifest, failures

    def import_package(
        self,
        package_dir: Path,
        manifest_name: str = "release.manifest",
    ) -> Path:
        """Verify package and create an UpdateProposal.

        Fails closed: if verification fails for any reason, no proposal is
        created, leaving the proposal store completely untouched.
        Returns the path to the newly written proposal file.
        """
        package_dir = Path(package_dir)
        manifest, failures = self.verify_package(package_dir, manifest_name)

        if failures:
            fail_str = "; ".join(failures)
            raise ShuttleVerificationError(
                f"shuttle package verification failed ({len(failures)} failure(s)): {fail_str}",
                failures=failures,
            )

        release = manifest["release"]
        packages = manifest.get("packages", [])

        title = f"Shuttle update {release['version']} (seq {release['release_sequence']})"
        if len(title) > 200:
            title = title[:197] + "..."

        summary_lines = [
            f"Verified offline shuttle package from {package_dir.name}:",
            f"- Version: {release['version']}",
            f"- Release sequence: {release['release_sequence']}",
            f"- Timestamp: {release.get('timestamp', 'unspecified')}",
            f"- Signer key fingerprint: {manifest['trust']['key_fingerprint']}",
        ]
        if "description" in release:
            summary_lines.append(f"- Description: {release['description']}")

        summary_lines.append("\nVerified Packages:")
        for pkg in packages:
            caps_str = ", ".join(pkg.get("capabilities", [])) or "none"
            summary_lines.append(
                f"  * {pkg['id']} v{pkg['version']}: {pkg['filename']} "
                f"({pkg['size']} bytes, sha256: {pkg['sha256'][:16]}...) [caps: {caps_str}]"
            )

        summary_lines.append(
            "\nVerification: OK (Ed25519 signature valid over canonical manifest, "
            "all payload SHA-256 hashes matched, sequence strictly monotonic)."
        )
        summary = "\n".join(summary_lines)
        if len(summary) > 8000:
            summary = summary[:7990] + "\n..."

        # Only now, after complete verification passes, write to proposal store
        proposal_path = self.proposal_store.create(title=title, summary=summary)
        return proposal_path
