//! Clean exit under QEMU via the `isa-debug-exit` device.
//!
//! Gated behind the cargo feature `qemu-exit` (off by default, W1 item 3).
//! When the feature is enabled (selftest build), exit codes via this device are
//! `(value << 1) | 1`, so value 16 yields exit code 33 and value 17 yields exit code 35.
//!
//! Without the feature (production build), `success()` and `failure()` print their
//! serial marker and halt indefinitely (`cli; hlt`). They never write to any I/O port.

#[cfg(feature = "qemu-exit")]
/// Port base for the isa-debug-exit ISA device (default QEMU build).
const DEBUG_EXIT_PORT: u16 = 0x501;
#[cfg(feature = "qemu-exit")]
/// Test success value; QEMU exit code becomes (16 << 1) | 1 = 33.
const EXIT_VALUE: u16 = 16;
#[cfg(feature = "qemu-exit")]
/// Test failure value; QEMU exit code becomes (17 << 1) | 1 = 35 —
/// distinct from success so CI can tell a failed exception-gate check from
/// a passing boot.
const FAILURE_VALUE: u16 = 17;

#[cfg(feature = "qemu-exit")]
/// Exit QEMU with the success code (33).
pub fn success() -> ! {
    x86_outw(EXIT_VALUE, DEBUG_EXIT_PORT);
    unreachable!("isa-debug-exit should have terminated QEMU");
}

#[cfg(feature = "qemu-exit")]
/// Exit QEMU with the failure code (35).
pub fn failure() -> ! {
    x86_outw(FAILURE_VALUE, DEBUG_EXIT_PORT);
    unreachable!("isa-debug-exit should have terminated QEMU");
}

#[cfg(not(feature = "qemu-exit"))]
/// Production mode (qemu-exit feature off): print marker and halt. Never write any I/O port.
pub fn success() -> ! {
    crate::sprintln!("NOVA_HALT: success (qemu-exit feature disabled)");
    halt()
}

#[cfg(not(feature = "qemu-exit"))]
/// Production mode (qemu-exit feature off): print marker and halt. Never write any I/O port.
pub fn failure() -> ! {
    crate::sprintln!("NOVA_HALT: failure (qemu-exit feature disabled)");
    halt()
}

#[cfg(not(feature = "qemu-exit"))]
fn halt() -> ! {
    loop {
        unsafe {
            core::arch::asm!("cli; hlt", options(nomem, nostack, preserves_flags));
        }
    }
}

#[cfg(feature = "qemu-exit")]
#[inline]
fn x86_outw(value: u16, port: u16) {
    unsafe {
        core::arch::asm!(
            "out dx, ax",
            in("dx") port,
            in("ax") value,
            options(nomem, nostack, preserves_flags)
        );
    }
}
