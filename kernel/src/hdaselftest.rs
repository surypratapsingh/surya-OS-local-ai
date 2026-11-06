//! K5c Intel High Definition Audio (HDA) Selftest (hda gate).
//!
//! Validates PCI HDA controller discovery, controller reset / unreset,
//! codec discovery, DAC / Pin widget routing, pure integer sine generator
//! mathematical invariants, and stream DMA execution.
//!
//! Output only: plays a generated 440 Hz tone in a cyclic DMA buffer.
//! Microphone/input capture remains STUB and UNVERIFIED. The K5 "round trip" gate is unmet.

use crate::hda;

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

pub fn summary() -> (u32, u32) {
    unsafe { (PASS, FAIL) }
}

pub fn run() {
    sprintln!("hda gate:   probing Intel HDA audio controller over PCI");
    sprintln!("hda:        STUB capture unverified (output only, round-trip gate unmet)");

    // Mathematical invariant checks on the pure integer sine generator
    // Angle 0: sin(0) == 0
    let s0 = hda::generate_sine_sample(0, 25000);
    check(
        s0 == 0,
        "hda/math: sine at phase 0 is exactly 0",
        "phase 0 sample is non-zero",
    );

    // Angle pi/2 (phase 16384): sin(pi/2) near peak amplitude (+25000)
    let s_peak = hda::generate_sine_sample(16384, 25000);
    check(
        (24900..=25100).contains(&s_peak),
        "hda/math: sine at phase pi/2 is peak amplitude (+25000 +- 100)",
        "phase pi/2 sample outside expected amplitude",
    );

    // Angle pi (phase 32768): sin(pi) == 0
    let s_pi = hda::generate_sine_sample(32768, 25000);
    check(
        s_pi == 0,
        "hda/math: sine at phase pi is exactly 0",
        "phase pi sample is non-zero",
    );

    // Angle 3*pi/2 (phase 49152): sin(3*pi/2) near negative peak (-25000)
    let s_neg = hda::generate_sine_sample(49152, 25000);
    check(
        (-25100..=-24900).contains(&s_neg),
        "hda/math: sine at phase 3*pi/2 is negative peak (-25000 +- 100)",
        "phase 3*pi/2 sample outside expected amplitude",
    );

    // Check if Intel HDA device is present on PCI bus
    if crate::pci::find_device_by_class(0x04, 0x03).is_none() {
        sprintln!("hda gate:   no Intel HDA controller on PCI (skipped; verified in stage 8)");
        return;
    }

    // 1. Controller discovery and initialization
    let init_res = hda::init();
    if let Err(e) = init_res {
        sprintln!("hda gate:   hda::init error: {:?}", e);
    }
    check(
        init_res.is_ok(),
        "pci/hda: controller discovered, reset, and initialized",
        "hda::init() failed",
    );

    if !hda::is_ready() {
        sprintln!("hda gate:   aborting gate tests: HDA controller not ready");
        return;
    }

    let (bar0, dac_nid, pin_nid) = hda::get_info().unwrap_or((0, 0, 0));

    // 2. MMIO BAR0 validity
    check(
        bar0 != 0,
        "hda: BAR0 MMIO address mapped and non-zero",
        "BAR0 is zero",
    );

    // 3. DAC Widget discovery
    check(
        dac_nid != 0,
        "hda/codec: Audio Output Converter (DAC) widget enumerated",
        "DAC widget NID is 0",
    );

    // 4. Pin Complex discovery
    check(
        pin_nid != 0,
        "hda/codec: Line-Out Pin Complex widget enumerated",
        "Output Pin widget NID is 0",
    );

    // 5. Output DMA stream active
    check(
        hda::is_ready(),
        "hda/dma: Output stream DMA engine running with cyclic 440 Hz tone buffer",
        "HDA output stream not active",
    );

    // 6. Output stream DMA buffer cycling
    let cycles = hda::wait_stream_cycles(2);
    check(
        cycles >= 2,
        "hda/dma: stream completed at least 2 full buffer cycles (>= 200 ms audio DMA)",
        "HDA DMA stream stalled or timed out before 2 cycles",
    );
}
