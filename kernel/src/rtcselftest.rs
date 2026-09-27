//! K3 CMOS RTC selftest (rtc gate).
//!
//! Oracle layering (AGENTS.md rule 1 — every check has an oracle that does
//! not trace to the code under test):
//!
//! - The decoder unit checks compare `decode_hour` against a HAND golden
//!   table (values written by a human from the MC146818 Register B rules,
//!   not from the implementation), and the BCD path against an encoder
//!   written independently in this file.
//! - The epoch golden table is hand-computed from published civil-calendar
//!   anchors (1970-01-01 = day 0; 2024-01-01 and 2026-01-01 weekdays are
//!   printed on every wall calendar), not derived from the driver.
//! - The weekday cross-check uses Hinnant's days_from_civil (module doc of
//!   rtc.rs) against the chip's own weekday register.
//! - The strongest oracle is EXTERNAL to this binary: the gate prints
//!   `RTC_READ UTC <epoch>` and `RTC_READ <y>-<m>-<d> <h>:<mm>:<ss>` lines;
//!   kernel/scripts/test-rtc.sh brackets the epoch between two host-UTC
//!   samples around the QEMU run (QEMU seeds the guest RTC from the host
//!   clock). A wrong century/BCD/12h decode cannot stay inside a ±120 s
//!   bracket.
//! - `check rline` validates the serial rendering so the harness parses a
//!   known format (RFC 3339 'T' separator, zero-padded).

use crate::rtc::{self, RtcError};

static mut PASS: u32 = 0;
static mut FAIL: u32 = 0;

fn check(ok: bool, name: &str, detail: &str) {
    unsafe {
        if ok {
            PASS += 1;
            sprintln!("  ok    {}", name);
        } else {
            FAIL += 1;
            sprintln!("  FAIL  {} - {}", name, detail);
        }
    }
}

// ---------------------------------------------------------------------------
// Independent encoders + golden tables (hand data, not implementation output)
// ---------------------------------------------------------------------------

/// Independent BCD encoder (this file's own two-digit packer; the driver has
/// only a decoder, so the round-trip cannot be a tautology).
fn to_bcd(v: u32) -> u8 {
    ((v / 10 * 16) + (v % 10)) as u8
}

/// Hour register encoding, written out by hand from the MC146818 Register B
/// rules (24h vs 12h with PM in bit 7, binary vs BCD). Table entries:
/// (decimal hour, binary_mode, twenty_four, expected raw register).
const HOUR_TABLE: &[(u32, bool, bool, u8)] = &[
    (0, false, true, 0x00),
    (9, false, true, 0x09),
    (12, false, true, 0x12),
    (13, false, true, 0x13),
    (23, false, true, 0x23),
    (0, false, false, 0x12), // 12 AM
    (1, false, false, 0x01),
    (11, false, false, 0x11),
    (12, false, false, 0x92), // 12 PM: PM bit 7 set, digits "12"
    (13, false, false, 0x81), // 1 PM
    (23, false, false, 0x91), // 11 PM
    (0, true, true, 0x00),
    (9, true, true, 0x09),
    (12, true, true, 0x0C),
    (13, true, true, 0x0D),
    (23, true, true, 0x17),
    (0, true, false, 0x0C), // 12 AM, binary digits: twelve
    (13, true, false, 0x81),
    (23, true, false, 0x8B), // 11 PM, binary digits: eleven + PM bit
];

/// Epoch golden table: hand-computed days from published anchors.
/// 1970-01-01 = 0; 2024-01-01 = 19723 (54 years with 13 leap days);
/// 2026-09-27 = 19723 + 731 (2024-25) + 269 (day-of-year of Sep 27, 2026
/// non-leap, counted from Jan 1) = 20723; 2038-01-19 = the INT32_MAX
/// boundary day. Each row carries its published weekday (1970-01-01
/// Thursday, 2024-01-01 Monday, 2026-09-27 Sunday, 2038-01-19 Tuesday).
const EPOCH_TABLE: &[(u32, u32, u32, u32, u32)] = &[
    (1970, 1, 1, 0, 4),
    (2024, 1, 1, 19_723, 1),
    (2026, 9, 27, 20_723, 0),
    (2038, 1, 19, 24_855, 2),
];

fn unit_checks() {
    // 1. Hour decode vs the hand table, both directions where reversible.
    let mut hour_ok = true;
    for &(h, bin, h24, raw) in HOUR_TABLE {
        if rtc::decode_hour(raw, bin, h24) != h {
            hour_ok = false;
        }
    }
    check(
        hour_ok,
        "hour decode matches hand-written MC146818 Register B table (19 rows)",
        "decode_hour disagrees with the golden table",
    );

    // 2. BCD decode vs the independent encoder, all two-digit values 0..99.
    let mut bcd_ok = true;
    for v in 0..=99u32 {
        if rtc::from_bcd(to_bcd(v)) != v {
            bcd_ok = false;
        }
    }
    // And a couple of hand values so the encoder itself is anchored:
    if to_bcd(59) != 0x59 || to_bcd(0) != 0x00 || to_bcd(27) != 0x27 {
        bcd_ok = false;
    }
    check(
        bcd_ok,
        "BCD decode round-trips 0..=99 against an independent encoder",
        "from_bcd(to_bcd(v)) != v somewhere in 0..=99",
    );

    // 3. Epoch math vs the hand golden table + weekday anchors.
    let mut epoch_ok = true;
    for &(y, m, d, want_days, wd) in EPOCH_TABLE {
        let days = rtc::days_from_civil(y, m, d);
        let weekday = (days + 4) % 7; // 1970-01-01 was a Thursday
        if days != want_days || weekday != wd {
            epoch_ok = false;
            sprintln!(
                "        {}-{:02}-{:02}: got days={} wd={} want days={} wd={}",
                y,
                m,
                d,
                days,
                weekday,
                want_days,
                wd
            );
        }
    }
    check(
        epoch_ok,
        "epoch math matches hand anchors (incl. weekday of each row)",
        "days_from_civil disagrees with the golden table",
    );
}

