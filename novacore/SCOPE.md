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

## Model requirement

Bring a locally licensed GGUF model and a local runner, then configure its exact
executable path and arguments in `config.example.toml`. The model runner must
read the UTF-8 prompt file supplied at `{prompt_file}` and write its reply to
standard output. NovaCore launches it without a shell and never downloads
weights or contacts a server.

The practical memory limit depends on the selected model, quantisation, context
window, and runner. This code makes no claim that a particular token or parameter
count will fit in 4 GB of RAM.
