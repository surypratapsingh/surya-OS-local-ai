//! K2 exception selftest: deliberately trigger the CPU exception vectors.
//!
//! Work-order gate (docs/work-orders.md, Phase A K2): "Every one of the 32
//! exception vectors is deliberately triggered by a test and reports
//! correctly."
//!
//! Two proof classes, both routed through the LIVE IDT gates:
//!
//!   native  — the CPU itself raises the exception (divide by zero, ud2,
//!             single-step, int3, TS-set x87, not-present selector,
//!             beyond-limit selector, unmapped load, unmasked x87 1/0,
//!             unmasked SSE 0/0). Ten vectors fire this way. Each trigger is
//!             fired and checked ONE AT A TIME against a PREDICTION recorded
//!             in the EXPECT table: the vector, and — where the architecture
//!             fixes one — the exact error code (plus CR2 for #PF). The
//!             predictions come from the SDM description of the trigger
//!             instruction, not from the handler's own output. Interleaving
//!             fire+assert is what makes the single recording slot in idt.rs
//!             sound: a trigger that fails to fault leaves the slot empty
//!             (FAIL), and a spurious fault lands in the slot and mismatches
//!             the next assert (FAIL).
//!
//!   dispatch — the other 22 vectors are exercised with a software `int n`
//!             equivalent: `idt::int_dispatch(v)` enters vector v's real
//!             entry stub, which synthesizes the exact frame a hardware
//!             `int n` pushes (SDM Vol. 3 §6.14.2) and runs the gate's real
//!             record/resume path. What is asserted is the gate's
//!             reachability and handler entry — NOT a CPU-generated fault:
//!             several of these vectors have no raisable condition in a VM
//!             (#DF, #MC, #AC, reserved vectors), and claiming otherwise
//!             would be dishonest.
//!
//! Handler contract for triggers: the interrupt round trip preserves only
//! callee-saved registers (+ RIP/CS/RFLAGS/RSP/SS via iretq). Any value that
//! must survive a fault lives in rbx/rbp; values clobbered by design are
//! declared as asm outputs. No allocator: all printing uses sprintln! args,
//! never format!.

use core::arch::asm;

use crate::idt;

/// One asserted prediction per native trigger.
struct Predict {
    vec: u8,
    name: &'static str,
    /// Some((code, cr2)) when the architecture fixes the value. Plain
    /// vectors record a synthesized zero code (the handler's documented
    /// normalization), which is asserted too.
    details: Option<(u64, Option<u64>)>,
    /// True for the one trigger the QEMU TCG CPU cannot deliver (unmasked
    /// SSE exceptions — qemu-project/qemu#215, from Launchpad 1668041;
    /// fires on KVM or real hardware). When it does not fire, the test
    /// records a SKIP — counted in the summary, never treated as a pass.
    /// If it DOES fire (KVM/hardware), the prediction is asserted as usual.
    env_skip_ok: bool,
}

static mut PASS: u32 = 0;
static mut FAIL: u32 = 0;
static mut SKIP: u32 = 0;

fn bump(passed: bool, label: &str) {
    unsafe {
        if passed {
            *core::ptr::addr_of_mut!(PASS) += 1;
        } else {
            *core::ptr::addr_of_mut!(FAIL) += 1;
            sprintln!("  FAIL  {}", label);
        }
    }
}

// ---------------------------------------------------------------------------
// Native triggers (the CPU raises the exception)
// ---------------------------------------------------------------------------
// Encoding lengths feed idt::ADVANCE_TABLE; each trigger's faulting
// instruction is commented with its bytes.

unsafe fn t_de() {
    // div bl with bl = 0 -> #DE, error code 0. (F6 F3, 2 bytes)
    // rbx is reserved by LLVM under code-model=kernel, so it is saved and
    // restored inside the asm rather than declared as an operand. div bl
    // writes only AX (quotient AL, remainder AH).
    asm!(
        "push rbx",
        "xor ebx, ebx",
        "div bl",
        "pop rbx",
        lateout("rax") _,
        options(nostack)
    );
}

