# Manifest format, version 1 (work order C2)

A manifest lists every package in a release, with its hash, size, install
order and declared capabilities, and is signed by the owner's root key
(`docs/root-key-ceremony.md`). The implementation is `tools/nova_trust.py`.
`tests/test_trust.py` checks it against OpenSSL and against the signing rule
below. If this file and the code disagree, one of them has a bug.

## Example

```json
{
  "manifest_version": 1,
  "packages": [
    {
      "capabilities": ["compute:expression", "compute:verify"],
      "filename": "mathd.bin",
      "id": "mathd",
      "sequence": 1,
      "sha256": "<64 lowercase hex>",
      "size": 1048576,
      "version": "0.2.0"
    }
  ],
  "release": {
    "description": "optional free text",
    "release_sequence": 7,
    "timestamp": "2026-09-25T10:00:00Z",
    "version": "0.2.0"
  },
  "signature": "<base64 of the 64-byte Ed25519 signature>",
  "trust": {
    "algorithm": "Ed25519",
    "key_fingerprint": "sha256:<SHA-256 of the raw 32-byte public key>"
  }
}
```

## Fields

Every field is required unless marked optional. A field not listed here is an
error anywhere in the manifest. The verifier fails closed: a field it doesn't
understand must not be able to carry meaning past it.

| Field | Rule |
|---|---|
| `manifest_version` | the integer `1` |
| `release.version` | non-empty string |
| `release.release_sequence` | integer ≥ 1, strictly greater than every release this machine has accepted (see Replay) |
| `release.timestamp` | UTC `YYYY-MM-DDTHH:MM:SSZ`. Informational only; never used for replay decisions, because clocks can be set |
| `release.description` | optional string |
| `trust.algorithm` | `"Ed25519"` |
| `trust.key_fingerprint` | `"sha256:"` + 64 lowercase hex, hashing the raw 32-byte public key. It deliberately doesn't hash the PEM file, whose bytes change under CRLF conversion |
| `packages` | non-empty list, in install order |
| `packages[i].id` | `[a-z0-9][a-z0-9._-]*`, at most 64 characters, unique |
| `packages[i].version` | non-empty string |
| `packages[i].filename` | a bare file name `[A-Za-z0-9][A-Za-z0-9._-]*`: no directory part and no leading dot, unique ignoring case (FAT and NTFS are case-insensitive) |
| `packages[i].sha256` | 64 lowercase hex, the SHA-256 of the file |
| `packages[i].size` | integer ≥ 0, the file size in bytes |
| `packages[i].sequence` | exactly `i + 1` (1, 2, 3 … in list order) |
| `packages[i].capabilities` | list of unique `namespace:permission` strings, lowercase `[a-z][a-z0-9_]*`, **no wildcards**. A wildcard is ambient authority, which `AGENTS.md` forbids |
| `signature` | base64 of 64 bytes |

The JSON is parsed strictly. A duplicate key, a non-integer number, `NaN` or
`Infinity` is an error.

## Signing

```
signing input = b"NOVA-MANIFEST-v1\n" + canonical JSON of the manifest without "signature"
canonical JSON = keys sorted, separators "," and ":", no whitespace, ASCII only
                 (Python: json.dumps(m, sort_keys=True, separators=(",", ":"), ensure_ascii=True))
signature = base64( Ed25519-sign(root key, signing input) )      RFC 8032 section 5.1.6
```

The fixed prefix separates domains: a manifest signature can never be valid
for any other message the root key signs. The key order and whitespace in the
file do not matter, because only the canonical form is signed.

`sign-manifest.py` refuses to sign a manifest that breaks any rule above, and
one whose `key_fingerprint` names a different key.

## Verification, in this order

1. `trust.key_fingerprint` equals the fingerprint of the trusted key
   (`kernel/trust/root-key.pub`).
2. The signature verifies over the signing input. Nothing that isn't covered
   by a valid signature is interpreted, so a forged manifest yields exactly one
   failure: the signature.
3. Every field rule above.
4. Replay: `release_sequence` is greater than the last accepted one.
5. With `--files DIR`: each package file exists in `DIR` and matches `size` and `sha256`.

`verify-manifest.py` exits 0 when the manifest is acceptable, 1 when it's
rejected, and 2 on a usage or I/O error.

## Replay and rollback

- The verifying machine keeps a state file, `{"release_sequence": N}`,
  recording the last release it accepted.
- A manifest is accepted only if its `release_sequence` is greater than `N`.
  Equal means a replay; lower means a rollback. Both are refused.
- `--accept` writes the new `N` (atomically, via a temp file and `os.replace`),
  and only after every check has passed. A rejected manifest never moves the
  state.
- A missing state file is an error (exit 2), never an implicit 0, so deleting
  the file doesn't reopen old releases. For a first install, create it on
  purpose: `{"release_sequence": 0}`.
- **Limitation:** the state is only as tamper-proof as the storage it lives on.
  Anyone who can write it can reset it and replay an old signed release.
  Protecting it belongs to C3/C6 (read-only storage, or a counter the boot
  chain protects). It isn't solved here.

## Not covered by this version

- **Enforcing capabilities.** The manifest only declares them. Enforcement is
  the kernel capability table (K4) and the `novacore` broker.
- **Installing (C3) and the boot chain (C4).** Limine and the kernel are
  checked by Limine's own `path#blake2b` hashes and `limine enroll-config`,
  not by this manifest (`docs/audit-2026-09-24.md`).
- **Key rotation.** A new key means committing a new
  `kernel/trust/root-key.pub`, after which manifests signed by the old key stop
  verifying.
