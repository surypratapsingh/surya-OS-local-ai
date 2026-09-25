//! Interrupt Descriptor Table + CPU exception handlers (K2 gate rework).
//!
//! Every one of the 32 exception vectors has a live gate. Handlers are small
//! assembly stubs (stable toolchain — no `x86-interrupt` ABI) that normalize
//! the stack, record `{vector, error code, CR2, RIP}` via a Rust helper, and
//! then either resume past the faulting instruction (selftest resume mode) or
//! report and halt (default mode — unchanged safety behavior for unexpected
//! faults).
//!
//! Resume mode is armed ONLY by the exception selftest (`excselftest`, run
//! from the `novatest` boot path). An unexpected fault while the shell runs
//! still halts the kernel, exactly as before this rework.
//!
//! Recording slots: slot 0 records a CPU-delivered exception; slot 1 records
//! a software `int n`. `native_seen(v)` / `int_seen(v)` return and CLEAR
//! their slot, so one event can never satisfy two checks. The selftest fires
//! and asserts one trigger at a time, which is what makes a single slot
//! sound: a trigger that fails to fault leaves the slot empty (FAIL), and a
//! spurious fault lands in the slot and mismatches the next assert (FAIL).
//!
//! Resume mode advances the interrupted RIP by ADVANCE_TABLE[vector] bytes —
//! the exact length of the selftest's trigger instruction for that vector —
//! then `iretq`. Traps (v1 #DB single-step, v3 #BP) already resume after the
//! instruction, so their entries are 0. Software-`int` entries never advance
//! (RIP already points after the `int n`). In halt mode nothing advances and
//! the kernel stops.
//!
//! IST routing: #DF -> IST1, #MC -> IST2 (gdt.rs arms the stacks), so gate
//! delivery of those vectors is legal even though neither condition is
//! produced in a QEMU VM. Note the software-dispatch path does NOT exercise
//! IST switching — it enters the same handler code, not the hardware gate
//! delivery path; IST wiring is asserted structurally in `set_gate` calls.
//!
//! Error-code shapes: the CPU pushes an error code on vectors {8,10,11,12,
//! 13,14,17,21} (SDM Vol. 3 table 6-5); on 64-bit everything else pushes
//! none. A software `int n` pushes NO code on any vector (SDM Vol. 3
//! §6.14.2), so the common core synthesizes a zero exactly when the stub's
//! flag bit0 is clear.
//!
//! Frame shapes arriving at `common_exc` (rsp = R on stub entry):
//!   native plain: [rip][cs][rflags][rsp][ss]      (CPU-pushed, 64-bit)
//!   native ec:    [code][rip][cs][rflags][rsp][ss]
//!   software int: [rip][cs][rflags][rsp][ss]      (sint stub synthesizes
//!                  the exact frame a hardware `int n` would push, using the
//!                  `call` return address as rip)
//! The common core pushes a zero code slot when bit0 is clear, making every
//! entry uniform: [code][rip][cs][rflags][rsp][ss]. Vector + flags live in
//! r10/r11 on entry; they are SPILLED below the frame before the Rust record
//! calls (caller-saved registers — the callee may clobber them) and reloaded
//! after.

use core::arch::asm;
use core::arch::global_asm;
use core::mem::size_of;
use core::sync::atomic::{AtomicBool, Ordering};

const VEC_COUNT: usize = 256;
pub const EXC_COUNT: usize = 32;

#[repr(C, packed)]
#[derive(Clone, Copy)]
struct InterruptDescriptor64 {
    offset_low: u16,
    selector: u16,
    ist: u8,
    type_attr: u8,
    offset_mid: u16,
    offset_high: u32,
    zero: u32,
}

impl InterruptDescriptor64 {
    const fn missing() -> Self {
        InterruptDescriptor64 {
            offset_low: 0,
            selector: 0,
            ist: 0,
            type_attr: 0,
            offset_mid: 0,
            offset_high: 0,
            zero: 0,
        }
    }
}

#[repr(C, align(16))]
struct Idt {
    entries: [InterruptDescriptor64; VEC_COUNT],
}

static mut IDT: Idt = Idt {
    entries: [InterruptDescriptor64::missing(); VEC_COUNT],
};

const ATTR_PRESENT_INT: u8 = 0x8E; // present, ring 0, 64-bit interrupt gate

// ---------------------------------------------------------------------------
// Recording slots
// ---------------------------------------------------------------------------