unsafe fn t_db() {
    // Set TF, execute one nop: the #DB trap fires after it. (trap: no
    // advance; the common handler clears TF in the saved RFLAGS so the
    // kernel does not single-step for the rest of its life.)
    asm!(
        "pushfq",
        "or qword ptr [rsp], 0x100",
        "popfq",
        "nop",
        options(nostack)
    );
}

unsafe fn t_bp() {
    // int3 -> #BP trap. (CC; trap: no advance)
    asm!("int3", options(nostack));
}

unsafe fn t_ud() {
    // ud2 -> #UD, error code 0. (0F 0B, 2 bytes)
    asm!("ud2", options(nostack));
}

unsafe fn t_nm() {
    // CR0.TS = 1 blocks x87: fldz faults with #NM. (D9 ED, 2 bytes)
    // The CR0 restore values live in r12/r13 (callee-saved, and usable as
    // explicit operands — unlike rbx/rbp, which LLVM reserves under
    // code-model=kernel) because the handler's execution between fault and
    // iretq may clobber caller-saved registers. The handler itself does
    // clts first, so its own Rust code can use SSE.
    let mut cr0: u64;
    asm!("mov {}, cr0", out(reg) cr0, options(nomem, nostack));
    asm!(
        "mov cr0, r12",
        "fldz",             // #NM here; resume advances past it
        "mov cr0, r13",     // TS=0 restored after resume
        in("r12") cr0 | 0x8,
        in("r13") cr0,
        options(nostack)
    );
}

unsafe fn t_np() {
    // mov fs, ax with ax = NP_SEL (P=0 data slot) -> #NP. The selector IS
    // the error code: index 5, TI=0, RPL=0 -> 0x28. Encodes 66 8E E0 —
    // THREE bytes (0x66 prefix); ADVANCE_TABLE t[11] = 3.
    let sel = crate::gdt::NP_SEL as u64;
    asm!("mov fs, ax", in("ax") sel as u16, options(nostack));
}

unsafe fn t_gp() {
    // mov fs, ax with ax = GP_BOGUS_SEL (beyond the GDT limit) -> #GP,
    // code = 0x30 (ext=0, IDT=0, RPL=0, index 6). Encodes 66 8E E0 —
    // THREE bytes (0x66 prefix); ADVANCE_TABLE t[13] = 3.
    let sel = crate::gdt::GP_BOGUS_SEL as u64;
    asm!("mov fs, ax", in("ax") sel as u16, options(nostack));
}

/// Canonical, unmapped address for the #PF trigger: well above anything
/// Limine maps in a <=4 GiB QEMU guest (identity-mapped RAM and the
/// framebuffer live below 0x100000000 / 0xFD00000000), and canonical because
/// bit 63 is clear. A NON-canonical address would raise #GP instead — the
/// reason this constant must stay in the lower canonical half.
const PF_PROBE_ADDR: u64 = 0x0000_7FFF_0000_0000;

unsafe fn t_pf() {
    // mov rax, [rax] where rax = PF_PROBE_ADDR -> #PF, error code 0
    // (supervisor read of a not-present page), CR2 = PF_PROBE_ADDR.
    // (48 8B 00, 3 bytes)
    asm!(
        "mov rax, [rax]",
        in("rax") PF_PROBE_ADDR,
        lateout("rax") _,
        options(nostack)
    );
}

/// x87 control word with only the invalid-operation mask cleared
/// (0x037F default with CW.IM = bit 0 clear). x87 masks are bits 0..5.
static X87_CW_UNMASK_IM: u16 = 0x037E;

