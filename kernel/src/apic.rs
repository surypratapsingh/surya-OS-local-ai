//! Local APIC & APIC Timer Driver (K4-T).
//!
//! Provides hardware timer interrupts for preemptive scheduling.
//! Conforms to Intel SDM Vol. 3A Chapter 11 "Advanced Programmable Interrupt Controller (APIC)".

use core::arch::asm;
use core::sync::atomic::{AtomicU64, Ordering};

const IA32_APIC_BASE_MSR: u32 = 0x1B;
const IA32_APIC_BASE_MSR_ENABLE: u64 = 1 << 11;
const APIC_BASE_MASK: u64 = 0x000F_FFFF_F000;

// APIC Register Offsets (in bytes from APIC base address)
pub const APIC_REG_ID: u64 = 0x020;
pub const APIC_REG_VERSION: u64 = 0x030;
pub const APIC_REG_TPR: u64 = 0x080;
pub const APIC_REG_EOI: u64 = 0x0B0;
pub const APIC_REG_LDR: u64 = 0x0D0;
pub const APIC_REG_DFR: u64 = 0x0E0;
pub const APIC_REG_SVR: u64 = 0x0F0;
pub const APIC_REG_LVT_TIMER: u64 = 0x320;
pub const APIC_REG_TIMER_ICR: u64 = 0x380;
pub const APIC_REG_TIMER_CCR: u64 = 0x390;
pub const APIC_REG_TIMER_DCR: u64 = 0x3E0;

pub const APIC_TIMER_VECTOR: u8 = 0x40;

static mut APIC_BASE_VIRT: u64 = 0;
static TIMER_TICKS: AtomicU64 = AtomicU64::new(0);

#[inline]
unsafe fn rdmsr(msr: u32) -> u64 {
    let low: u32;
    let high: u32;
    asm!(
        "rdmsr",
        in("ecx") msr,
        out("eax") low,
        out("edx") high,
        options(nomem, nostack, preserves_flags)
    );
    ((high as u64) << 32) | (low as u64)
}

#[inline]
unsafe fn wrmsr(msr: u32, val: u64) {
    let low = val as u32;
    let high = (val >> 32) as u32;
    asm!(
        "wrmsr",
        in("ecx") msr,
        in("eax") low,
        in("edx") high,
        options(nomem, nostack, preserves_flags)
    );
}

#[inline]
unsafe fn outb(port: u16, value: u8) {
    asm!("out dx, al", in("dx") port, in("al") value, options(nomem, nostack, preserves_flags));
}

/// Mask the legacy 8259 PIC so it does not generate spurious IRQs.
pub unsafe fn mask_pic() {
    outb(0x21, 0xFF);
    outb(0xA1, 0xFF);
}

#[inline]
pub unsafe fn read_reg(offset: u64) -> u32 {
    let base = APIC_BASE_VIRT;
    if base == 0 {
        return 0;
    }
    core::ptr::read_volatile((base + offset) as *const u32)
}

#[inline]
pub unsafe fn write_reg(offset: u64, val: u32) {
    let base = APIC_BASE_VIRT;
    if base == 0 {
        return;
    }
    core::ptr::write_volatile((base + offset) as *mut u32, val);
}

/// Acknowledge the interrupt to the Local APIC (End of Interrupt).
#[no_mangle]
pub unsafe extern "C" fn apic_timer_eoi() {
    write_reg(APIC_REG_EOI, 0);
}

/// Return total APIC timer ticks delivered.
pub fn tick_count() -> u64 {
    TIMER_TICKS.load(Ordering::SeqCst)
}

/// Increment tick counter (called from timer ISR).
pub fn on_timer_tick() -> u64 {
    TIMER_TICKS.fetch_add(1, Ordering::SeqCst) + 1
}

pub const APIC_BASE_OFFSET: u64 = 1 << 30; // 1 GiB above private_base (PDPTE index 1, away from heap & ELF loader)

/// Initialize the Local APIC.
pub fn init() {
    unsafe {
        mask_pic();

        let mut msr = rdmsr(IA32_APIC_BASE_MSR);
        if (msr & IA32_APIC_BASE_MSR_ENABLE) == 0 {
            msr |= IA32_APIC_BASE_MSR_ENABLE;
            wrmsr(IA32_APIC_BASE_MSR, msr);
        }

        let apic_phys = msr & APIC_BASE_MASK;
        let apic_virt = crate::paging::private_base() + APIC_BASE_OFFSET;
        crate::paging::map_page(apic_virt, apic_phys);
        crate::paging::tlb_flush_all();
        APIC_BASE_VIRT = apic_virt;

        // Software enable APIC by setting bit 8 in SVR, with spurious vector 0xFF
        write_reg(APIC_REG_SVR, 0x1FF);

        // Accept all interrupts
        write_reg(APIC_REG_TPR, 0);

        let svr_val = read_reg(APIC_REG_SVR);
        let ver_val = read_reg(APIC_REG_VERSION);
        sprintln!(
            "apic:        Local APIC initialized at phys {:#x} -> virt {:#x} (SVR={:#x}, VER={:#x})",
            apic_phys,
            apic_virt,
            svr_val,
            ver_val
        );
    }
}

/// Arm the Local APIC timer in periodic mode with the given initial count.
pub fn start_timer(initial_count: u32) {
    unsafe {
        // Divide by 16 (DCR value 0x3)
        write_reg(APIC_REG_TIMER_DCR, 0x03);

        // Vector 0x40, Periodic mode (bit 17 = 1), unmasked (bit 16 = 0)
        let lvt_val = (1 << 17) | (APIC_TIMER_VECTOR as u32);
        write_reg(APIC_REG_LVT_TIMER, lvt_val);

        // Setting Initial Count starts the countdown
        write_reg(APIC_REG_TIMER_ICR, initial_count);
    }
}

/// Stop and mask the Local APIC timer.
pub fn stop_timer() {
    unsafe {
        // Mask the timer (bit 16 = 1)
        write_reg(APIC_REG_LVT_TIMER, 1 << 16);
        write_reg(APIC_REG_TIMER_ICR, 0);
    }
}
