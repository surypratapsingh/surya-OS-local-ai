//! Kernel GDT + TSS (K2 exception-test gate).
//!
//! The IDT alone is not enough for the K2 gate: a CPU-generated #DF needs a
//! valid TSS to switch to an IST stack, and the exception selftest fires #NP
//! and #GP against real GDT geometry. Layout (selectors):
//!
//! ```text
//! 0x00 null
//! 0x08 kernel code (64-bit, ring 0)
//! 0x10 kernel data
//! 0x18 TSS64 (descriptor occupies entries 3..5)
//! 0x28 deliberately not-present segment  -> native #NP, error code 0x28
//! ```
//!
//! The GDT limit covers exactly entries 0..=5 (limit 0x2F), so selector
//! 0x30 is beyond the limit -> native #GP, error code 0x30.
//!
//! Proven by fire, not by inspection: selectors 0x28 and 0x30 are consumed
//! by `exctest::t_np` / `t_gp`, whose EXPECT entries assert the CPU-pushed
//! error code equals the selector value. Wrong geometry fails the selftest
//! with observed-vs-predicted codes on serial.

use core::arch::asm;
use core::mem::size_of;
use core::ptr;

pub const KERNEL_CODE: u16 = 0x08;
pub const KERNEL_DATA: u16 = 0x10;
pub const TSS_SEL: u16 = 0x18;
/// Not-present descriptor (P=0, type 0): `mov fs, ax(=0x28)` -> #NP, code 0x28.
pub const NP_SEL: u16 = 0x28;
/// One past the GDT limit: `mov fs, ax(=0x30)` -> #GP, code 0x30.
pub const GP_BOGUS_SEL: u16 = 0x30;

const GDT_ENTRIES: usize = 6;
/// Entries 0..=5 -> last valid byte 0x2F.
const GDT_LIMIT: u16 = (GDT_ENTRIES as u16) * 8 - 1;

const CODE64: u64 = 0x00AF_9A00_0000_FFFF;
const DATA64: u64 = 0x00CF_9200_0000_FFFF;
/// Same data descriptor with Present cleared (attr 0x92 -> 0x12). The #NP
/// trigger needs a descriptor that is a VALID DATA type which is merely
/// not-present: an all-zero slot is a system descriptor (S=0) with an
/// illegal type, and loading it raises #GP, not #NP (observed on QEMU and
/// per SDM Vol. 3A §5.10 #GP conditions for MOV to Sreg).
const DATA_NP: u64 = 0x00CF_1200_0000_FFFF;

/// Two IST stacks: IST1 for #DF, IST2 for #MC (see idt.rs gate table).
const IST_SIZE: usize = 64 * 1024;

#[repr(C, align(16))]
struct IstStack([u8; IST_SIZE]);

static mut IST_DF: IstStack = IstStack([0; IST_SIZE]);
static mut IST_MC: IstStack = IstStack([0; IST_SIZE]);

/// Task State Segment, 64-bit layout (SDM Vol. 3, figure 8-10 style: rsp0..2,
/// ist1..7, I/O map base at offset 96).
#[repr(C)]
struct TaskStateSegment {
    reserved0: u32,
    rsp0: u64,
    rsp1: u64,
    rsp2: u64,
    reserved1: u64,
    ist1: u64,
    ist2: u64,
    #[allow(dead_code)] // ist3..7 exist in the layout; only 1 and 2 are armed
    ist3: u64,
    ist4: u64,
    ist5: u64,
    ist6: u64,
    ist7: u64,
    reserved2: u64,
    reserved3: u16,
    iopb: u16,
}

static mut TSS: TaskStateSegment = TaskStateSegment {
    reserved0: 0,
    rsp0: 0,
    rsp1: 0,
    rsp2: 0,
    reserved1: 0,
    ist1: 0,
    ist2: 0,
    ist3: 0,
    ist4: 0,
    ist5: 0,
    ist6: 0,
    ist7: 0,
    reserved2: 0,
    reserved3: 0,
    iopb: size_of::<TaskStateSegment>() as u16, // 104: no I/O permission map
};

static mut GDT: [u64; GDT_ENTRIES] = [0; GDT_ENTRIES];

/// Encode a 64-bit TSS descriptor arithmetically (SDM Vol. 3 §7.2.3, figure
/// 7-4 field positions). No structs, no memory reinterpretation: every field
/// is shifted into position, so a layout surprise cannot corrupt it.
fn encode_tss(base: u64, limit: u32) -> (u64, u64) {
    let lo = ((limit & 0xFFFF) as u64)
        | (base & 0xFFFF) << 16
        | ((base >> 16) & 0xFF) << 32
        // P=1, DPL=0, type=0b1001 (64-bit available TSS) -> attr byte 0x89
        | 0x89u64 << 40
        | (((limit >> 16) & 0x0F) as u64) << 48; // G=0, AVL/L/D=0
    let hi = base >> 32;
    (lo, hi)
}

