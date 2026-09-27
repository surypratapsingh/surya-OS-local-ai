//! CMOS RTC driver (MC146818-compatible) — K3 slice 4.
//!
//! Reads the wall clock through ports 0x70/0x71. Everything the chip can
//! vary — BCD vs binary, 12h vs 24h, the century register — is decoded from
//! Register B / status Register D at runtime, never assumed, because
//! firmware and QEMU are free to set either.
//!
//! Oracle layering (AGENTS.md rule 1 — no check here traces to another NOVA
//! module):
//! - The update-in-progress polling and the "read twice until equal" re-read
//!   follow the MC146818 data sheet's update-cycle protocol; the port
//!   protocol (write index to 0x70, read/write data at 0x71, bit 7 of the
//!   index byte left 0 so NMI state is untouched) is the standard PC/AT CMOS
//!   convention, cited per function below.
//! - `days_from_civil` is transcribed from Howard Hinnant's
//!   "chrono-Compatible Low-Level Date Algorithms" paper (days_from_civil,
//!   an independent proleptic-Gregorian day count), not from any NOVA code.
//! - The day-of-week cross-check compares the chip's own weekday register
//!   (0x06) against the weekday computed from y/m/d by that algorithm.
//! - The strongest oracle lives OUTSIDE the kernel: the gate prints
//!   `RTC_READ UTC <epoch>` on serial, and kernel/scripts/test-rtc.sh
//!   brackets the guest value between two host-UTC samples taken before and
//!   after the QEMU run. QEMU seeds the guest RTC from the host clock, so a
//!   wrong year/century/encoding decode cannot stay inside the bracket.
//!
//! What is deliberately NOT assumed: the century register's existence
//! (0x32 is the ACPI extension most firmware and QEMU implement; if the
//! value is implausible the driver falls back to 2000+yy and the gate
//! prints the fallback so the bracket check judges it).

use crate::ports::{inb, outb};

/// CMOS address register (bit 7 = NMI mask; we always write 0 to leave NMI
/// state alone — PC/AT convention).
const CMOS_ADDRESS: u16 = 0x70;
/// CMOS data register.
const CMOS_DATA: u16 = 0x71;

/// RTC clock/register indexes (PC/AT standard layout).
const REG_SEC: u8 = 0x00;
const REG_SEC_ALARM: u8 = 0x01;
const REG_MIN: u8 = 0x02;
const REG_MIN_ALARM: u8 = 0x03;
const REG_HOUR: u8 = 0x04;
const REG_HOUR_ALARM: u8 = 0x05;
const REG_WEEKDAY: u8 = 0x06; // 1 = Sunday per the data sheet
const REG_DAY: u8 = 0x07;
const REG_MONTH: u8 = 0x08;
const REG_YEAR: u8 = 0x09;
const REG_STATUS_A: u8 = 0x0A;
const REG_STATUS_B: u8 = 0x0B;
const REG_STATUS_D: u8 = 0x0D;
const REG_CENTURY: u8 = 0x32; // ACPI century extension (optional)

/// Status Register A bits.
const STATUS_A_UIP: u8 = 0x80; // update in progress
/// Status Register B bits (MC146818 data sheet section on Register B).
const STATUS_B_SET: u8 = 0x80; // updates stopped (setup mode)
const STATUS_B_DM: u8 = 0x04; // 1 = binary, 0 = BCD
const STATUS_B_24H: u8 = 0x02; // 1 = 24-hour mode
const STATUS_B_UIE: u8 = 0x10; // update-ended interrupt enable
/// Status Register D bits.
const STATUS_D_VALID: u8 = 0x80; // CMOS battery/power good

/// How many `io_wait()` spins to budget waiting for the update cycle.
/// The UIP window is < 244 us (data sheet, update cycle); even at a
/// pessimistic 1024 Hz update rate a few tens of thousands of port waits
/// cover it many times over. Exceeding the budget is an error, not a
/// fallback-to-garbage.
const UIP_BUDGET: u32 = 200_000;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RtcError {
    /// Status Register D says the CMOS power is not valid.
    NoPower,
    /// Status Register A's UIP bit never cleared (or the register is dead).
    UpdateStuck,
    /// Registers never stabilised across two consecutive reads.
    Unstable,
}

#[derive(Debug, Clone, Copy)]
pub struct RtcDateTime {
    pub year: u32,
    pub month: u32,
    pub day: u32,
    pub hour: u32,
    pub minute: u32,
    pub second: u32,
    /// Chip's weekday register, 1 = Sunday (raw; NOT converted).
    pub weekday_reg: u32,
    /// Century register value when plausible, else `None` (see module doc).
    pub century: Option<u32>,
    /// Register B state the decode used, for evidence lines in the gate.
    pub binary_mode: bool,
    pub twenty_four_hour: bool,
    pub update_ended_interrupts: bool,
}

