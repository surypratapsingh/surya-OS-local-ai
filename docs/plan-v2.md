# NOVA — Plan v2: one owner, one machine, one AI

This document is the system of record for the refined vision. Everything built from here on
is checked against it. If a change violates a line below, the change is wrong.

## The vision

**NOVA is an individually-owned AI operating system.** One person owns the machine, the
models, and the data — no account, no cloud, no telemetry. A *small code base* holds the
machinery; *intelligence arrives as swappable data* (model weights, indexes, modules) carried
in on a pendrive. The owner is served, not managed: no feeds, no notifications, no internet
browsing by default. Wi-Fi exists but is off unless explicitly wanted.

From a student's perspective: it is not another GUI to learn. It is an assistant that knows
your subjects and hobbies, teaches you, quizzes you, plays your music and games, and asks
you — never the internet — for the things it still needs.

## The three layers

1. **Base** — boots the machine and owns the hardware.
   - Today: Nucleus kernel (from scratch, `kernel/`) boots from the pendrive image.
   - Daily driver (M1): a minimal-Linux image (~300 MB, no daemons, no app store) so every
     camera/audio/USB chip works *now* while the AI claims ~90% of resources. Nucleus grows
     underneath (K2→K6) and replaces Linux at the end.
   - Apple Silicon: VM-only (UTM/QEMU). Bare-metal pendrive boot targets x86-64 machines.

2. **NovaCore** — the AI brain (Track B, `eyes/` → `novacore/`).
   - Small model first (0.5B–1.5B class, Q4), escalating to bigger weights when the owner
     shuttles them in.
   - Skill library, case memory, capability registry, demand list (below).

3. **Modules** — abilities installed on demand: study packs, music, emulators/games, Linux
   apps, OCR, transcription. All signed, all offline, all arriving via the pendrive channel.

## Core mechanisms

### Skill library (no million-token reasoning)
Everything NOVA can do is a **recipe**: a fixed, predefined procedure with slots.
"Study session", "make a quiz from chapter 3", "draw this formula", "play my music",
"run the retro console" are recipes. The model's only job is to *pick the recipe and fill
the slots* — that is a small-model task. New abilities = new recipes = modules.

### Case memory (iterations stop costing reasoning)
When a problem is solved — a maths technique, a config, a how-to — the question, the method,
and the answer are stored as vectors. A similar problem later *replays* the stored solution
instead of re-deriving it. The machine gets faster and cheaper the more you use it.

### Demand list + shuttle protocol ("no more information")
When NOVA lacks knowledge or ability, it says so and **writes a demand entry** to the
pendrive export: exactly what file/model/module it needs, from which URL or pack, in a
step-by-step list the owner can follow on any internet machine. The shuttle pendrive carries
the answer back; everything is ed25519-signed and verified before install. The owner never
opens a terminal for this.

### Study pipeline (the maths walkthrough)
1. Owner shuttles in a `maths-pack` (PDFs, video files, photos of notes).
2. During idle time NOVA indexes: PDFs → text; video → whisper transcription; handwriting →
   OCR. Every chunk becomes an embedding vector on disk (RAG).
3. "Study chapter 3" → `study_session` skill: retrieves the chapter, teaches at the owner's
   level, draws graphs and charts on screen, quizzes, logs weak spots in the journal.
4. Solved problems go to case memory.
5. Gaps ("nothing on matrices beyond class 10") become demand-list entries with URLs.

### Model cascade
Cheap model handles ~90% of requests. When confidence is low, it *flags* the task and asks
the owner to shuttle in a bigger model for that class of work. The OS never needs the big
model to boot or serve basic needs.

### Resource honesty
The minimal-Linux base is what "light" really means: the heaviness of ordinary distros is
their services and stores, not Linux. NOVA's base runs nothing the owner didn't ask for,
so the AI gets the machine. On Nucleus (endgame), the AI owns 100% of it.

## Decisions locked this round

| Decision | Choice | Why |
|---|---|---|
| First deliverable after plan v2 | Pendrive installer (`nova.hdd`) | The plugging-in moment is the product's soul |
| Daily-driver base | Minimal Linux first, Nucleus later | Drivers today, purity tomorrow |
| Study materials | PDFs + video + handwritten | All three, indexing at idle time |
| Apple Silicon | Documented VM-only | Secure Boot can't sign a custom OS |
| Games | Emulator cores as modules | Retro consoles are just software |
| Wi-Fi | Off by default | Distraction minimization is a feature |