#[repr(C, packed)]
struct DescriptorTablePointer {
    limit: u16,
    base: u64,
}

/// Install the GDT and TSS. Must run before `idt::init()` (the exception
/// gates reference the TSS IST stacks).
pub fn init() {
    unsafe {
        let tss_base = ptr::addr_of!(TSS) as u64;
        let gdt = ptr::addr_of_mut!(GDT);
        (*gdt)[1] = CODE64;
        (*gdt)[2] = DATA64;
        let tss_limit = (size_of::<TaskStateSegment>() - 1) as u32;
        let (lo, hi) = encode_tss(tss_base, tss_limit);
        (*gdt)[3] = lo;
        (*gdt)[4] = hi;
        (*gdt)[5] = DATA_NP; // not-present DATA descriptor for the #NP test
                             // Readback proof on serial: the descriptor we just wrote must decode
                             // back to the TSS address and limit we encoded (catches any future
                             // encoding regression at boot, in the log, not in a debugger).
        let chk = (*gdt)[3];
        let chk_base = ((chk >> 16) & 0xFF_FFFF) | ((*gdt)[4] << 32);
        let chk_limit = (chk & 0xFFFF) | (((chk >> 48) & 0xF) << 16);
        let chk_attr = (chk >> 40) & 0xFF;
        sprintln!(
            "gdt:         tss desc: base={:#x} limit={:#x} attr={:#x}",
            chk_base,
            chk_limit,
            chk_attr
        );
        (*ptr::addr_of_mut!(TSS)).ist1 = (ptr::addr_of_mut!(IST_DF) as usize + IST_SIZE) as u64;
        (*ptr::addr_of_mut!(TSS)).ist2 = (ptr::addr_of_mut!(IST_MC) as usize + IST_SIZE) as u64;

        let gdt_ptr = DescriptorTablePointer {
            limit: GDT_LIMIT,
            base: ptr::addr_of!(GDT) as u64,
        };
        asm!("lgdt [{}]", in(reg) &gdt_ptr, options(readonly, nostack));

        // Reload CS via a far return; then all data segments. Pushes below
        // use the cached (hidden) descriptor of the old SS, which stays valid
        // until reloaded — the standard reload order. The far return is
        // emitted as its raw encoding (48 CB = REX.W lret) because LLVM's
        // integrated assembler does not accept the `lretq` mnemonic.
        asm!(
            "push {code}",
            "lea r10, [rip + 2f]",
            "push r10",
            ".byte 0x48, 0xCB", // lretq: pop RIP (=2f), pop CS (={code})
            "2:",
            "mov ds, ax",
            "mov es, ax",
            "mov ss, ax",
            "mov fs, ax",
            "mov gs, ax",
            code = in(reg) KERNEL_CODE as u64,
            in("ax") KERNEL_DATA,
            out("r10") _,
            options(nostack)
        );

        asm!("ltr {:x}", in(reg) TSS_SEL, options(nomem, nostack, preserves_flags));

        // Enable x87/SSE for the kernel (Limine leaves CR4.OSFXSR clear):
        //   CR0: MP=1 (monitor coprocessor), EM=0 (no x87 emulation),
        //        NE=1 (native x87 exception reporting — without it unmasked
        //        x87 exceptions are delivered PC-style, never as #MF)
        //   CR4: OSFXSR=1 (bit 9 — without it every SSE instruction raises
        //        #UD) and OSXMMEXCPT=1 (bit 10 — without it unmasked SIMD
        //        faults deliver #UD instead of #XM, so the selftest's #XM
        //        trigger could never fire as #XM).
        // Observed: CR4 was 0x20 at boot; the first t_xm run raised #UD at
        // xorps until these were set.
        let mut cr0: u64;
        asm!("mov {}, cr0", out(reg) cr0, options(nomem, nostack));
        let cr0_new = (cr0 | 0x2 | 0x20) & !0x4; // MP=1, NE=1, EM=0
        asm!("mov cr0, {}", in(reg) cr0_new, options(nomem, nostack));
        let mut cr4: u64;
        asm!("mov {}, cr4", out(reg) cr4, options(nomem, nostack));
        let cr4_new = cr4 | 0x200 | 0x400; // OSFXSR | OSXMMEXCPT
        asm!("mov cr4, {}", in(reg) cr4_new, options(nomem, nostack));
    }
}
