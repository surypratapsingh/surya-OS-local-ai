# K6 Architectural Design: Running `mathd` Unchanged on Nucleus

## Executive Summary

Work order K6 requires running the complete `mathd` test suite unchanged on Nucleus.
`mathd` is a pure `std`-only Rust library with zero external crate dependencies,
implementing symbolic algebra, calculus, differentiation, and verification algorithms.

Running `mathd` on Nucleus requires providing the runtime environment that Rust's standard
library (`std`) expects: dynamic memory allocation (`alloc`), basic standard I/O for test
reporting (`write` to stdout/stderr), filesystem access for corpus and fixture reading (`read`,
`open`, `close`), high-resolution benchmarking timers (`std::time::Instant`), thread-local
storage (`TLS`), and clean process exit (`exit`/`exit_group`).

This document analyzes and compares the two primary architectural approaches:
1. **Option A: Static `x86_64-unknown-linux-musl` binary over a Linux-compatible syscall layer**
2. **Option B: Custom Rust target specification (`x86_64-unknown-nucleus`) with custom std runtime**

Neither option has been started in code. This document presents the technical analysis,
trade-offs, required syscall surface, and a concrete recommendation for decision by the
lead and the owner.

---

## Technical Context: What `mathd` Requires from the Runtime

A comprehensive audit of `mathd` shows that it requires:
1. **Dynamic Memory Allocation**: Standard heap allocation (`Box`, `Vec`, `String`, `HashMap`,
   `BTreeMap`). Handled by the runtime's global allocator (`malloc`/`mmap`/`brk`).
2. **Console Output**: Writing test results and mistake ledgers to standard output (file
   descriptor 1) and standard error (file descriptor 2).
3. **Filesystem Input**: Reading corpus files (`mathd/corpus/domain_claims.jsonl`) and test
   fixtures (`mathd/fixtures/*.json`) from disk.
4. **Time Measurement**: Wall-clock / monotonic timestamping (`std::time::Instant`) for
   benchmarks and timeout detection.
5. **Thread-Local Storage**: Rust's standard library and memory allocators maintain thread-local
   structures via the x86-64 `FS` segment base register (`%fs:0`).
6. **Process Termination**: Reporting test suite success or failure via process exit status code.

---

## Detailed Comparison: Option A vs Option B

### Option A: Static `x86_64-unknown-linux-musl` over Linux Syscall Compatibility

In this approach, `mathd` (and its test runner) is compiled using standard Rust for target
`x86_64-unknown-linux-musl`. Musl libc is statically linked into a single self-contained ELF64
binary. The binary executes in Ring 3 on Nucleus and issues standard x86-64 `syscall` instructions.
Nucleus's `syscall` handler dispatches these calls through its capability gating subsystem.

#### Required Syscall Surface (x86-64 Linux ABI)

A static single-threaded musl binary running `mathd`'s test harness requires exactly 12 core
syscalls:

| Syscall # | Syscall Name | Calling Parameters | Purpose in `mathd` / Musl Runtime |
|:---|:---|:---|:---|
| **0** | `sys_read` | `(int fd, char *buf, size_t count)` | Read corpus and fixture files from FAT32 pendrive |
| **1** | `sys_write` | `(int fd, const char *buf, size_t count)` | Write test runner progress and results to stdout/stderr |
| **2** | `sys_open` | `(const char *filename, int flags, mode_t mode)` | Open corpus JSONL and fixture JSON files (read-only) |
| **3** | `sys_close` | `(int fd)` | Close file descriptors |
| **9** | `sys_mmap` | `(void *addr, size_t len, int prot, int flags, int fd, off_t off)` | Anonymous memory allocation for musl's `malloc` |
| **11** | `sys_munmap` | `(void *addr, size_t len)` | Memory deallocation |
| **12** | `sys_brk` | `(void *addr)` | Traditional heap expansion (or return current brk) |
| **60** | `sys_exit` | `(int status)` | Process termination with exit code |
| **158** | `sys_arch_prctl` | `(int code, unsigned long addr)` | Set `%fs` base register (`ARCH_SET_FS = 0x1002`) for TLS |
| **218** | `sys_set_tid_address` | `(int *tidptr)` | Musl thread ID initialization (stub returns PID) |
| **228** | `sys_clock_gettime` | `(clockid_t clk_id, struct timespec *tp)` | Monotonic clock for `std::time::Instant` |
| **231** | `sys_exit_group` | `(int status)` | Process group termination with exit code |

*Fallback / Query Syscalls (harmless failure returns):*
- `sys_ioctl (16)`: Query terminal attributes (`TIOCGWINSZ`). Returning `-ENOTTY` causes musl
  to assume standard unbuffered or line-buffered output.
- `sys_fstat (5)` / `sys_newfstatat (262)`: Return file size and mode for opened files.
- `sys_mprotect (10)`: Memory protection changes (can return success for readable/writable pages).
- `sys_getrandom (318)` / `sys_prlimit64 (302)`: Returning `-ENOSYS` allows musl to fall back to
  default limits and user-space pseudo-random seeding.

#### Advantages of Option A
1. **Toolchain Independence & Stability**: Builds with standard, official stable Rust toolchain
   (`rustup target add x86_64-unknown-linux-musl`). No nightly compiler, no `-Z build-std`, no
   experimental toolchain components.
