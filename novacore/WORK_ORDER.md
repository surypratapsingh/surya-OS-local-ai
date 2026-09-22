# NovaCore W0 — local companion runtime foundation

## Requested outcome

Create an isolated local talking-companion runtime that can use an owner-supplied
local language model, keep explicit owner-approved text memories, prepare (but
never apply) OS-update proposals, and define a narrow future hardware-control
boundary.

## Scope

- New files under `novacore/` only.
- Python 3.11+ standard library only; no package installation, model download,
  training job, telemetry, or network client.
- A local subprocess adapter for a model runner such as `llama.cpp`.
- Text conversation, explicit local memory, an event outbox for future hardware
  adapters, and non-applicable update proposals.
- Unit tests using `unittest` only.

## Deliberate exclusions

- This work order does not train, bundle, download, or claim to create model
  weights. A one-billion-parameter model requires a separate data, training,
  licensing, and hardware project.
- This work order does not modify `kernel/`, boot images, trust keys, or existing
  project documentation.
- This work order does not give a language model direct shell, disk, network,
  camera, microphone, power, or kernel access.
- A proposal is not an update. Applying an update remains blocked until the
  Phase C trust model and K4 capability system exist.

## Acceptance criteria

1. Without a configured local runner, the program clearly says it is unavailable
   rather than inventing a reply.
2. All persistent memories are added by an explicit owner command.
3. Hardware requests are emitted as clearly labelled `STUB` outbox events; no
   real device operation occurs.
4. OS changes become `proposed_unverified` JSON documents only.
5. The standard-library test suite proves the above boundaries.
