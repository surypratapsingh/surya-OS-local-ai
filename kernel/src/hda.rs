//! Intel High Definition Audio (HDA) Output Driver.
//!
//! Derived from Intel High Definition Audio Specification Revision 1.0a:
//! - §3.3: Controller Register Description (GCAP, GCTL, STATESTS, IC/IR/ICS, SDnCTL, etc.)
//! - §3.6.3: Buffer Descriptor List (BDL) format and alignment requirements
//! - §7.3.3: Codec Command Verb Summary (Get Parameter, Set Converter Format, Set Stream/Channel,
//!   Set Pin Widget Control, Set Amplifier Gain/Mute, Set Power State)
//!
//! Output only: plays a generated 440 Hz tone in a cyclic DMA buffer.
//! Microphone/input capture remains STUB and UNVERIFIED. The K5 "round trip" gate is unmet.

use crate::pci;
use crate::pmm;

pub const HDA_BASE_OFFSET: u64 = 3 << 30; // 3 GiB above private_base (PDPTE index 3)

/// Configurable tone frequency (440 Hz baseline; 880 Hz in sabotage plant test).
pub const TONE_FREQ_HZ: u32 = 440;

#[derive(Copy, Clone, Debug)]
pub enum HdaError {
    PciDeviceNotFound,
    ControllerResetTimeout,
    CodecNotDetected,
    ImmediateCommandBusy,
    ImmediateCommandTimeout,
    NoAudioFunctionGroup,
    NoDacWidget,
    NoOutputPinWidget,
    StreamResetTimeout,
}

#[inline]
unsafe fn mmio_read8(addr: u64) -> u8 {
    core::ptr::read_volatile(addr as *const u8)
}

#[inline]
unsafe fn mmio_write8(addr: u64, val: u8) {
    core::ptr::write_volatile(addr as *mut u8, val);
}

#[inline]
unsafe fn mmio_read16(addr: u64) -> u16 {
    core::ptr::read_volatile(addr as *const u16)
}

#[inline]
unsafe fn mmio_write16(addr: u64, val: u16) {
    core::ptr::write_volatile(addr as *mut u16, val);
}

#[inline]
unsafe fn mmio_read32(addr: u64) -> u32 {
    core::ptr::read_volatile(addr as *const u32)
}

#[inline]
unsafe fn mmio_write32(addr: u64, val: u32) {
    core::ptr::write_volatile(addr as *mut u32, val);
}

#[repr(C, packed)]
struct BdlEntry {
    addr_low: u32,
    addr_high: u32,
    len: u32,
    flags: u32,
}

pub struct HdaController {
    virt_base: u64,
    pub phys_bar0: u64,
    hhdm: u64,
    pub num_iss: u8,
    pub num_oss: u8,
    pub dac_nid: u8,
    pub out_pin_nid: u8,
    bdl_phys: u64,
    buffer_frames: [u64; 5],
    pub active: bool,
}

static mut CONTROLLER: Option<HdaController> = None;

/// Pure integer fixed-point Taylor series sine generator (Bhaskara / Taylor hybrid).
/// Input: phase in range [0, 65535] representing [0, 2*pi).
/// Target amplitude in range [0, 32767].
/// Output: signed 16-bit PCM amplitude.
pub fn generate_sine_sample(phase_u16: u16, target_amp: i32) -> i16 {
    let quad = (phase_u16 >> 14) & 3;
    let frac = (phase_u16 & 0x3FFF) as i64;
    let (x, sign) = match quad {
        0 => (frac, 1i64),
        1 => (16384 - frac, 1i64),
        2 => (frac, -1i64),
        _ => (16384 - frac, -1i64),
    };
    // x in [0, 16384]. Angle in radians = x * (pi/2) / 16384.
    // In Q28 fixed-point: pi/2 = 421657428.
    let pi_over_2_q28: i64 = 421657428;
    let rad_q28 = (x * pi_over_2_q28) >> 14;
    let r2_q28 = (rad_q28 * rad_q28) >> 28;
    let r3_q28 = ((r2_q28 * rad_q28) >> 28) / 6;
    let r4_q28 = (r2_q28 * r2_q28) >> 28;
    let r5_q28 = ((r4_q28 * rad_q28) >> 28) / 120;
    let r7_q28 = ((((r4_q28 * r2_q28) >> 28) * rad_q28) >> 28) / 5040;
    let s_q28 = rad_q28 - r3_q28 + r5_q28 - r7_q28;
    let amp = ((s_q28 * (target_amp as i64)) >> 28) as i16;
    if sign > 0 {
        amp
    } else {
        -amp
    }
}

