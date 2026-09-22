# NovaCore scope and safety boundary

NovaCore is a local **runtime around** an owner-supplied language model. It is
not model training code and it is not a claim that NOVA currently has human
emotions, voice recognition, hardware drivers, or OS-update authority.

## Delivered in this track

- A warm, truthful system prompt for a talking companion.
- A subprocess adapter for an explicitly configured local model executable.
- Conversation context and local lexical recall over owner-approved text notes.
- A machine-readable capability catalogue and append-only event outbox.
- Update proposals that describe a requested change, risks, tests, and rollback
  plan, while explicitly remaining unverified and unapplied.

## Non-negotiable boundary

The language model proposes words. It cannot directly execute commands. A
trusted capability broker validates a named request, applies owner-confirmation
rules, and currently emits an event only. A future Nucleus adapter must consume
only this narrow protocol after K4 implements capabilities.

This separation prevents a prompt injection in conversation or imported text
from becoming raw disk, power, sensor, network, or kernel access.

## Privacy and data handling

Conversation content (the system prompt, memories, history) is assembled once per
turn and delivered to the model runner. On **POSIX systems**, temporary prompt
files (when not using stdin mode) are created in the state directory with
owner-only permissions (`0o600`) and deleted after the runner exits, even on
timeout or crash.

On **Windows**, `os.chmod` does not enforce POSIX permissions; file access is
controlled by ACLs instead. This code does not currently apply ACLs; until it
does, the guarantee on Windows is weaker than on POSIX.

**Unlink is not erasure.** Deleting a file removes the directory entry but does
not destroy the data on SSDs, copy-on-write filesystems, or anything with wear
levelling. If files containing conversation content are critical to erase before
the machine leaves owner control, this code alone is not sufficient. Overwriting
before unlinking provides some defence in depth but is not reliable erasure.

## Model requirement

Bring a locally licensed GGUF model and a local runner, then configure its exact
executable path and arguments in `config.example.toml`. The model runner reads
the UTF-8 prompt either via stdin (preferred) or from a file at `{prompt_file}`,
and writes its reply to standard output. NovaCore launches it without a shell,
does not pass the owner's environment (proxy variables, credentials) to it, and
never downloads weights or contacts a server.

The practical memory limit depends on the selected model, quantisation, context
window, and runner. This code makes no claim that a particular token or parameter
count will fit in 4 GB of RAM.