/// slot 0: CPU-delivered (vec, code, cr2, rip); slot 1: software `int n`.
static mut REC: [[u64; 4]; 2] = [[0; 4]; 2];
/// Nonzero while the selftest expects faults (handlers resume, not halt).
#[no_mangle]
static RESUME_MODE: AtomicBool = AtomicBool::new(false);

const REC_CPU: usize = 0;
const REC_INT: usize = 1;

/// Record one CPU-delivered exception. Called from `common_exc`.
unsafe fn record_native(vec: u64, code: u64, rip: u64) {
    let cr2 = read_cr2();
    let r = core::ptr::addr_of_mut!(REC);
    (*r)[REC_CPU] = [vec, code, cr2, rip];
}

/// Record one software `int n`.
unsafe fn record_int(vec: u64, _code: u64, rip: u64) {
    let r = core::ptr::addr_of_mut!(REC);
    (*r)[REC_INT] = [vec, 0, 0, rip];
}

#[inline]
unsafe fn read_cr2() -> u64 {
    let v: u64;
    asm!("mov {}, cr2", out(reg) v, options(nomem, nostack, preserves_flags));
    v
}

/// True if the CPU delivered `vec` since the last call; clears the slot.
pub fn native_seen(vec: u8) -> bool {
    unsafe {
        let r = core::ptr::addr_of_mut!(REC);
        let seen = (*r)[REC_CPU][0] == vec as u64;
        if seen {
            (*r)[REC_CPU] = [0; 4];
        }
        seen
    }
}

/// (error code, cr2) of the recorded native event for `vec`, if still present.
/// Read BEFORE `native_seen(vec)`, which clears the slot.
pub fn native_details(vec: u8) -> Option<(u64, u64)> {
    unsafe {
        let r = core::ptr::addr_of!(REC);
        if (*r)[REC_CPU][0] == vec as u64 {
            Some(((*r)[REC_CPU][1], (*r)[REC_CPU][2]))
        } else {
            None
        }
    }
}

/// True if software `int vec` reached its gate since the last call; clears.
pub fn int_seen(vec: u8) -> bool {
    unsafe {
        let r = core::ptr::addr_of_mut!(REC);
        let seen = (*r)[REC_INT][0] == vec as u64;
        if seen {
            (*r)[REC_INT] = [0; 4];
        }
        seen
    }
}

/// Enable resume mode (selftest only): handlers advance past the trigger
/// instruction instead of halting.
pub fn set_resume_mode(on: bool) {
    RESUME_MODE.store(on, Ordering::SeqCst);
}

// ---------------------------------------------------------------------------
// Common handler core
// ---------------------------------------------------------------------------

/// Halt-mode reporter. Never returns. Same behavior as the pre-rework kernel.
unsafe extern "C" fn fault_report(vec: u64, code: u64, rip: u64) -> ! {
    sprintln!("NOVA_FAULT: vector {} at rip={:#x}", vec, rip);
    sprintln!("  error code: {:#x}", code);
    if vec == 14 {
        sprintln!("  cr2:        {:#x}", read_cr2());
    }
    sprintln!("NOVA: halted.");
    loop {
        asm!("cli; hlt", options(nomem, nostack, preserves_flags));
    }
}

/// Rust wrapper the assembly calls with (rdi=vector, rsi=code, rdx=rip).
unsafe extern "C" fn record_stub(vec: u64, code: u64, rip: u64) {
    record_native(vec, code, rip);
}

unsafe extern "C" fn int_record_stub(vec: u64, _code: u64, rip: u64) {
    record_int(vec, _code, rip);
}