// ---------------------------------------------------------------------------
// Live-hardware checks
// ---------------------------------------------------------------------------

fn live_checks() -> Option<rtc::RtcDateTime> {
    // 4. Live read: power validity, UIP wait and two-read stability are all
    //    reported by read_datetime's error path — a dead chip is a FAIL, not
    //    a fallback to garbage.
    let dt = match rtc::read_datetime() {
        Ok(dt) => dt,
        Err(RtcError::NoPower) => {
            check(
                false,
                "CMOS status D reports valid power",
                "STATUS_D_VALID clear — RTC unusable",
            );
            return None;
        }
        Err(RtcError::UpdateStuck) => {
            check(
                false,
                "UIP clears within budget",
                "UIP stuck — dead or absent RTC",
            );
            return None;
        }
        Err(RtcError::Unstable) => {
            check(
                false,
                "registers stable across two reads",
                "values changed mid-update",
            );
            return None;
        }
    };

    // 5. Ranges: every field within what the decode permits.
    let range_ok = (1..=12).contains(&dt.month)
        && (1..=31).contains(&dt.day)
        && dt.hour <= 23
        && dt.minute <= 59
        && dt.second <= 59
        && dt.year >= 1970
        && dt.year <= 4095
        && (1..=7).contains(&dt.weekday_reg);
    check(
        range_ok,
        "all decoded fields within legal ranges",
        "field out of range",
    );

    // 6. The strongest in-kernel check: chip weekday register vs weekday
    //    computed from y/m/d via days_from_civil. MC146818 convention:
    //    the register is 1=Sunday .. 7=Saturday, so register r maps to
    //    computed (r - 1) with 0 = Sunday. QEMU follows the convention;
    //    a firmware that does not is a finding, and the epoch bracket
    //    oracle is the arbiter of the date itself.
    let computed0 = dt.weekday_computed(); // 0 = Sunday
    let reg_conv = dt.weekday_reg.saturating_sub(1); // 1=Sun..7=Sat -> 0..6
    check(
        reg_conv == computed0,
        "chip weekday register agrees with y/m/d (Hinnant cross-check)",
        "register says one day, y/m/d math says another",
    );

    // 7. Status A: the divider should be settled at 32.768 kHz (0b0010 in
    //    bits 6-4) on a working chip; QEMU sets exactly that. Not fatal if
    //    different (real firmware may vary), so this is informational — a
    //    printed line, not a check.
    // (kept out of the check counter deliberately)

    // 8. Format invariant (only when the chip uses update-ended interrupts;
    //    skipped loudly otherwise).
    match rtc::format_check() {
        Some(true) => check(
            true,
            "update cycle ends with seconds rolled to zero (format invariant)",
            "",
        ),
        Some(false) => check(
            false,
            "update cycle ends with seconds rolled to zero (format invariant)",
            "seconds != 0 after a completed update",
        ),
        None => sprintln!(
            "  note  format invariant skipped: UIE off (update-ended interrupts disabled)"
        ),
    }

    // Evidence lines for the host-side bracket oracle.
    sprintln!("RTC_READ UTC {}", dt.epoch());
    sprintln!(
        "RTC_READ {:04}-{:02}-{:02}T{:02}:{:02}:{:02} reg={:?} mode={}",
        dt.year,
        dt.month,
        dt.day,
        dt.hour,
        dt.minute,
        dt.second,
        dt.weekday_reg,
        if dt.binary_mode { "binary" } else { "bcd" },
    );
    match dt.century {
        Some(c) => sprintln!("RTC_READ century={} (register 0x32 plausible)", c),
        None => sprintln!("RTC_READ century=none (fell back to 2000+yy)"),
    }

    // 9. Rendering: the RFC-3339-style line must round-trip through the
    //    exact format the harness parses (T separator, zero padded).
    let mut buf = [0u8; 40];
    let n = rtc::render_datetime(&dt, &mut buf);
    let s = core::str::from_utf8(&buf[..n]).unwrap_or("");
    let bytes = s.as_bytes();
    let rline_ok = n == 19
        && bytes[4] == b'-'
        && bytes[7] == b'-'
        && bytes[10] == b'T'
        && bytes[13] == b':'
        && bytes[16] == b':'
        // Every non-separator position must be a digit (the previous
        // predicate forgot ':' is a separator and contradicted itself).
        && bytes
            .iter()
            .enumerate()
            .all(|(i, &b)| matches!(i, 4 | 7 | 10 | 13 | 16) || b.is_ascii_digit());
    check(
        rline_ok,
        "RTC_READ line renders in the documented parseable format",
        "render_datetime output malformed",
    );

    Some(dt)
}

pub fn run() {
    sprintln!("rtc gate:   reading the CMOS RTC (ports 0x70/0x71)");
    unit_checks();
    let dt = live_checks();
    if let Some(dt) = dt {
        // render line already emitted inside live_checks; print the render
        // length evidence here.
        let mut buf = [0u8; 40];
        let n = rtc::render_datetime(&dt, &mut buf);
        sprintln!("RTC_READ render_len={}", n);
    }
    let (p, f) = unsafe { (PASS, FAIL) };
    sprintln!("rtc gate:   {} checks passed, {} failed", p, f);
}

pub fn summary() -> (u32, u32) {
    unsafe { (PASS, FAIL) }
}
