//! PS/2 keyboard driver (polling, ports 0x60/0x64) for K2.
//!
//! Scancode set 1 → ASCII, with Shift and CapsLock; Ctrl and Alt tracked for
//! the shell. Polling (no IRQs yet) fits the K2 no-interrupt model.

use crate::ports::{inb, io_wait, outb};

const DATA_PORT: u16 = 0x60;
const STATUS_PORT: u16 = 0x64;

const STATUS_OBF: u8 = 0x01; // output buffer full

// Controller commands (written to 0x64; arg via 0x60).
const CMD_DISABLE_PORT1: u8 = 0xAD;
const CMD_ENABLE_PORT1: u8 = 0xAE;
const CMD_CONTROLLER_BYTE: u8 = 0x20;
const CMD_WRITE_CONTROLLER_BYTE: u8 = 0x60;

// Device commands (written to 0x60 while port1 enabled).
const CMD_SCANCODE_SET: u8 = 0xF0;
const SCANCODE_SET_1: u8 = 1;
const CMD_ENABLE_SCANNING: u8 = 0xF4;

const KEY_RELEASED: u8 = 0x80;

#[derive(Clone, Copy, Default)]
pub struct Modifiers {
    pub shift: bool,
    pub ctrl: bool,
    pub alt: bool,
    pub caps: bool,
}

/// A decoded key event. `ch` is None for non-printing keys (arrows, F-keys
/// are ignored entirely in K2).
#[derive(Clone, Copy)]
pub struct KeyEvent {
    pub ch: Option<u8>,
    pub released: bool,
    pub mods: Modifiers,
    /// Escape, Enter, Backspace and Tab arrive as control chars; this flag
    /// marks the special keys the shell treats individually.
    pub is_escape: bool,
}

pub struct Keyboard {
    mods: Modifiers,
    initialized: bool,
    /// Debug: echo every raw byte to serial as hex (enabled via `novaraw`
    /// on the kernel command line).
    pub raw_echo: bool,
}

impl Keyboard {
    pub const fn new() -> Keyboard {
        Keyboard {
            mods: Modifiers {
                shift: false,
                ctrl: false,
                alt: false,
                caps: false,
            },
            initialized: false,
            raw_echo: false,
        }
    }

    /// Bring the first PS/2 port up, best effort, in translation-forced mode.
    /// Never panics: if the controller is absent we simply never report keys.
    pub fn init(&mut self) {
        // Disable both ports while configuring.
        outb(STATUS_PORT, CMD_DISABLE_PORT1);
        io_wait();
        outb(STATUS_PORT, 0xA7); // disable port 2 (may not exist)
        io_wait();

        // Drain stale output.
        let _ = inb(DATA_PORT);

        // Read current configuration byte; if the controller is missing the
        // read returns 0xFF and we still proceed (harmless).
        outb(STATUS_PORT, CMD_CONTROLLER_BYTE);
        io_wait();
        let cfg = inb(DATA_PORT);
        if self.raw_echo {
            crate::sprint!("[cfg={:02x}]", cfg);
        }

        // Ask the device which scancode set it uses (0xF0, 0x00 → ACK, set).
        let mut device_set = 0u8;
        outb(DATA_PORT, CMD_SCANCODE_SET);
        io_wait();
        outb(DATA_PORT, 0x00);
        io_wait();
        let mut spins = 0;
        while spins < 200_000 {
            if self.output_full() {
                let b = inb(DATA_PORT);
                if b == 0xFA {
                    continue; // ACK; the set number follows
                }
                if (1..=3).contains(&b) {
                    device_set = b;
                }
                break;
            }
            spins += 1;
            core::hint::spin_loop();
        }
        if self.raw_echo {
            crate::sprint!("[set={}]", device_set);
        }

        // Clear the controller's translation bit unconditionally. Our driver
        // speaks scancode set 1 natively and we ask the device to use set 1
        // below; leaving translation on double-translates when the device
        // (or QEMU's emulated device) already emits set 1 — makes get
        // re-mapped to the wrong keys while breaks pass through, which is
        // exactly the `sendkey h` → `d` scramble observed under QEMU.
        let new_cfg = cfg & !0x40;
        outb(STATUS_PORT, CMD_WRITE_CONTROLLER_BYTE);
        io_wait();
        outb(DATA_PORT, new_cfg);
        if self.raw_echo {
            crate::sprint!("[newcfg={:02x}]", new_cfg);
        }

        // Re-enable port 1.
        outb(STATUS_PORT, CMD_ENABLE_PORT1);
        io_wait();

        // Ask the device for scancode set 1 (ignored if unsupported).
        outb(DATA_PORT, CMD_SCANCODE_SET);
        io_wait();
        outb(DATA_PORT, SCANCODE_SET_1);
        io_wait();
        let _ = inb(DATA_PORT); // eat ACK/residue

        // Enable scanning.
        outb(DATA_PORT, CMD_ENABLE_SCANNING);
        io_wait();
        let _ = inb(DATA_PORT); // eat ACK

        self.initialized = true;
    }