unsafe fn t_mf() {
    // 0 / 0 with CW.IM unmasked -> #MF. fdiv st(0), st(1) with BOTH zero
    // raises the INVALID-OPERATION exception (1/0 would be zero-divide,
    // which stays masked in this control word and never faults — the first
    // version of this trigger computed 1/0 and proved nothing). The
    // exception is flagged by fdiv but DELIVERED on the next waiting
    // instruction, so the faulting instruction is `wait` (9B, 1 byte —
    // hence ADVANCE_TABLE t[16] = 1).
    asm!(
        "fninit",            // CW = 0x037F (all masked), x87 stack cleared
        "fldz",              // st0 = 0.0
        "fldz",              // st0 = 0.0, st1 = 0.0
        "fldcw [r12]",       // unmask IM only
        "fdiv st(0), st(1)", // 0/0 -> invalid operation flagged, not delivered
        "wait",              // pending unmasked exception delivers #MF here
        "fninit",            // reset (only reached on resume)
        in("r12") core::ptr::addr_of!(X87_CW_UNMASK_IM),
        options(nostack)
    );
}

/// MXCSR with only the invalid-operation mask cleared. MXCSR reset value is
/// 0x1F80 with mask bits 7..12 set; IM is bit 7 — NOT bit 0 (bit 0 is the
/// read-only IE flag; the pre-rework draft cleared it and proved nothing).
static MXCSR_UNMASK_IM: u32 = 0x1F00;
static MXCSR_DEFAULT: u32 = 0x1F80;

unsafe fn t_xm() {
    // divps xmm0, xmm1 with both zero -> SIMD invalid operation, unmasked
    // via MXCSR -> #XM. (0F 5E C1, 3 bytes)
    asm!(
        "xorps xmm0, xmm0",
        "xorps xmm1, xmm1",
        "ldmxcsr [r12]",    // unmask SIMD invalid operation
        "divps xmm0, xmm1", // 0/0 -> invalid, unmasked -> #XM here
        "ldmxcsr [r13]",    // restore (only reached on resume)
        in("r12") core::ptr::addr_of!(MXCSR_UNMASK_IM),
        in("r13") core::ptr::addr_of!(MXCSR_DEFAULT),
        out("xmm0") _,
        out("xmm1") _,
        options(nostack)
    );
}

// ---------------------------------------------------------------------------
// EXPECT table — the predictions, written before the runs they judge
// ---------------------------------------------------------------------------

fn expect() -> [Predict; 10] {
    [
        Predict { vec: 0, name: "#DE div-by-zero", details: Some((0, None)), env_skip_ok: false },
        Predict { vec: 1, name: "#DB single-step", details: Some((0, None)), env_skip_ok: false },
        Predict { vec: 3, name: "#BP int3", details: Some((0, None)), env_skip_ok: false },
        Predict { vec: 6, name: "#UD ud2", details: Some((0, None)), env_skip_ok: false },
        Predict { vec: 7, name: "#NM TS-set x87", details: Some((0, None)), env_skip_ok: false },
        Predict {
            vec: 11,
            name: "#NP not-present selector",
            details: Some((crate::gdt::NP_SEL as u64, None)),
            env_skip_ok: false,
        },
        Predict {
            vec: 13,
            name: "#GP beyond-limit selector",
            details: Some((crate::gdt::GP_BOGUS_SEL as u64, None)),
            env_skip_ok: false,
        },
        Predict {
            vec: 14,
            name: "#PF unmapped read",
            details: Some((0, Some(PF_PROBE_ADDR))),
            env_skip_ok: false,
        },
        Predict { vec: 16, name: "#MF x87 0/0 unmasked", details: Some((0, None)), env_skip_ok: false },
        Predict { vec: 19, name: "#XM SSE 0/0 unmasked", details: Some((0, None)), env_skip_ok: true },
    ]
}