// Flags in r11d, set by the per-vector stubs:
//   bit0 = this entry arrived with a real CPU error code on the stack
//   bit1 = this entry came from the software `int n` dispatcher
//
// common_exc: normalize the stack, then SWITCH to a dedicated handler stack
// (the interrupted stack is treated as read-only from here on) and copy the
// frame over. Everything the handler does — Rust record calls included —
// runs on the handler stack with fxsave/fxrstor around it, so the
// interrupted context's stack and FPU/SSE state survive no matter what the
// Rust code does. The resume path IRETs from the handler stack copy.
global_asm!(
    ".section .text",
    // Working area below TOP once switched (offsets from rsp after
    // `sub rsp, 640`):
    //   +0   vector (spilled)         +8   flags (spilled)
    //   +16  error code               +24  interrupted RSP value
    //   +32..71 frame copy: rip cs rflags rsp ss
    //   +80  512-byte fxsave area (16-aligned: 640 and 80 are 16-mult)
    "common_exc:",
    // Uniform code slot: CPU-pushed where the shape has one, else synthesized.
    "    test r11b, 1",
    "    jnz 1f",
    "    push 0",
    "1:",
    // Frame is now uniform with rsp -> code slot: [rsp]=code, [rsp+8]=rip,
    // [rsp+16]=cs, [rsp+24]=rflags, [rsp+32]=interrupted-rsp, [rsp+40]=ss.
    // Keep the frame base in r9 across the stack switch.
    "    mov r9, rsp",
    // Switch to the handler stack.
    "    lea rax, [rip + exc_handler_stack_top]",
    "    mov rsp, rax",
    "    sub rsp, 640",
    // Spill vector + flags before anything clobbers r10/r11.
    "    mov [rsp + 0], r10",
    "    mov [rsp + 8], r11",
    // Copy the frame off the interrupted stack (source offsets from r9:
    // code at +0, rip +8, cs +16, rflags +24, interrupted-rsp +32, ss +40).
    "    mov rdx, [r9]",
    "    mov [rsp + 16], rdx",
    "    mov rdx, [r9 + 8]",
    "    mov [rsp + 32], rdx",
    "    mov rdx, [r9 + 16]",
    "    mov [rsp + 40], rdx",
    "    mov rdx, [r9 + 24]",
    "    mov [rsp + 48], rdx",
    "    mov rdx, [r9 + 32]",
    "    mov [rsp + 56], rdx",
    "    mov rdx, [r9 + 40]",
    "    mov [rsp + 64], rdx",
    // CR0.TS blocks x87 AND fxsave/fxrstor: clear it before anything else
    // touches FP state (the #NM selftest sets TS; for every other vector
    // clts is a no-op).
    "    clts",
    // Save the interrupted FPU/SSE state on the handler stack.
    "    fxsave [rsp + 80]",
    // Record the event. rdi=vector, rsi=code, rdx=rip (SysV).
    "    mov rdi, [rsp + 0]",
    "    mov rsi, [rsp + 16]",
    "    mov rdx, [rsp + 32]",
    "    test byte ptr [rsp + 8], 2", // software int path records into slot 1
    "    je 3f",
    "    call {rec_int}",
    "    jmp 4f",
    "3:",
    "    call {record}",
    "4:",
    // Restore the interrupted FPU/SSE state.
    "    fxrstor [rsp + 80]",
    // Resume mode?
    "    lea rax, [rip + {resume_on}]",
    "    cmp byte ptr [rax], 0",
    "    jne 5f",
    // Halt mode: report and stop (never returns).
    "    mov rdi, [rsp + 0]",
    "    mov rsi, [rsp + 16]",
    "    mov rdx, [rsp + 32]",
    "    call {report}",
    "    ud2",
    "5:",
    // Resume mode. Software-int entries already carry a complete frame whose
    // RIP points after the (notional) `int n`; nothing to adjust.
    "    test byte ptr [rsp + 8], 2",
    "    jnz 6f",
    // Native entries: v1 #DB must clear TF (bit 8) in the saved RFLAGS, else
    // every following instruction single-steps for the rest of the kernel's
    // life. RFLAGS slot is at +48. (The immediate is the signed imm32 form
    // of 0xfffffeff; the assembler rejects the unsigned hex literal.)
    "    cmp dword ptr [rsp + 0], 1",
    "    jne 7f",
    "    and qword ptr [rsp + 48], -257",
    "7:",
    // Advance RIP by the trigger instruction's length for this vector.
    "    lea rax, [rip + {adv_tab}]",
    "    mov rdx, [rsp + 0]",
    "    movzx eax, byte ptr [rax + rdx]",
    "    add [rsp + 32], rax",
    "6:",
    // IRET from the frame copy: point rsp at the rip slot (+32).
    "    add rsp, 32",
    "    iretq",
    record = sym record_stub,
    rec_int = sym int_record_stub,
    resume_on = sym RESUME_MODE,
    adv_tab = sym ADVANCE_TABLE,
    report = sym fault_report
);