    fn output_full(&self) -> bool {
        inb(STATUS_PORT) & STATUS_OBF != 0
    }

    /// Poll for one key event; None when nothing is pending.
    pub fn poll(&mut self) -> Option<KeyEvent> {
        if !self.initialized || !self.output_full() {
            return None;
        }
        let sc = inb(DATA_PORT);
        if self.raw_echo {
            crate::sprint!("[{:02x}]", sc);
        }
        if sc == 0xE0 {
            // Extended prefix (arrows etc.) — consume the next byte and drop.
            let mut spins = 0;
            while !self.output_full() && spins < 100_000 {
                spins += 1;
                core::hint::spin_loop();
            }
            if self.output_full() {
                let _ = inb(DATA_PORT);
            }
            return None;
        }
        let released = sc & KEY_RELEASED != 0;
        let make = sc & 0x7F;
        match make {
            0x2A | 0x36 => self.mods.shift = !released, // L/R shift
            0x1D => self.mods.ctrl = !released,         // ctrl
            0x38 => self.mods.alt = !released,          // alt
            0x3A => {
                if !released {
                    self.mods.caps = !self.mods.caps; // capslock toggles on press
                }
            }
            _ => {}
        }
        if released {
            return Some(KeyEvent {
                ch: None,
                released: true,
                mods: self.mods,
                is_escape: false,
            });
        }
        let (base, shifted, is_escape) = match scancode_ascii(make) {
            Some(t) => t,
            None => {
                return Some(KeyEvent {
                    ch: None,
                    released: false,
                    mods: self.mods,
                    is_escape: false,
                })
            }
        };
        let ch = if is_escape {
            Some(0x1B)
        } else if (base as char).is_ascii_alphabetic() {
            let upper = self.mods.shift != self.mods.caps;
            Some(if upper { shifted } else { base })
        } else {
            Some(if self.mods.shift { shifted } else { base })
        };
        Some(KeyEvent {
            ch,
            released: false,
            mods: self.mods,
            is_escape,
        })
    }
}

/// Scancode set 1 (make codes) → (normal, shifted, is_special).
const fn scancode_ascii(make: u8) -> Option<(u8, u8, bool)> {
    match make {
        0x01 => Some((0x1B, 0x1B, true)),  // ESC
        0x02 => Some((b'1', b'!', false)),
        0x03 => Some((b'2', b'@', false)),
        0x04 => Some((b'3', b'#', false)),
        0x05 => Some((b'4', b'$', false)),
        0x06 => Some((b'5', b'%', false)),
        0x07 => Some((b'6', b'^', false)),
        0x08 => Some((b'7', b'&', false)),
        0x09 => Some((b'8', b'*', false)),
        0x0A => Some((b'9', b'(', false)),
        0x0B => Some((b'0', b')', false)),
        0x0C => Some((b'-', b'_', false)),
        0x0D => Some((b'=', b'+', false)),
        0x0E => Some((0x08, 0x08, false)), // backspace
        0x0F => Some((b'\t', b'\t', false)),
        0x10 => Some((b'q', b'Q', false)),
        0x11 => Some((b'w', b'W', false)),
        0x12 => Some((b'e', b'E', false)),
        0x13 => Some((b'r', b'R', false)),
        0x14 => Some((b't', b'T', false)),
        0x15 => Some((b'y', b'Y', false)),
        0x16 => Some((b'u', b'U', false)),
        0x17 => Some((b'i', b'I', false)),
        0x18 => Some((b'o', b'O', false)),
        0x19 => Some((b'p', b'P', false)),
        0x1A => Some((b'[', b'{', false)),
        0x1B => Some((b']', b'}', false)),
        0x1C => Some((b'\r', b'\r', false)), // enter
        0x1E => Some((b'a', b'A', false)),
        0x1F => Some((b's', b'S', false)),
        0x20 => Some((b'd', b'D', false)),
        0x21 => Some((b'f', b'F', false)),
        0x22 => Some((b'g', b'G', false)),
        0x23 => Some((b'h', b'H', false)),
        0x24 => Some((b'j', b'J', false)),
        0x25 => Some((b'k', b'K', false)),
        0x26 => Some((b'l', b'L', false)),
        0x27 => Some((b';', b':', false)),
        0x28 => Some((b'\'', b'"', false)),
        0x29 => Some((b'`', b'~', false)),
        0x2B => Some((b'\\', b'|', false)),
        0x2C => Some((b'z', b'Z', false)),
        0x2D => Some((b'x', b'X', false)),
        0x2E => Some((b'c', b'C', false)),
        0x2F => Some((b'v', b'V', false)),
        0x30 => Some((b'b', b'B', false)),
        0x31 => Some((b'n', b'N', false)),
        0x32 => Some((b'm', b'M', false)),
        0x33 => Some((b',', b'<', false)),
        0x34 => Some((b'.', b'>', false)),
        0x35 => Some((b'/', b'?', false)),
        0x39 => Some((b' ', b' ', false)),
        _ => None,
    }
}
