//! Serial port logging (COM1, 16550 UART, 115200 8N1).
//!
//! K1's primary debug channel. Every boot report line lands here, and the
//! smoke test greps for `NOVA_BOOT_OK`.

use core::arch::asm;
use core::fmt;

/// COM1 base port.
const COM1: u16 = 0x3F8;

const DATA: u16 = 0;
const INTERRUPT_ENABLE: u16 = 1;
const FIFO_CONTROL: u16 = 2;
const LINE_CONTROL: u16 = 3;
const MODEM_CONTROL: u16 = 4;
const LINE_STATUS: u16 = 5;

const LINE_STATUS_TRANSMIT_EMPTY: u8 = 0x20;

#[inline]
fn outb(port: u16, value: u8) {
    unsafe {
        asm!("out dx, al", in("dx") port, in("al") value, options(nomem, nostack, preserves_flags));
    }
}

#[inline]
fn inb(port: u16) -> u8 {
    let value: u8;
    unsafe {
        asm!("in al, dx", out("al") value, in("dx") port, options(nomem, nostack, preserves_flags));
    }
    value
}

fn port(offset: u16) -> u16 {
    COM1 + offset
}

/// Program COM1 for 115200 8N1, FIFO on. Safe to call once at boot.
pub fn init() {
    outb(port(INTERRUPT_ENABLE), 0x00); // disable interrupts
    outb(port(LINE_CONTROL), 0x80); // DLAB on
    outb(port(DATA), 0x01); // divisor low: 1 => 115200
    outb(port(INTERRUPT_ENABLE), 0x00); // divisor high: 0
    outb(port(LINE_CONTROL), 0x03); // 8N1, DLAB off
    outb(port(FIFO_CONTROL), 0xC7); // enable+clear FIFOs, 14-byte threshold
    outb(port(MODEM_CONTROL), 0x0B); // DTR|RTS|OUT2
}

/// True when the UART can accept another byte.
fn can_send() -> bool {
    inb(port(LINE_STATUS)) & LINE_STATUS_TRANSMIT_EMPTY != 0
}

/// Blocking byte write.
pub fn send_byte(b: u8) {
    let mut spins: u32 = 0;
    while !can_send() && spins < 5_000_000 {
        spins += 1;
        core::hint::spin_loop();
    }
    outb(port(DATA), b);
}

/// Write a string, mapping `\n` to CRLF (terminal friendliness).
pub fn write_str(s: &str) {
    for b in s.bytes() {
        if b == b'\n' {
            send_byte(b'\r');
        }
        send_byte(b);
    }
}

pub struct SerialWriter;

impl fmt::Write for SerialWriter {
    fn write_str(&mut self, s: &str) -> fmt::Result {
        write_str(s);
        Ok(())
    }
}

/// `print!`-style macro to serial.
#[macro_export]
macro_rules! sprint {
    ($($arg:tt)*) => {{
        use core::fmt::Write as _;
        let _ = write!($crate::serial::SerialWriter, $($arg)*);
    }};
}

/// `println!`-style macro to serial with a trailing newline.
#[macro_export]
macro_rules! sprintln {
    () => {
        $crate::serial::write_str("\n")
    };
    ($($arg:tt)*) => {{
        use core::fmt::Write as _;
        let _ = writeln!($crate::serial::SerialWriter, $($arg)*);
    }};
}