/// Fire one native trigger in resume mode, then assert the EXPECT entry.
unsafe fn fire_and_check(fire: unsafe fn(), p: &Predict) {
    idt::set_resume_mode(true);
    fire();
    idt::set_resume_mode(false);

    // Peek at the recorded details BEFORE native_seen, which clears the slot.
    let rec = idt::native_details(p.vec);
    let seen = idt::native_seen(p.vec);
    if seen {
        sprintln!("  ok    {}", p.name);
        *core::ptr::addr_of_mut!(PASS) += 1;
    } else if p.env_skip_ok {
        // The CPU did not raise the exception. For the #XM trigger this is
        // the KNOWN QEMU TCG limitation (no unmasked SSE exception delivery,
        // qemu-project/qemu#215): recorded as a SKIP, loudly, and counted.
        *core::ptr::addr_of_mut!(SKIP) += 1;
        sprintln!(
            "  SKIP  {} - no fault delivered; QEMU TCG lacks unmasked SSE",
            p.name
        );
        sprintln!("        exception delivery (qemu#215); fires on KVM/real HW");
    } else {
        *core::ptr::addr_of_mut!(FAIL) += 1;
        sprintln!("  FAIL  {} - no fault delivered", p.name);
    }

    if seen {
        if let Some((want_code, want_cr2)) = p.details {
            let got = rec.unwrap_or((0, 0));
            let ok = got.0 == want_code
                && match want_cr2 {
                    None => true,
                    Some(c) => got.1 == c,
                };
            if ok {
                *core::ptr::addr_of_mut!(PASS) += 1;
            } else {
                *core::ptr::addr_of_mut!(FAIL) += 1;
                sprintln!("  FAIL  {} code/cr2", p.name);
                match want_cr2 {
                    Some(c) => sprintln!(
                        "    predicted code {:#x} cr2 {:#x}  got code {:#x} cr2 {:#x}",
                        want_code, c, got.0, got.1
                    ),
                    None => sprintln!(
                        "    predicted code {:#x}  got code {:#x} cr2 {:#x}",
                        want_code, got.0, got.1
                    ),
                }
            }
        }
    }
}

fn run_native() {
    let preds = expect();
    let fires: [unsafe fn(); 10] = [t_de, t_db, t_bp, t_ud, t_nm, t_np, t_gp, t_pf, t_mf, t_xm];
    for (fire, p) in fires.iter().zip(preds.iter()) {
        unsafe { fire_and_check(*fire, p) };
    }
}

/// Drive every gate (0..=31) through its software-int entry stub and assert
/// each reached its handler. Proves gate + handler wiring for the 22 vectors
/// with no VM-raisable condition — it does NOT claim a CPU-generated fault.
fn run_dispatch() {
    idt::set_resume_mode(true);
    for v in 0u8..32 {
        idt::int_dispatch(v);
        unsafe {
            if idt::int_seen(v) {
                *core::ptr::addr_of_mut!(PASS) += 1;
                sprintln!("  ok    int vec {} (0x{:02x})", v, v);
            } else {
                *core::ptr::addr_of_mut!(FAIL) += 1;
                sprintln!("  FAIL  int gate vec {} (0x{:02x})", v, v);
            }
        }
    }
    idt::set_resume_mode(false);
}

/// Entry point from the `novatest` boot path. Returns (passed, failed).
/// Checks: 10 native (seen) + 10 native (code/cr2 where predicted; 9 of 10
/// can fire under QEMU TCG) + 32 dispatch. Under QEMU TCG exactly one
/// native check (#XM) is skipped as an environment limitation, loudly.
pub fn run() -> (u32, u32) {
    sprintln!("exc gate:   arming resume mode; firing 10 native triggers one at a time");
    run_native();
    sprintln!("exc gate:   dispatching software int n through all 32 gates");
    run_dispatch();
    let (p, f, s) = unsafe {
        (
            *core::ptr::addr_of!(PASS),
            *core::ptr::addr_of!(FAIL),
            *core::ptr::addr_of!(SKIP),
        )
    };
    if s > 0 {
        sprintln!(
            "exc gate:   {} checks passed, {} failed, {} SKIPPED (skips are not passes)",
            p, f, s
        );
    } else {
        sprintln!("exc gate:   {} checks passed, {} failed", p, f);
    }
    (p, f)
}
