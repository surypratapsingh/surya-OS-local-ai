# Root key ceremony (work order C1)

The owner's Ed25519 root key signs every release manifest
(`docs/manifest-format.md`). It is created on an offline machine, stays
there, and never exists on the NOVA machine or on any machine with a network.

## What it protects, and what it doesn't

**It protects against:**
- a package changed after signing;
- a package added to or removed from a release;
- an old signed release replayed onto a machine that has already accepted a newer one.

**It does not protect against:**
- the owner signing something bad;
- a stolen private key;
- someone with write access to the verifying machine's replay-state file, who can reset it (`docs/manifest-format.md`, "Replay and rollback");
- physical tampering with the boot path (C4, C6).

## The machines

| Machine | Holds | Runs |
|---|---|---|
| Offline machine: never networked, ideally never networked again | `root.priv` | `keygen.py`, `sign-manifest.py` |
| Build machine | the repo, including `kernel/trust/root-key.pub` | `create-manifest.py` |
| NOVA machine | `root-key.pub`, the replay-state file | `verify-manifest.py` |

The tools need Python 3.10+ and nothing else (standard library only). To set
up the offline machine, copy `tools/nova_trust.py`, `tools/keygen.py` and
`tools/sign-manifest.py` to it over USB.

## Once: create the key (offline machine)

```bash
python tools/keygen.py --out-dir /media/encrypted-usb/nova-root-key
```

- It writes `root.priv` (the private key, file mode 0600) and `root.pub`, and prints the fingerprint.
- **Write the fingerprint on paper** and keep the paper away from the key.
- `keygen.py` refuses to overwrite an existing key.
- On Windows the 0600 mode is not applied, so the key is protected only by the folder's permissions. Prefer an encrypted USB drive.

Then copy **only** `root.pub` to the build machine, save it as
`kernel/trust/root-key.pub`, and commit it. Check the fingerprint that
`create-manifest.py` prints against your paper copy.

## Every release

1. **Build machine:** create the unsigned manifest.
   ```bash
   python tools/create-manifest.py --version 0.2.0 --sequence 2 \
       --package mathd:0.2.0:build/mathd.bin \
       --capabilities mathd:compute:expression,compute:verify \
       --output build/release-0.2.0.manifest
   ```
   `--sequence` must be higher than any release ever accepted.

2. **Carry it to the offline machine** on a USB stick, and sign it.
   ```bash
   python tools/sign-manifest.py release-0.2.0.manifest /media/encrypted-usb/nova-root-key/root.priv
   ```
   Before it signs, the tool prints every package with its size and SHA-256.
   Read the list. That is exactly what your signature will vouch for.

3. **Carry it back and verify it** before installing anything from it.
   ```bash
   python tools/verify-manifest.py release-0.2.0.manifest \
       --files /media/stick/release --state /var/nova/accepted.json --accept
   ```
   On a machine's first install, create the state file on purpose first:
   `{"release_sequence": 0}`.

## Keeping the private key

- Store it on an encrypted USB drive that is mounted only on the offline machine, or on the offline machine itself.
- Keep a paper or second-drive backup somewhere physically separate.
- Never put it in email, cloud storage, git, a screenshot, or on the build or NOVA machine.
- Signing uses pure Python, which is **not constant-time**. This is another reason to sign only on the offline machine.

## If something goes wrong

| Event | Do this |
|---|---|
| Key lost, backup intact | Restore it from the backup. |
| Key lost, no backup | Generate a new key, commit the new `root-key.pub`, and re-sign current releases. Old manifests stop verifying. |
| Key possibly exposed | Treat it as stolen: new key, new `root-key.pub`, re-sign, and distrust anything signed after the suspected exposure. |
| `verify-manifest.py` rejects a release | Install nothing from it. Read the FAIL lines: a changed file, a wrong key or a replay each need a different response. |

## Checking the tools themselves

```bash
python tests/test_trust.py     # Ed25519 checked byte for byte against OpenSSL-generated fixtures
python tests/mutate_trust.py   # plants deliberate bugs; every one must be caught
```

The fixtures come from `tests/fixtures/gen_ed25519_openssl.py`, which uses
OpenSSL through the `cryptography` package. That is a dev-machine dependency
only; nothing at run time needs it.
