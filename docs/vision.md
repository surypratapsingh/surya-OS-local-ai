# NOVA — Vision

## One sentence

An operating system that lives on your machine, serves only you, sees and hears and speaks —
fully local, updatable only by a pendrive you control — whose kernel you also wrote.

## Origin story

The owner asked for an AI OS that: updates itself, lives locally, runs required apps, uses the
camera and microphone, uses and optimizes every resource, is smart enough to change itself,
serves the owner absolutely, understands time, reads emotions via the camera, and gets internet
data only via a pendrive — once a day or once a month. This document is the constitution every
later decision must answer to.

## The three laws of NOVA

1. **The owner is sovereign.** NOVA serves the owner. It never obeys a remote party, never
   accepts remote instructions, and treats every unsigned or third-party update as hostile.
2. **Nothing leaves the machine.** No telemetry, no network stack on the AI core path, no
   frames or audio beyond RAM. The only data channel is a pendrive whose packages are signed
   and verified. A letterbox goes *out* only when the owner plugs the stick.
3. **Earned resources.** Every subsystem competes honestly for RAM/CPU/thermal budget via the
   Thrift governor. The AI core adapts its own footprint before it ever degrades the owner's
   experience.

## The long game (kernel-first)

The owner chose the hardest, most honest path: **write the kernel from scratch.** That takes
years, so NOVA runs two tracks in parallel:

- **Track A — Nucleus:** a from-scratch Rust kernel. Milestone by milestone it gains console,
  memory, FAT32 (the pendrive reader!), userspace, USB camera and audio, and finally native
  AI inference (llama.cpp / whisper.cpp / Piper are C/C++ — they port onto a kernel that
  provides an allocator and a VFS).
- **Track B — NovaEyes:** the AI core, experience-first. It runs **today** on any machine so
  the owner lives with the product while the kernel grows underneath it. Every NovaEyes
  module is written as a portable pure function of its inputs — the emotion classifier is
  `(frame) -> emotion` and will re-target onto Nucleus unchanged in spirit, in C++.

The convergence point is **K6**: the full AI core running on the owner's own kernel. That is
the moment the OS is truly, completely theirs.

## What NOVA is not

- Not a cloud product. There is no "account."
- Not a phone-home assistant. There is no server.
- Not a black box. The journal is plain text, the kernel source is the owner's, the update
  channel is a pendrive the owner physically carries.