// 4 KiB dedicated handler stack — in .bss because it must be WRITABLE (.text
// is read-only; the first store to a .text-resident stack page-faults and
// triple-faults, which is exactly what the first boot of this design did).
// Single-CPU kernel: one stack is enough; the 4 KiB of headroom covers a
// fault DURING the handler (re-entry reuses the same area — accepted for
// this design, and the record path is a pair of mapped stores that cannot
// fault in practice).
global_asm!(
    ".section .bss.exc_handler_stack, \"aw\"",
    ".balign 16",
    "exc_handler_stack_space:",
    ".skip 4096",
    "exc_handler_stack_top:",
    ".section .text"
);

/// Per-vector RIP advance amounts (bytes) for resume mode. Faults need the
/// trigger instruction's length; TRAPS (v1 #DB single-step, v3 #BP) already
/// resume after the instruction, so their entries are 0. Software-int entries
/// never advance (handled by the bit1 check in common_exc).
///   v0  `div bl`          (F6 F3)       = 2  (fault)
///   v6  `ud2`             (0F 0B)       = 2  (fault)
///   v7  `fldz` w/ TS set  (D9 ED)       = 2  (fault)
///   v11 `mov fs, ax`      (66 8E E0)    = 3  (fault, not-present 0x28;
///                                           the 0x66 operand-size prefix
///                                           makes it THREE bytes — assumed
///                                           2 once and the resume landed
///                                           inside the instruction, which
///                                           decoded as `loopne` into
///                                           unrelated code: the +3 #PF
///                                           storm in the K2 logs)
///   v13 `mov fs, ax`      (66 8E E0)    = 3  (fault, beyond-limit 0x30)
///   v14 `mov rax, [rax]`  (48 8B 00)    = 3  (fault)
///   v16 `wait`            (9B)          = 1  (fault: pending unmasked x87
///                                           exception delivers here)
///   v19 `divps xmm0,xmm1` (0F 5E C1)    = 3  (fault)
#[no_mangle]
pub static ADVANCE_TABLE: [u8; EXC_COUNT] = {
    let mut t = [0u8; EXC_COUNT];
    t[0] = 2;
    t[6] = 2;
    t[7] = 2;
    t[11] = 3;
    t[13] = 3;
    t[14] = 3;
    t[16] = 1;
    t[19] = 3;
    t
};

// ---------------------------------------------------------------------------
// Per-vector stubs (r10d = vector, r11d = flags, jmp common_exc)
// ---------------------------------------------------------------------------

macro_rules! stub {
    ($name:ident, $vec:literal, $flags:literal) => {
        global_asm!(concat!(
            ".globl ",
            stringify!($name),
            "\n",
            stringify!($name),
            ":\n",
            "    mov r10d, ",
            stringify!($vec),
            "\n",
            "    mov r11d, ",
            stringify!($flags),
            "\n",
            "    jmp common_exc\n"
        ));
    };
}

// Native entries: flag bit0 set on error-code vectors (SDM Vol. 3 table 6-5).
stub!(vec0, 0, 0); // #DE divide error
stub!(vec1, 1, 0); // #DB debug
stub!(vec2, 2, 0); // NMI
stub!(vec3, 3, 0); // #BP breakpoint
stub!(vec4, 4, 0); // #OF overflow
stub!(vec5, 5, 0); // #BR bound range exceeded
stub!(vec6, 6, 0); // #UD invalid opcode
stub!(vec7, 7, 0); // #NM device not available
stub!(vec8, 8, 1); // #DF double fault (IST1)
stub!(vec9, 9, 0); // legacy #MF (never delivered on x86-64)
stub!(vec10, 10, 1); // #TS invalid TSS
stub!(vec11, 11, 1); // #NP segment not present
stub!(vec12, 12, 1); // #SS stack-segment fault
stub!(vec13, 13, 1); // #GP general protection fault
stub!(vec14, 14, 1); // #PF page fault
stub!(vec15, 15, 0); // reserved (Intel: do not use; gated anyway)
stub!(vec16, 16, 0); // #MF x87 FPE
stub!(vec17, 17, 1); // #AC alignment check
stub!(vec18, 18, 0); // #MC machine check (IST2)
stub!(vec19, 19, 0); // #XM SIMD floating point
stub!(vec20, 20, 1); // #CP control protection
stub!(vec21, 21, 1); // reserved (ec-shaped in the SDM table)
stub!(vec22, 22, 0); // reserved
stub!(vec23, 23, 0); // reserved
stub!(vec24, 24, 0); // reserved
stub!(vec25, 25, 0); // reserved
stub!(vec26, 26, 0); // reserved
stub!(vec27, 27, 0); // reserved
stub!(vec28, 28, 0); // reserved
stub!(vec29, 29, 0); // reserved
stub!(vec30, 30, 0); // reserved
stub!(vec31, 31, 0); // reserved