impl RtcDateTime {
    /// Seconds since 1970-01-01T00:00:00Z, treating the RTC fields as UTC.
    /// RTC registers hold LOCAL time on real firmware; the boot configs and
    /// test harness run with `-rtc base=utc`, and the bracket oracle in
    /// test-rtc.sh is what actually validates this choice.
    pub fn epoch(&self) -> u64 {
        let days = days_from_civil(self.year, self.month, self.day) as u64;
        days * 86_400 + self.hour as u64 * 3_600 + self.minute as u64 * 60 + self.second as u64
    }

    /// 0 = Sunday .. 6 = Saturday, computed from y/m/d (Hinnant's algorithm,
    /// shifted because 1970-01-01 was a Thursday).
    pub fn weekday_computed(&self) -> u32 {
        (days_from_civil(self.year, self.month, self.day) + 4) % 7
    }
}

fn cmos_read(index: u8) -> u8 {
    // Bit 7 of the address byte is the NMI mask; writing the index with
    // that bit clear keeps NMI state unchanged.
    outb(CMOS_ADDRESS, index & 0x7F);
    inb(CMOS_DATA)
}

/// True while the chip is mid-update (its registers are being refreshed;
/// data sheet: the UIP bit is set ~244 us before and during the update).
pub fn update_in_progress() -> bool {
    cmos_read(REG_STATUS_A) & STATUS_A_UIP != 0
}

/// Decode one register as BCD (two decimal digits packed in nibbles).
#[inline]
pub(crate) fn from_bcd(v: u8) -> u32 {
    ((v >> 4) & 0x0F) as u32 * 10 + (v & 0x0F) as u32
}

/// Decode one register as plain binary.
#[inline]
fn from_binary(v: u8) -> u32 {
    v as u32
}

/// Decode the hour register per Register B's 24h/12h and DM bits.
/// 12-hour mode packs PM into bit 7 [MC146818 Register B description].
pub(crate) fn decode_hour(raw: u8, binary_mode: bool, twenty_four: bool) -> u32 {
    let h = if binary_mode {
        (raw & 0x7F) as u32
    } else {
        from_bcd(raw & 0x7F)
    };
    if twenty_four {
        h
    } else {
        let pm = raw & 0x80 != 0;
        // 12h: 12 AM -> 0, 1..11 unchanged, 12 PM -> 12, 1..11 PM -> +12.
        if h == 12 {
            if pm {
                12
            } else {
                0
            }
        } else if pm {
            h + 12
        } else {
            h
        }
    }
}

/// Read a stable sample: wait out any in-flight update, then read the
/// register set twice until the interesting bytes agree [MC146818 update
/// cycle; osdev "CMOS — Getting the date/time reliably"]. A budget on the
/// wait turns a dead chip into RtcError::UpdateStuck instead of a hang.
fn read_stable() -> Result<(RtcDateTime, [u8; 10]), RtcError> {
    // Status D first: without battery power nothing else is meaningful.
    if cmos_read(REG_STATUS_D) & STATUS_D_VALID == 0 {
        return Err(RtcError::NoPower);
    }

    // Wait for the UIP bit to clear, bounded.
    let mut spins = 0u32;
    while update_in_progress() {
        crate::ports::io_wait();
        spins += 1;
        if spins > UIP_BUDGET {
            return Err(RtcError::UpdateStuck);
        }
    }

    let reg_b = cmos_read(REG_STATUS_B);
    let binary_mode = reg_b & STATUS_B_DM != 0;
    let twenty_four = reg_b & STATUS_B_24H != 0;
    let uie = reg_b & STATUS_B_UIE != 0;
    // (Register B's SET bit would mean the clock is frozen mid-read; the
    // stability check below covers that case without special handling.)

    let grab = || -> [u8; 10] {
        [
            cmos_read(REG_SEC),
            cmos_read(REG_SEC_ALARM),
            cmos_read(REG_MIN),
            cmos_read(REG_MIN_ALARM),
            cmos_read(REG_HOUR),
            cmos_read(REG_HOUR_ALARM),
            cmos_read(REG_WEEKDAY),
            cmos_read(REG_DAY),
            cmos_read(REG_MONTH),
            cmos_read(REG_YEAR),
        ]
    };
    let a = grab();
    let b = grab();
    // The alarm registers are not part of the update refresh in some
    // implementations' docs, but they are static anyway; stability is
    // asserted on the fields that tick: sec/min/hour + date fields.
    let tick_stable = a[0] == b[0]
        && a[2] == b[2]
        && a[4] == b[4]
        && a[6] == b[6]
        && a[7] == b[7]
        && a[8] == b[8]
        && a[9] == b[9];
    if !tick_stable {
        return Err(RtcError::Unstable);
    }

    // Century register: only trusted when it carries a plausible value
    // (19xx..21xx). Some firmware leaves 0x32 at 0; those hosts get the
    // documented 2000+yy fallback and the gate prints it.
    let century_raw = cmos_read(REG_CENTURY);
    let century = if binary_mode {
        let c = century_raw as u32;
        (19..=21).contains(&c).then_some(c)
    } else {
        let c = from_bcd(century_raw);
        (19..=21).contains(&c).then_some(c)
    };

    let year2 = if binary_mode {
        b[9] as u32
    } else {
        from_bcd(b[9])
    };
    let year = match century {
        Some(c) => c * 100 + year2,
        // ACPI convention for a missing century register.
        None => 2000 + year2,
    };
    let dt = RtcDateTime {
        year,
        month: if binary_mode {
            b[8] as u32
        } else {
            from_bcd(b[8])
        },
        day: if binary_mode {
            b[7] as u32
        } else {
            from_bcd(b[7])
        },
        hour: decode_hour(b[4], binary_mode, twenty_four),
        minute: if binary_mode {
            b[2] as u32
        } else {
            from_bcd(b[2])
        },
        second: if binary_mode {
            a[0] as u32
        } else {
            from_bcd(a[0])
        },
        weekday_reg: b[6] as u32,
        century,
        binary_mode,
        twenty_four_hour: twenty_four,
        update_ended_interrupts: uie,
    };
    Ok((dt, b))
}

