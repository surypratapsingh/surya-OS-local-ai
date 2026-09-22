# NovaCore — local talking companion runtime

NovaCore is the beginning of NOVA's talking model layer. It intentionally does
not ship a model, download weights, make network requests, or control a device.
It gives an owner-supplied local model a truthful persona, explicit text memory,
and a narrow future path to capabilities and signed update review.

## What this is now

- A standard-library Python 3.11+ command-line runtime.
- A local subprocess adapter for a model runner such as `llama.cpp`.
- An owner-approved text memory store.
- A `STUB` hardware event outbox with no physical adapter.
- An update-proposal writer; proposals are never applied.

## What this is not

- Model weights, a trainer, a voice-recognition engine, a text-to-speech engine,
  or a hardware driver.
- A general shell agent or a self-modifying OS.
- Evidence that a selected model is emotional, conscious, or human. NovaCore
  aims to sound warm and considerate without making false claims about feelings.

## Run

Copy `config.example.toml` to a private `config.toml`, then set the command to
your installed local model runner. The `{prompt_file}` token must be an argument
of that command.

```powershell
$env:PYTHONPATH = "novacore/src"
& "C:\path\to\python.exe" -m novacore --config novacore/config.toml
```

Use `/help` in the prompt for local commands. `/remember` is the only command
that persists text. `/propose-update` writes a review document only. Runtime
state is created under the configured `data/` directory and is ignored by Git.

## Test

```powershell
$env:PYTHONPATH = "novacore/src"
& "C:\path\to\python.exe" -m unittest discover -s novacore/tests -v
```

See [WORK_ORDER.md](WORK_ORDER.md), [SCOPE.md](SCOPE.md),
[DECISIONS.md](DECISIONS.md), and [VALIDATION.md](VALIDATION.md) for the exact
scope, decisions, and evidence.