// Software `int n` entries. These are CALLed as ordinary functions from
// `int_dispatch` (below), so on entry [rsp] holds the return address. The
// stub turns that into the exact frame a hardware `int n` would push
// (SDM Vol. 3 §6.14.2: rip, cs, rflags, rsp, ss — no error code), so the
// common core handles both entry classes identically. r9/rcx/rax are
// caller-saved scratch; the resumed context (the Rust caller) only expects
// callee-saved registers preserved, which iretq's frame guarantees.
macro_rules! int_stub {
    ($name:ident, $vec:literal) => {
        global_asm!(concat!(
            ".globl ",
            stringify!($name),
            "\n",
            stringify!($name),
            ":\n",
            "    pop rax\n",     // return address; rsp back to caller's top
            "    mov r9, rsp\n", // the RSP to restore
            "    mov r10d, ",
            stringify!($vec),
            "\n",
            "    mov r11d, 2\n", // flags: software-int entry
            "    pushfq\n",
            "    pop rcx\n",   // current RFLAGS
            "    push 0x10\n", // SS   = gdt::KERNEL_DATA
            "    push r9\n",   // RSP
            "    push rcx\n",  // RFLAGS
            "    push 0x08\n", // CS   = gdt::KERNEL_CODE
            "    push rax\n",  // RIP
            "    jmp common_exc\n"
        ));
    };
}

macro_rules! int_stubs {
    ($($name:ident, $vec:literal;)*) => {
        $(int_stub!($name, $vec);)*
    };
}

int_stubs! {
    sint_0, 0;
    sint_1, 1;
    sint_2, 2;
    sint_3, 3;
    sint_4, 4;
    sint_5, 5;
    sint_6, 6;
    sint_7, 7;
    sint_8, 8;
    sint_9, 9;
    sint_10, 10;
    sint_11, 11;
    sint_12, 12;
    sint_13, 13;
    sint_14, 14;
    sint_15, 15;
    sint_16, 16;
    sint_17, 17;
    sint_18, 18;
    sint_19, 19;
    sint_20, 20;
    sint_21, 21;
    sint_22, 22;
    sint_23, 23;
    sint_24, 24;
    sint_25, 25;
    sint_26, 26;
    sint_27, 27;
    sint_28, 28;
    sint_29, 29;
    sint_30, 30;
    sint_31, 31;
}

extern "C" {
    fn vec0();
    fn vec1();
    fn vec2();
    fn vec3();
    fn vec4();
    fn vec5();
    fn vec6();
    fn vec7();
    fn vec8();
    fn vec9();
    fn vec10();
    fn vec11();
    fn vec12();
    fn vec13();
    fn vec14();
    fn vec15();
    fn vec16();
    fn vec17();
    fn vec18();
    fn vec19();
    fn vec20();
    fn vec21();
    fn vec22();
    fn vec23();
    fn vec24();
    fn vec25();
    fn vec26();
    fn vec27();
    fn vec28();
    fn vec29();
    fn vec30();
    fn vec31();
    fn sint_0();
    fn sint_1();
    fn sint_2();
    fn sint_3();
    fn sint_4();
    fn sint_5();
    fn sint_6();
    fn sint_7();
    fn sint_8();
    fn sint_9();
    fn sint_10();
    fn sint_11();
    fn sint_12();
    fn sint_13();
    fn sint_14();
    fn sint_15();
    fn sint_16();
    fn sint_17();
    fn sint_18();
    fn sint_19();
    fn sint_20();
    fn sint_21();
    fn sint_22();
    fn sint_23();
    fn sint_24();
    fn sint_25();
    fn sint_26();
    fn sint_27();
    fn sint_28();
    fn sint_29();
    fn sint_30();
    fn sint_31();
}

unsafe fn set_gate(vec: u8, addr: u64, ist: u8) {
    let idt = core::ptr::addr_of_mut!(IDT);
    (*idt).entries[vec as usize] = InterruptDescriptor64 {
        offset_low: addr as u16,
        selector: crate::gdt::KERNEL_CODE,
        ist,
        type_attr: ATTR_PRESENT_INT,
        offset_mid: (addr >> 16) as u16,
        offset_high: (addr >> 32) as u32,
        zero: 0,
    };
}

