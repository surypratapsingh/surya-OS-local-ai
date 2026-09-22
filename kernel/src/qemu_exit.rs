//! Clean exit under QEMU via the `isa-debug-exit` device.
//!
//! QEMU-specific and therefore opt-in: exit codes via this device are
//! `(value << 1) | 1`, so value 16 yields exit code 33. On real hardware the
//! ports simply don't exist and writes are ignored — so the same binary is
//! safe to flash onto the old machine.

/// Port base for the isa-debug-exit ISA device (default QEMU build).
const DEBUG_EXIT_PORT: u16 = 0x501;
/// Test success value; QEMU exit code becomes (16 << 1) | 1 = 33.
const EXIT_VALUE: u16 = 16;

/// Exit QEMU with the success code (33). On hardware this is a no-op.
pub fn success() -> ! {
    x86_outw(EXIT_VALUE, DEBUG_EXIT_PORT);
    unreachable!("isa-debug-exit should have terminated QEMU");
}

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