impl HdaController {
    /// Send a command verb via Immediate Command Interface (HDA Spec §3.3.26 - §3.3.28).
    fn send_verb(&self, codec: u8, nid: u8, verb: u32, payload: u32) -> Result<u32, HdaError> {
        unsafe {
            // 1. Wait for ICB (bit 0 of ICS at offset 0x68) to be 0
            for _ in 0..100_000 {
                if (mmio_read16(self.virt_base + 0x68) & 1) == 0 {
                    break;
                }
            }
            if (mmio_read16(self.virt_base + 0x68) & 1) != 0 {
                return Err(HdaError::ImmediateCommandBusy);
            }

            // 2. Clear IRV (bit 1 of ICS) by writing 1 to it (W1C)
            mmio_write16(self.virt_base + 0x68, 2);

            // 3. Assemble 32-bit command word (HDA Spec §7.3.1)
            let cmd = if verb >= 0x10 {
                // 12-bit verb (with 8-bit payload)
                ((codec as u32) << 28)
                    | ((nid as u32) << 20)
                    | ((verb & 0xFFF) << 8)
                    | (payload & 0xFF)
            } else {
                // 4-bit verb (with 16-bit payload)
                ((codec as u32) << 28)
                    | ((nid as u32) << 20)
                    | ((verb & 0x0F) << 16)
                    | (payload & 0xFFFF)
            };

            // 4. Write command to ICW (offset 0x60)
            mmio_write32(self.virt_base + 0x60, cmd);

            // 5. Trigger execution: write 1 to ICB in ICS (offset 0x68)
            mmio_write16(self.virt_base + 0x68, 1);

            // 6. Wait for ICB to clear and IRV to become 1
            for _ in 0..100_000 {
                let ics = mmio_read16(self.virt_base + 0x68);
                if (ics & 1) == 0 && (ics & 2) != 0 {
                    let resp = mmio_read32(self.virt_base + 0x64);
                    return Ok(resp);
                }
            }

            Err(HdaError::ImmediateCommandTimeout)
        }
    }
}

pub fn is_ready() -> bool {
    unsafe {
        match (&raw const CONTROLLER).as_ref() {
            Some(Some(c)) => c.active,
            _ => false,
        }
    }
}

pub fn stream_lpib() -> u32 {
    unsafe {
        match (&raw const CONTROLLER).as_ref() {
            Some(Some(c)) => {
                let sd_base = c.virt_base + 0x80 + (c.num_iss as u64) * 0x20;
                mmio_read32(sd_base + 0x04)
            }
            _ => 0,
        }
    }
}

pub fn stream_status() -> u8 {
    unsafe {
        match (&raw const CONTROLLER).as_ref() {
            Some(Some(c)) => {
                let sd_base = c.virt_base + 0x80 + (c.num_iss as u64) * 0x20;
                mmio_read8(sd_base + 0x03)
            }
            _ => 0,
        }
    }
}

