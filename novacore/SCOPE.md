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

## Log integrity

Two logs share one format (`novacore/src/novacore/chainlog.py`): the memory log
`memories.jsonl` and the capability outbox `events.jsonl`. Each is a hash
chain: each record's `chain_hash` is the SHA-256 of the record's other fields
(canonical JSON) followed by the previous record's `chain_hash`; the first
record links to 64 zeros. Every read verifies the whole chain and names the
first bad line. Outbox events also carry a `sequence` that must count up from 1
with no gap.

A chain alone accepts a shorter valid chain, so `<log name>.anchor` next to each
log (`memories.jsonl.anchor`, `events.jsonl.anchor`) holds the record count and
the last `chain_hash`. `MemoryStore.add()` and `CapabilityBroker.request()`
rewrite it (write to a temporary file, then rename) after each append. A read
fails if the count or the head digest disagrees, if the anchor is missing while
the log has records, or if the log is missing while the anchor exists. Neither
`add()` nor `request()` extends a log that does not verify: both raise
`ValueError`. Before R12 the outbox restarted at sequence 1 on a bad line; it
no longer does.

What the anchor protects against: records dropped from the end by a crash, a
tool, a partial copy, or a restore of an old log without its anchor.

What it does not protect against:

- **Anyone who can write the folder.** The chain has no key. Whoever can
  rewrite the log can recompute every digest and rewrite the anchor too. To
  catch that, the owner must keep the count and head digest somewhere else
  (for example, written down) and compare them with the anchor file.
- **Deleting both files.** That reads as a new, empty store.
- **A crash between the append and the anchor write.** The anchor is one record
  behind and every read fails closed. Recovery: read the last line of the log;
  if it is yours, delete the anchor file and run the log's `anchor()`
  (`MemoryStore(path).anchor()` or `CapabilityBroker(path).anchor()`).

The memory read cache is keyed on the log's and the anchor's modification time (ns)
and size. A rewrite of the same size that also restores the modification time
returns the records last verified by that `MemoryStore`. A new `MemoryStore`
still verifies the file.

**Migration.** A log that has no anchor (written before anchors, or by hand) is
refused until the owner checks it and runs, once (write STATE_DIR with forward
slashes; a backslash path breaks the Python string):

    PYTHONPATH=novacore/src py -3 -c "from pathlib import Path; from novacore.memory import MemoryStore; MemoryStore(Path('STATE_DIR/memories.jsonl')).anchor()"
    PYTHONPATH=novacore/src py -3 -c "from pathlib import Path; from novacore.capabilities import CapabilityBroker; CapabilityBroker(Path('STATE_DIR/events.jsonl')).anchor()"

`anchor()` verifies the whole chain first and refuses if an anchor already
exists.

**v1 memory records are refused.** A v1 record has no `chain_hash` (its hash covered
the text only). Reading one raises with its line number. There is no migration:
chaining v1 records now would vouch for an order and timestamps nobody
verified. Re-add those notes by hand.

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