2. **Host-to-Guest Determinism**: The exact binary tested in CI under Linux or WSL can be copied
   directly onto the test pendrive and executed under Nucleus without recompilation or translation.
3. **Synergy with Work Orders**: Work orders K6-0 (file syscalls) and K6-1 (memory syscalls)
   directly implement the exact syscalls needed for Option A.
4. **Type-Enforced Capability Gating**: Every syscall passes through Nucleus's capability checks
   (`cap.rs`): `sys_open` requires `Cap::FsRead`, `sys_write` to disk requires `Cap::FsWrite`,
   and hardware I/O remains completely inaccessible to Ring 3.

#### Risks and Mitigations for Option A
- **Risk 1: Musl TLS Setup**: Musl libc requires `arch_prctl(ARCH_SET_FS, addr)` during startup.
  *Mitigation*: The x86-64 architecture provides `MSR_FS_BASE (0xC0000100)`. Nucleus sets this
  MSR with a single `wrmsr` instruction during `sys_arch_prctl`.
- **Risk 2: Unimplemented Syscall Panic**: If musl issues an unhandled syscall, an unhandled fault
  could occur.
  *Mitigation*: Nucleus's syscall dispatcher logs unhandled syscall numbers and returns `-ENOSYS`
  (`-38`), allowing standard libc fallbacks.

---

### Option B: Custom Rust Target Specification (`x86_64-unknown-nucleus`)

In this approach, a custom target JSON is defined (`x86_64-unknown-nucleus.json`), and Rust's
standard library is compiled from source specifically for Nucleus using Cargo's `-Z build-std`
feature. Syscalls are custom-designed for Nucleus rather than mimicking Linux.

#### Architecture of Option B
- `x86_64-unknown-nucleus.json`: Defines data layout, CPU features, ABI, and OS type ("none" or "nucleus").
- Syscall interface: Uses custom Nucleus-specific syscall numbers (e.g., syscall 1 = Alloc,
  syscall 2 = Console, syscall 3 = FileRead).
- Runtime library: Requires either maintaining a fork of `rust-lang/rust`'s `library/std/src/sys/`
  or linking against a custom C/Rust shim library providing libc interfaces.

#### Advantages of Option B
1. **Clean Kernel Interface**: No need to maintain POSIX/Linux errno codes, complex ioctl structures,
   or historical Linux ABI quirks.
2. **Explicit Capability Semantics**: Syscalls can carry capability tokens directly in registers
   rather than relying on ambient Linux file descriptor tables.

#### Risks and Disadvantages of Option B
1. **Requires Nightly Compiler**: Building `std` from source via `-Z build-std` is permanently
   nightly-only in Rust. NOVA's repository invariants (`rust-toolchain.toml`, `AGENTS.md`)
   require stable Rust. Moving to nightly introduces toolchain drift, breakage across compiler
   versions, and reproducible build hazards (violating C5).
2. **Maintenance Burden**: Every upstream Rust version updates internal structures within
   `std::sys`. Maintaining an out-of-tree `std` target requires ongoing upstream tracking.
3. **Build Friction**: Developers and CI would need nightly toolchains and prolonged build times
   (rebuilding `std` from source on every clean build).

---

## Detailed Trade-off Matrix

| Criterion | Option A: Static Musl over Linux ABI | Option B: Custom Target (`build-std`) |
|:---|:---|:---|
| **Rust Toolchain** | **Stable** (`x86_64-unknown-linux-musl`) | **Nightly** (`-Z build-std` required) |
| **Reproducibility (C5)** | High (pre-built musl target in rustup) | Low (recompiling std from source) |
| **Syscall Count** | ~12 standard Linux syscalls | ~8 custom Nucleus syscalls |
| **Standard Library Code** | Zero kernel-side std code | Extensive custom `sys/` backend |
| **Testing Oracle** | Run identical binary on Linux & Nucleus | Binary only runs on Nucleus |
| **Maintenance Burden** | Minimal (Linux ABI is frozen & stable) | High (tracks internal Rust std changes) |
| **Capability Enforcement**| Gated at syscall dispatch | Gated at syscall dispatch |

---

## Recommendation

**Recommendation: Option A (Static `x86_64-unknown-linux-musl` binary over Linux syscall layer).**

Reasons:
1. **Maintains Project Invariants**: Option A runs on **stable Rust**, adhering to NOVA's
   toolchain pin and avoiding the risks of nightly compiler instability.
2. **Direct Alignment with Roadmap**: Work order K6-0 already specifies implementing read-only
   file syscalls (`open`, `read`, `close`), and K6-1 specifies memory syscalls (`mmap`, `brk`).
   Option A directly consumes these work orders without creating redundant custom interfaces.
3. **Verification Oracle**: Using the standard Linux ABI allows the identical test binary to be
   run on the host runner (Linux/musl) and inside Nucleus under QEMU, enabling exact byte-for-byte
   oracle comparison of outputs and mistake ledgers.

---

## Decision Authority

Per the standing brief, this design is submitted for review and selection.
Neither option will be started until the lead (Claude Code) and the owner choose and record
their decision in `docs/decisions.md` and `docs/team.md`.
Work orders K6-0 and K6-1 implement the foundational syscalls (`open`, `read`, `close`, `brk`, `mmap`)
that are required under either choice.