/// Wait until at least `cycles` full passes of the 19,200-byte buffer (each pass = 100 ms) have elapsed.
/// Returns the number of completed cycles.
pub fn wait_stream_cycles(cycles: u32) -> u32 {
    unsafe {
        match (&raw const CONTROLLER).as_ref() {
            Some(Some(c)) => {
                let sd_base = c.virt_base + 0x80 + (c.num_iss as u64) * 0x20;
                let mut completed = 0u32;
                let mut prev_pos = mmio_read32(sd_base + 0x04);
                let mut timeout = 20_000_000usize;
                while completed < cycles && timeout > 0 {
                    timeout -= 1;
                    let cur_pos = mmio_read32(sd_base + 0x04);
                    if cur_pos < prev_pos && prev_pos > 9600 && cur_pos < 9600 {
                        completed += 1;
                    }
                    prev_pos = cur_pos;
                    core::hint::spin_loop();
                }
                completed
            }
            _ => 0,
        }
    }
}

pub fn get_info() -> Option<(u64, u8, u8)> {
    unsafe {
        match (&raw const CONTROLLER).as_ref() {
            Some(Some(c)) => Some((c.phys_bar0, c.dac_nid, c.out_pin_nid)),
            _ => None,
        }
    }
}

pub fn init() -> Result<(), HdaError> {
    // 1. Scan PCI for Multimedia Audio Controller (Class 0x04, Subclass 0x03)
    let pci_dev = match pci::find_device_by_class(0x04, 0x03) {
        Some(d) => d,
        None => return Err(HdaError::PciDeviceNotFound),
    };

    sprintln!(
        "hda:        found controller at B:{:02x} D:{:02x} F:{} bar0={:#x}",
        pci_dev.bus,
        pci_dev.dev,
        pci_dev.func,
        pci_dev.bar0
    );

    pci::enable_bus_master_and_memory(pci_dev.bus, pci_dev.dev, pci_dev.func);
    let phys_bar0 = pci_dev.bar0;
    let virt_base = crate::paging::private_base() + HDA_BASE_OFFSET;

    // Map 4 pages (16 KiB) for HDA MMIO registers
    for i in 0..4 {
        crate::paging::map_page(virt_base + i * 4096, phys_bar0 + i * 4096);
    }

    let hhdm = crate::limine::hhdm_offset().expect("HHDM required for HDA DMA");

    // 2. Controller Reset (HDA Spec §3.3.7 GCTL.CRST)
    unsafe {
        let mut gctl = mmio_read32(virt_base + 0x08);
        if (gctl & 1) != 0 {
            // Bring into reset first to clear previous state
            mmio_write32(virt_base + 0x08, gctl & !1);
            let mut reset_ok = false;
            for _ in 0..100_000 {
                if (mmio_read32(virt_base + 0x08) & 1) == 0 {
                    reset_ok = true;
                    break;
                }
            }
            if !reset_ok {
                return Err(HdaError::ControllerResetTimeout);
            }
        }

        // Take controller out of reset
        gctl = mmio_read32(virt_base + 0x08);
        mmio_write32(virt_base + 0x08, gctl | 1);
        let mut unreset_ok = false;
        for _ in 0..100_000 {
            if (mmio_read32(virt_base + 0x08) & 1) != 0 {
                unreset_ok = true;
                break;
            }
        }
        if !unreset_ok {
            return Err(HdaError::ControllerResetTimeout);
        }

        // Wait for codecs to initialize on the link and assert STATESTS
        for _ in 0..10_000 {
            core::hint::spin_loop();
        }

        let statests = mmio_read16(virt_base + 0x0E);
        if (statests & 1) == 0 {
            // Codec 0 did not assert presence
            sprintln!(
                "hda:        warning: codec 0 not detected in STATESTS ({:#x})",
                statests
            );
        }
    }

    // 3. Read Global Capabilities (HDA Spec §3.3.1 GCAP)
    let gcap = unsafe { mmio_read16(virt_base) };
    let num_iss = ((gcap >> 8) & 0x0F) as u8;
    let num_oss = ((gcap >> 12) & 0x0F) as u8;

    sprintln!("hda:        capabilities: ISS={} OSS={}", num_iss, num_oss);

    let mut controller = HdaController {
        virt_base,
        phys_bar0,
        hhdm,
        num_iss,
        num_oss,
        dac_nid: 0,
        out_pin_nid: 0,
        bdl_phys: 0,
        buffer_frames: [0; 5],
        active: false,
    };

    // 4. Codec discovery and topology enumeration
    // Root Node (NID 0): get Subordinate Node Count (Verb 0xF00, param 0x04)
    let root_sub = controller.send_verb(0, 0, 0xF00, 0x04)?;
    let afg_start = ((root_sub >> 16) & 0xFF) as u8;
    let afg_count = (root_sub & 0xFF) as u8;

    if afg_count == 0 {
        return Err(HdaError::NoAudioFunctionGroup);
    }

    let afg_nid = afg_start;

    // Power up Audio Function Group: Set Power State D0 (Verb 0x705, payload 0)
    let _ = controller.send_verb(0, afg_nid, 0x705, 0);

    // Get AFG Subordinate Node Count (widgets)
    let afg_sub = controller.send_verb(0, afg_nid, 0xF00, 0x04)?;
    let wid_start = ((afg_sub >> 16) & 0xFF) as u8;
    let wid_count = (afg_sub & 0xFF) as u8;

    for i in 0..wid_count {
        let nid = wid_start + i;
        // Power up widget
        let _ = controller.send_verb(0, nid, 0x705, 0);

        // Get Audio Widget Capabilities (Verb 0xF00, param 0x09)
        let cap = match controller.send_verb(0, nid, 0xF00, 0x09) {
            Ok(c) => c,
            Err(_) => continue,
        };
        let widget_type = (cap >> 20) & 0x0F;

        if widget_type == 0 && controller.dac_nid == 0 {
            // Audio Output Converter (DAC)
            controller.dac_nid = nid;
        } else if widget_type == 4 && controller.out_pin_nid == 0 {
            // Pin Complex: check Pin Capabilities (Verb 0xF00, param 0x0C)
            let pincap = controller.send_verb(0, nid, 0xF00, 0x0C).unwrap_or(0);
            if (pincap & (1 << 4)) != 0 || pincap == 0 {
                // Output capable (bit 4)
                controller.out_pin_nid = nid;
            }
        }
    }

    if controller.dac_nid == 0 {
        return Err(HdaError::NoDacWidget);
    }
    if controller.out_pin_nid == 0 {
        return Err(HdaError::NoOutputPinWidget);
    }

    sprintln!(
        "hda:        audio path configured: DAC NID {} -> Out Pin NID {}",
        controller.dac_nid,
        controller.out_pin_nid
    );

    // 5. Configure DAC and Pin Widgets
    // Format 0x0011 = 48 kHz, 16-bit, 2 channels (stereo)
    controller.send_verb(0, controller.dac_nid, 0x2, 0x0011)?;
    // Set Stream 1, Channel 0 (Verb 0x706, payload 0x10)
    controller.send_verb(0, controller.dac_nid, 0x706, 0x10)?;
    // Unmute DAC amplifier (Verb 0x3, payload 0xB07F = Left, Right, OutAmp, Unmute, 0dB/max gain)
    let _ = controller.send_verb(0, controller.dac_nid, 0x3, 0xB07F);

    // Pin Control: OutEn enabled (Verb 0x707, payload 0x40)
    controller.send_verb(0, controller.out_pin_nid, 0x707, 0x40)?;
    // Unmute Pin amplifier
    let _ = controller.send_verb(0, controller.out_pin_nid, 0x3, 0xB07F);

    // 6. Allocate DMA memory: BDL frame + 5 audio buffer frames
    let bdl_phys = pmm::alloc_frame().expect("out of memory for HDA BDL");
    controller.bdl_phys = bdl_phys;
    let bdl_virt = hhdm + bdl_phys;
    unsafe {
        core::ptr::write_bytes(bdl_virt as *mut u8, 0, 4096);
    }

    for f in 0..5 {
        let frame_phys = pmm::alloc_frame().expect("out of memory for HDA audio frame");
        controller.buffer_frames[f] = frame_phys;
        let frame_virt = hhdm + frame_phys;
        unsafe {
            core::ptr::write_bytes(frame_virt as *mut u8, 0, 4096);
        }
    }

    // 7. Synthesize mathematically seamless 440 Hz sine wave into buffers
    // 5 frames * 3840 bytes = 19,200 bytes = 4800 stereo frames (0.1000 sec).
    // In 4800 stereo frames at 48 kHz, 440 Hz tone completes EXACTLY 44 full cycles.
    // Each buffer entry is 3840 bytes (exact multiple of 128: 30 * 128).
    let phase_inc = (((TONE_FREQ_HZ as u64) * 4294967296u64) / 48000) as u32;
    for f in 0..5 {
        let frame_virt = hhdm + controller.buffer_frames[f];
        let frame_ptr = frame_virt as *mut i16;
        let base_sample = (f * 960) as u32;

        for s in 0..960usize {
            let global_sample = base_sample + (s as u32);
            let phase_u32 = global_sample.wrapping_mul(phase_inc);
            let phase_u16 = (phase_u32 >> 16) as u16;
            let sample = generate_sine_sample(phase_u16, 25000);

            unsafe {
                // Interleaved stereo: left = sample, right = sample
                *frame_ptr.add(s * 2) = sample;
                *frame_ptr.add(s * 2 + 1) = sample;
            }
        }
    }

    // 8. Populate BDL table (5 entries of 3840 bytes each)
    let bdl_entries = bdl_virt as *mut BdlEntry;
    for f in 0..5 {
        let phys = controller.buffer_frames[f];
        unsafe {
            let entry = &mut *bdl_entries.add(f);
            entry.addr_low = (phys & 0xFFFF_FFFF) as u32;
            entry.addr_high = (phys >> 32) as u32;
            entry.len = 3840;
            entry.flags = 0;
        }
    }

    // 9. Configure Stream Descriptor (Output Stream 0)
    // First output stream starts at 0x80 + (ISS * 0x20) (HDA Spec §3.3.35)
    let sd_base = virt_base + 0x80 + (controller.num_iss as u64) * 0x20;

    unsafe {
        // Stop stream
        mmio_write8(sd_base, 0);
        for _ in 0..10_000 {
            if (mmio_read8(sd_base) & 0x02) == 0 {
                break;
            }
        }

        // Reset stream (SRST)
        mmio_write8(sd_base, 0x01);
        for _ in 0..10_000 {
            if (mmio_read8(sd_base) & 0x01) != 0 {
                break;
            }
        }
        mmio_write8(sd_base, 0x00);
        for _ in 0..10_000 {
            if (mmio_read8(sd_base) & 0x01) == 0 {
                break;
            }
        }

        // Clear status interrupt and error flags (BCIS=bit 2, DESE=bit 4, FIFOE=bit 5)
        mmio_write8(sd_base + 0x03, 0x3C);

        // Set Stream ID = 1 in SDnCTL byte 2
        mmio_write8(sd_base + 0x02, 1 << 4);

        // Set Format: 0x0011 (48 kHz, 16-bit, stereo)
        mmio_write16(sd_base + 0x12, 0x0011);

        // Program BDL physical address
        mmio_write32(sd_base + 0x18, (bdl_phys & 0xFFFF_FFFF) as u32);
        mmio_write32(sd_base + 0x1C, (bdl_phys >> 32) as u32);

        // Cyclic Buffer Length: 5 * 3840 = 19200 bytes
        mmio_write32(sd_base + 0x08, 19200);

        // Last Valid Index (LVI): 5 entries - 1 = 4
        mmio_write16(sd_base + 0x0C, 4);

        // Start DMA engine: RUN bit (bit 1)
        mmio_write8(sd_base, 0x02);
    }

    controller.active = true;
    unsafe {
        CONTROLLER = Some(controller);
    }

    sprintln!(
        "hda:        output stream active (48 kHz 16-bit stereo, {} Hz generated tone)",
        TONE_FREQ_HZ
    );
    sprintln!("hda:        STUB capture unverified (output only, round-trip gate unmet)");

    Ok(())
}