/// Public read: a fully decoded, stability-checked datetime.
pub fn read_datetime() -> Result<RtcDateTime, RtcError> {
    read_stable().map(|(dt, _)| dt)
}

/// Public read: seconds since the Unix epoch (UTC), per `RtcDateTime::epoch`.
pub fn read_epoch() -> Result<u64, RtcError> {
    read_datetime().map(|dt| dt.epoch())
}

/// Hardware format invariant [MC146818 update cycle]: when update-ended
/// interrupts are enabled, an update cycle terminates by rolling the
/// seconds register to zero — so (seconds == 0) must follow a UIP pulse
/// exactly. Returns `None` when the preconditions are absent (UIE off), so
/// the caller can skip loudly rather than assume.
pub fn format_check() -> Option<bool> {
    let reg_b = cmos_read(REG_STATUS_B);
    if reg_b & STATUS_B_UIE == 0 {
        return None;
    }
    // Wait for an update cycle to start, bounded.
    let mut spins = 0u32;
    while !update_in_progress() {
        crate::ports::io_wait();
        spins += 1;
        if spins > UIP_BUDGET {
            return Some(false);
        }
    }
    // Ride it out to the end of the cycle.
    let mut spins = 0u32;
    while update_in_progress() {
        crate::ports::io_wait();
        spins += 1;
        if spins > UIP_BUDGET {
            return Some(false);
        }
    }
    let seconds = cmos_read(REG_SEC);
    Some(seconds == 0)
}

/// Render `YYYY-MM-DDTHH:MM:SS` (RFC 3339-style, 'T' separator, zero
/// padded) into `buf`; returns the used length (19), or 0 when `buf` is
/// too small. The selftest gate asserts this exact shape so the serial
/// evidence lines stay parseable by the harness.
pub fn render_datetime(dt: &RtcDateTime, buf: &mut [u8]) -> usize {
    /// Write `v` as two zero-padded decimal digits at buf[pos..pos+2].
    fn put2(buf: &mut [u8], pos: usize, v: u32) {
        buf[pos] = b'0' + ((v / 10) % 10) as u8;
        buf[pos + 1] = b'0' + (v % 10) as u8;
    }
    if buf.len() < 19 {
        return 0;
    }
    let y = dt.year;
    buf[0] = b'0' + ((y / 1000) % 10) as u8;
    buf[1] = b'0' + ((y / 100) % 10) as u8;
    put2(buf, 2, y % 100);
    buf[4] = b'-';
    put2(buf, 5, dt.month);
    buf[7] = b'-';
    put2(buf, 8, dt.day);
    buf[10] = b'T';
    put2(buf, 11, dt.hour);
    buf[13] = b':';
    put2(buf, 14, dt.minute);
    buf[16] = b':';
    put2(buf, 17, dt.second);
    19
}

/// Days since 1970-01-01 for a proleptic-Gregorian date.
/// Transcribed from Howard Hinnant, "chrono-Compatible Low-Level Date
/// Algorithms" (days_from_civil); valid for the RTC's practical range and
/// independent of every other date computation in this repository.
pub fn days_from_civil(y: u32, m: u32, d: u32) -> u32 {
    let y = y as i64;
    let m = m as i64;
    let d = d as i64;
    let yy = if m <= 2 { y - 1 } else { y };
    let era = if yy >= 0 { yy } else { yy - 399 } / 400;
    let yoe = yy - era * 400; // [0, 399]
    let mp = if m > 2 { m - 3 } else { m + 9 }; // March=0
    let doy = (153 * mp + 2) / 5 + d - 1; // [0, 365]
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy; // [0, 146096]
    (era * 146_097 + doe - 719_468) as u32
}
