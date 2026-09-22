//! Interrupt Descriptor Table + CPU exception handlers (K2).
//!
//! Handlers are tiny assembly stubs (stable toolchain — no `x86-interrupt`
//! ABI) that normalize the stack (synthesizing a zero error code where the
//! CPU pushes none), then call a Rust reporter with (vector, code, rip).
//! Every fault reports on serial and halts.

use core::arch::asm;
use core::arch::global_asm;
use core::mem::size_of;

const VEC_COUNT: usize = 256;

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

/// Rust-side reporter: prints the fault and halts. Never returns.
extern "C" fn fault_report(vec: u64, code: u64, rip: u64) -> ! {
    sprintln!("NOVA_FAULT: vector {} at rip={:#x}", vec, rip);
    sprintln!("  error code: {:#x}", code);
    sprintln!("NOVA: halted.");
    loop {
        unsafe { asm!("cli; hlt", options(nomem, nostack, preserves_flags)) };
    }
}

global_asm!(
    ".section .text",
    "common_fault_entry:",
    "    mov rsi, [rsp + 8]",   // error code (real or synthesized)
    "    mov rdx, [rsp + 16]",  // faulting RIP (return address)
    "    call {report}",
    "    ud2",
    report = sym fault_report
);

// Non-error-code vectors: push a synthetic zero code first so the stack
// layout matches, then set the vector number and jump to the common path.
macro_rules! fault_stub {
    ($name:ident, $vec:literal, plain) => {
        global_asm!(
            concat!(
                ".globl ", stringify!($name), "\n",
                stringify!($name), ":\n",
                "    push 0\n",
                "    mov edi, ", stringify!($vec), "\n",
                "    jmp common_fault_entry\n"
            )
        );
    };
    // Error-code vectors: the CPU already pushed the code.
    ($name:ident, $vec:literal, ec) => {
        global_asm!(
            concat!(
                ".globl ", stringify!($name), "\n",
                stringify!($name), ":\n",
                "    mov edi, ", stringify!($vec), "\n",
                "    jmp common_fault_entry\n"
            )
        );
    };
}

fault_stub!(vec0, 0, plain); // divide by zero
fault_stub!(vec1, 1, plain); // debug
fault_stub!(vec2, 2, plain); // NMI
fault_stub!(vec3, 3, plain); // breakpoint
fault_stub!(vec4, 4, plain); // overflow
fault_stub!(vec5, 5, plain); // bound range
fault_stub!(vec6, 6, plain); // invalid opcode
fault_stub!(vec7, 7, plain); // device not available
fault_stub!(vec8, 8, ec); // double fault
fault_stub!(vec10, 10, ec); // invalid TSS
fault_stub!(vec11, 11, ec); // segment not present
fault_stub!(vec12, 12, ec); // stack fault
fault_stub!(vec13, 13, ec); // general protection fault
fault_stub!(vec14, 14, ec); // page fault
fault_stub!(vec16, 16, plain); // x87 FP
fault_stub!(vec17, 17, ec); // alignment check
fault_stub!(vec18, 18, plain); // machine check
fault_stub!(vec19, 19, plain); // SIMD FP
fault_stub!(vec20, 20, ec); // control protection

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
    fn vec10();
    fn vec11();
    fn vec12();
    fn vec13();
    fn vec14();
    fn vec16();
    fn vec17();
    fn vec18();
    fn vec19();
    fn vec20();
}

unsafe fn set_gate(vec: u8, addr: u64) {
    let idt = core::ptr::addr_of_mut!(IDT);
    (*idt).entries[vec as usize] = InterruptDescriptor64 {
        offset_low: addr as u16,
        selector: 0x08,
        ist: 0,
        type_attr: ATTR_PRESENT_INT,
        offset_mid: (addr >> 16) as u16,
        offset_high: (addr >> 32) as u32,
        zero: 0,
    };
}

/// Arm all exception gates and load the IDT.
pub fn init() {
    unsafe {
        set_gate(0, vec0 as usize as u64);
        set_gate(1, vec1 as usize as u64);
        set_gate(2, vec2 as usize as u64);
        set_gate(3, vec3 as usize as u64);
        set_gate(4, vec4 as usize as u64);
        set_gate(5, vec5 as usize as u64);
        set_gate(6, vec6 as usize as u64);
        set_gate(7, vec7 as usize as u64);
        set_gate(8, vec8 as usize as u64);
        set_gate(10, vec10 as usize as u64);
        set_gate(11, vec11 as usize as u64);
        set_gate(12, vec12 as usize as u64);
        set_gate(13, vec13 as usize as u64);
        set_gate(14, vec14 as usize as u64);
        set_gate(16, vec16 as usize as u64);
        set_gate(17, vec17 as usize as u64);
        set_gate(18, vec18 as usize as u64);
        set_gate(19, vec19 as usize as u64);
        set_gate(20, vec20 as usize as u64);
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