/// Fire software interrupt `vec` (0..=31) through its gate's entry stub.
/// The stub synthesizes the hardware `int n` frame and routes through the
/// same common core as a CPU-delivered exception, recording into the
/// software slot and resuming without advancing RIP. Exposed so the
/// selftest can drive every gate without 32 inline asm blocks.
#[inline(never)]
pub fn int_dispatch(vec: u8) {
    assert!(
        (vec as usize) < EXC_COUNT,
        "int_dispatch: vector out of range"
    );
    unsafe {
        match vec {
            0 => sint_0(),
            1 => sint_1(),
            2 => sint_2(),
            3 => sint_3(),
            4 => sint_4(),
            5 => sint_5(),
            6 => sint_6(),
            7 => sint_7(),
            8 => sint_8(),
            9 => sint_9(),
            10 => sint_10(),
            11 => sint_11(),
            12 => sint_12(),
            13 => sint_13(),
            14 => sint_14(),
            15 => sint_15(),
            16 => sint_16(),
            17 => sint_17(),
            18 => sint_18(),
            19 => sint_19(),
            20 => sint_20(),
            21 => sint_21(),
            22 => sint_22(),
            23 => sint_23(),
            24 => sint_24(),
            25 => sint_25(),
            26 => sint_26(),
            27 => sint_27(),
            28 => sint_28(),
            29 => sint_29(),
            30 => sint_30(),
            _ => sint_31(),
        }
    }
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

/// Arm all 32 exception gates and load the IDT. `gdt::init()` must run first
/// (the gates reference its code selector; #DF/#MC reference its IST stacks).
pub fn init() {
    unsafe {
        set_gate(0, vec0 as *const () as usize as u64, 0);
        set_gate(1, vec1 as *const () as usize as u64, 0);
        set_gate(2, vec2 as *const () as usize as u64, 0);
        set_gate(3, vec3 as *const () as usize as u64, 0);
        set_gate(4, vec4 as *const () as usize as u64, 0);
        set_gate(5, vec5 as *const () as usize as u64, 0);
        set_gate(6, vec6 as *const () as usize as u64, 0);
        set_gate(7, vec7 as *const () as usize as u64, 0);
        set_gate(8, vec8 as *const () as usize as u64, 1); // #DF -> IST1
        set_gate(9, vec9 as *const () as usize as u64, 0);
        set_gate(10, vec10 as *const () as usize as u64, 0);
        set_gate(11, vec11 as *const () as usize as u64, 0);
        set_gate(12, vec12 as *const () as usize as u64, 0);
        set_gate(13, vec13 as *const () as usize as u64, 0);
        set_gate(14, vec14 as *const () as usize as u64, 0);
        set_gate(15, vec15 as *const () as usize as u64, 0);
        set_gate(16, vec16 as *const () as usize as u64, 0);
        set_gate(17, vec17 as *const () as usize as u64, 0);
        set_gate(18, vec18 as *const () as usize as u64, 2); // #MC -> IST2
        set_gate(19, vec19 as *const () as usize as u64, 0);
        set_gate(20, vec20 as *const () as usize as u64, 0);
        set_gate(21, vec21 as *const () as usize as u64, 0);
        set_gate(22, vec22 as *const () as usize as u64, 0);
        set_gate(23, vec23 as *const () as usize as u64, 0);
        set_gate(24, vec24 as *const () as usize as u64, 0);
        set_gate(25, vec25 as *const () as usize as u64, 0);
        set_gate(26, vec26 as *const () as usize as u64, 0);
        set_gate(27, vec27 as *const () as usize as u64, 0);
        set_gate(28, vec28 as *const () as usize as u64, 0);
        set_gate(29, vec29 as *const () as usize as u64, 0);
        set_gate(30, vec30 as *const () as usize as u64, 0);
        set_gate(31, vec31 as *const () as usize as u64, 0);
        load();
    }
}

unsafe fn load() {
    let idt = core::ptr::addr_of!(IDT);
    let ptr = DescriptorTablePointer {
        limit: (size_of::<Idt>() - 1) as u16,
        base: idt as u64,
    };
    asm!("lidt [{}]", in(reg) &ptr, options(readonly, nostack, preserves_flags));
}

#[repr(C, packed)]
struct DescriptorTablePointer {
    limit: u16,
    base: u64,
}
