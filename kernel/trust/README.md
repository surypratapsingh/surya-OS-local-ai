# Trust root

This directory holds `kernel/trust/root-key.pub`, the owner's Ed25519 public
key in PEM (SubjectPublicKeyInfo) format. It is generated offline by
`tools/keygen.py`.

**It does not exist yet.** The owner hasn't run the ceremony. Until the file
is committed, `tools/verify-manifest.py` has no default key, and no release
can be verified.

- The ceremony: `docs/root-key-ceremony.md`
- The manifest format it verifies: `docs/manifest-format.md`
- Fingerprint: `sha256:` + SHA-256 of the raw 32-byte key (`fingerprint()` in
  `tools/nova_trust.py`). It hashes the key, not the file, so git's line-ending
  conversion cannot change it.

Never put `root.priv` in this directory, or anywhere in the repository.
