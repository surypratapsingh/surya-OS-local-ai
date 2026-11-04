//! xHCI (eXtensible Host Controller Interface) & USB Mass Storage (BOT / SCSI).
//!
//! Provides PCI-discovered xHCI host controller initialization, root hub port
//! reset, slot allocation, device configuration, Bulk-Only Transport (BOT),
//! and SCSI commands (INQUIRY, READ CAPACITY, READ (10)).
//! Real pendrives stay UNVERIFIED (tested under QEMU qemu-xhci + usb-storage).

use crate::pci;
use crate::pmm;
use crate::ports;

pub const XHCI_BASE_OFFSET: u64 = 2 << 30; // 2 GiB above private_base (PDPTE index 2)

#[derive(Copy, Clone, Debug)]
pub enum UsbError {
    PciDeviceNotFound,
    ControllerResetTimeout,
    ControllerNotReadyTimeout,
    RunTimeout,
    NoDeviceConnected,
    PortResetFailed,
    EnableSlotFailed,
    AddressDeviceFailed,
    ControlTransferFailed,
    ConfigureEndpointFailed,
    MassStorageNotFound,
    BotCommandFailed,
    CswStatusFailed,
    BufferTooSmall,
    ReadOutOfRange,
}

#[inline]
unsafe fn mmio_read32(addr: u64) -> u32 {
    core::ptr::read_volatile(addr as *const u32)
}

#[inline]
unsafe fn mmio_write32(addr: u64, val: u32) {
    core::ptr::write_volatile(addr as *mut u32, val);
}

#[inline]
unsafe fn mmio_read64(addr: u64) -> u64 {
    core::ptr::read_volatile(addr as *const u64)
}

#[inline]
unsafe fn mmio_write64(addr: u64, val: u64) {
    core::ptr::write_volatile(addr as *mut u64, val);
}

struct TransferRing {
    phys: u64,
    cycle: u32,
    idx: usize,
}

impl TransferRing {
    fn new(phys: u64) -> Self {
        Self {
            phys,
            cycle: 1,
            idx: 0,
        }
    }

    fn push_trb(&mut self, hhdm: u64, dw0: u32, dw1: u32, dw2: u32, dw3_no_cycle: u32) {
        let trb_addr = hhdm + self.phys + (self.idx as u64) * 16;
        let dw3 = dw3_no_cycle | self.cycle;
        unsafe {
            core::ptr::write_volatile(trb_addr as *mut u32, dw0);
            core::ptr::write_volatile((trb_addr + 4) as *mut u32, dw1);
            core::ptr::write_volatile((trb_addr + 8) as *mut u32, dw2);
            core::ptr::write_volatile((trb_addr + 12) as *mut u32, dw3);
        }

        self.idx += 1;
        if self.idx == 255 {
            // Link TRB
            let link_addr = hhdm + self.phys + 255 * 16;
            let l_dw0 = (self.phys & 0xFFFF_FFFF) as u32;
            let l_dw1 = (self.phys >> 32) as u32;
            let l_dw2 = 0u32;
            let l_dw3 = (6 << 10) | (1 << 1) | self.cycle; // Link, Toggle Cycle
            unsafe {
                core::ptr::write_volatile(link_addr as *mut u32, l_dw0);
                core::ptr::write_volatile((link_addr + 4) as *mut u32, l_dw1);
                core::ptr::write_volatile((link_addr + 8) as *mut u32, l_dw2);
                core::ptr::write_volatile((link_addr + 12) as *mut u32, l_dw3);
            }
            self.cycle ^= 1;
            self.idx = 0;
        }
    }
}

pub struct XhciController {
    virt_base: u64,
    op_base: u64,
    db_base: u64,
    rts_base: u64,
    ctx_size: usize,
    hhdm: u64,
    phys_dcbaa: u64,
    phys_cmd_ring: u64,
    cmd_cycle: u32,
    cmd_idx: usize,
    phys_event_ring: u64,
    event_cycle: u32,
    event_idx: usize,

    // Connected device state
    slot_id: u8,
    dci_out: u8,
    dci_in: u8,
    out_ring: TransferRing,
    in_ring: TransferRing,
    ep0_ring: TransferRing,

    // DMA buffers
    phys_cbw: u64,
    phys_csw: u64,
    phys_dma_buf: u64,
    tag_counter: u32,

    // Mass storage info
    pub total_sectors: u64,
    pub sector_size: u32,
    pub vendor_str: [u8; 9],
    pub product_str: [u8; 17],
    pub ready: bool,
}

static mut CONTROLLER: Option<XhciController> = None;

impl XhciController {
    fn send_command(
        &mut self,
        param: u64,
        status: u32,
        control: u32,
    ) -> Result<(u32, u32, u32, u32), UsbError> {
        let trb_addr = self.hhdm + self.phys_cmd_ring + (self.cmd_idx as u64) * 16;
        let dw0 = (param & 0xFFFF_FFFF) as u32;
        let dw1 = (param >> 32) as u32;
        let dw2 = status;
        let dw3 = control | self.cmd_cycle;

        unsafe {
            core::ptr::write_volatile(trb_addr as *mut u32, dw0);
            core::ptr::write_volatile((trb_addr + 4) as *mut u32, dw1);
            core::ptr::write_volatile((trb_addr + 8) as *mut u32, dw2);
            core::ptr::write_volatile((trb_addr + 12) as *mut u32, dw3);
        }

        self.cmd_idx += 1;
        if self.cmd_idx == 255 {
            let link_addr = self.hhdm + self.phys_cmd_ring + 255 * 16;
            let l_dw0 = (self.phys_cmd_ring & 0xFFFF_FFFF) as u32;
            let l_dw1 = (self.phys_cmd_ring >> 32) as u32;
            let l_dw2 = 0u32;
            let l_dw3 = (6 << 10) | (1 << 1) | self.cmd_cycle;
            unsafe {
                core::ptr::write_volatile(link_addr as *mut u32, l_dw0);
                core::ptr::write_volatile((link_addr + 4) as *mut u32, l_dw1);
                core::ptr::write_volatile((link_addr + 8) as *mut u32, l_dw2);
                core::ptr::write_volatile((link_addr + 12) as *mut u32, l_dw3);
            }
            self.cmd_cycle ^= 1;
            self.cmd_idx = 0;
        }

        // Ring Command Doorbell (DB 0)
        unsafe {
            mmio_write32(self.db_base, 0);
        }

        // Wait for Command Completion Event (Type 33)
        loop {
            let (dw0, dw1, dw2, dw3) = self.wait_event()?;
            let ev_type = (dw3 >> 10) & 0x3F;
            if ev_type == 33 {
                return Ok((dw0, dw1, dw2, dw3));
            }
            // Skip non-command events (such as Port Status Change 34)
        }
    }

    fn wait_event(&mut self) -> Result<(u32, u32, u32, u32), UsbError> {
        for _ in 0..2_000_000 {
            let ev_addr = self.hhdm + self.phys_event_ring + (self.event_idx as u64) * 16;
            let ev_dw3 = unsafe { core::ptr::read_volatile((ev_addr + 12) as *const u32) };
            if (ev_dw3 & 1) == self.event_cycle {
                let ev_dw0 = unsafe { core::ptr::read_volatile(ev_addr as *const u32) };
                let ev_dw1 = unsafe { core::ptr::read_volatile((ev_addr + 4) as *const u32) };
                let ev_dw2 = unsafe { core::ptr::read_volatile((ev_addr + 8) as *const u32) };

                self.event_idx += 1;
                if self.event_idx == 256 {
                    self.event_idx = 0;
                    self.event_cycle ^= 1;
                }

                // Update ERDP with EHB (bit 3) set
                let erdp = self.phys_event_ring + (self.event_idx as u64) * 16;
                unsafe {
                    mmio_write64(self.rts_base + 0x20 + 0x18, erdp | (1 << 3));
                }

                return Ok((ev_dw0, ev_dw1, ev_dw2, ev_dw3));
            }
            ports::io_wait();
        }
        Err(UsbError::ControllerResetTimeout)
    }

    fn control_transfer(
        &mut self,
        bm_req_type: u8,
        b_req: u8,
        w_val: u16,
        w_idx: u16,
        w_len: u16,
        dir_in: bool,
    ) -> Result<(), UsbError> {
        let trt = if w_len == 0 {
            0 // No data stage
        } else if dir_in {
            3 // IN data stage
        } else {
            2 // OUT data stage
        };

        // 1. Setup Stage TRB (Type 2, IDT = 1)
        let s_dw0 = (bm_req_type as u32) | ((b_req as u32) << 8) | ((w_val as u32) << 16);
        let s_dw1 = (w_idx as u32) | ((w_len as u32) << 16);
        let s_dw2 = 8u32;
        let s_dw3 = (2 << 10) | (1 << 6) | (trt << 16);
        self.ep0_ring
            .push_trb(self.hhdm, s_dw0, s_dw1, s_dw2, s_dw3);

        // 2. Data Stage TRB (Type 3)
        if w_len > 0 {
            let dir_bit = if dir_in { 1 } else { 0 };
            let d_dw0 = (self.phys_dma_buf & 0xFFFF_FFFF) as u32;
            let d_dw1 = (self.phys_dma_buf >> 32) as u32;
            let d_dw2 = (w_len as u32) | (dir_bit << 16);
            let d_dw3 = (3 << 10) | (dir_bit << 16);
            self.ep0_ring
                .push_trb(self.hhdm, d_dw0, d_dw1, d_dw2, d_dw3);
        }

        // 3. Status Stage TRB (Type 4, IOC = 1)
        let status_dir = if dir_in && w_len > 0 { 0 } else { 1 };
        let st_dw0 = 0u32;
        let st_dw1 = 0u32;
        let st_dw2 = status_dir << 16;
        let st_dw3 = (4 << 10) | (1 << 5) | (status_dir << 16);
        self.ep0_ring
            .push_trb(self.hhdm, st_dw0, st_dw1, st_dw2, st_dw3);

        // Ring EP0 Doorbell (target = 1)
        unsafe {
            mmio_write32(self.db_base + (self.slot_id as u64) * 4, 1);
        }

        // Wait for Transfer Event
        let (ev_dw0, ev_dw1, ev_dw2, ev_dw3) = loop {
            let (dw0, dw1, dw2, dw3) = self.wait_event()?;
            let ev_type = (dw3 >> 10) & 0x3F;
            if ev_type == 32 {
                break (dw0, dw1, dw2, dw3);
            }
        };
        let comp_code = (ev_dw2 >> 24) & 0xFF;
        if comp_code != 1 {
            sprintln!(
                "xhci:       control transfer failed: comp_code={} trb_ptr={:#x} dw3={:#x}",
                comp_code,
                (ev_dw0 as u64) | ((ev_dw1 as u64) << 32),
                ev_dw3
            );
            return Err(UsbError::ControlTransferFailed);
        }
        Ok(())
    }

    fn bot_exec(&mut self, cdb: &[u8], dir_in: bool, transfer_len: u32) -> Result<(), UsbError> {
        self.tag_counter += 1;
        let tag = self.tag_counter;

        // 1. Prepare 31-byte CBW
        let cbw_ptr = (self.hhdm + self.phys_cbw) as *mut u8;
        unsafe {
            core::ptr::write_bytes(cbw_ptr, 0, 31);
            let p32 = cbw_ptr as *mut u32;
            core::ptr::write_volatile(p32, 0x4342_5355); // "USBC"
            core::ptr::write_volatile(p32.add(1), tag);
            core::ptr::write_volatile(p32.add(2), transfer_len);
            *cbw_ptr.add(12) = if dir_in { 0x80 } else { 0x00 };
            *cbw_ptr.add(13) = 0; // LUN
            *cbw_ptr.add(14) = cdb.len() as u8;
            for (i, &b) in cdb.iter().enumerate().take(16) {
                *cbw_ptr.add(15 + i) = b;
            }
        }

        // 2. Submit CBW on Bulk-Out Ring (Normal TRB, IOC = 1)
        let c_dw0 = (self.phys_cbw & 0xFFFF_FFFF) as u32;
        let c_dw1 = (self.phys_cbw >> 32) as u32;
        let c_dw2 = 31u32;
        let c_dw3 = (1 << 10) | (1 << 5);
        self.out_ring
            .push_trb(self.hhdm, c_dw0, c_dw1, c_dw2, c_dw3);

        unsafe {
            mmio_write32(
                self.db_base + (self.slot_id as u64) * 4,
                self.dci_out as u32,
            );
        }

        let (_, _, ev_dw2, _) = loop {
            let (dw0, dw1, dw2, dw3) = self.wait_event()?;
            let ev_type = (dw3 >> 10) & 0x3F;
            if ev_type == 32 {
                break (dw0, dw1, dw2, dw3);
            }
        };
        let comp_code = (ev_dw2 >> 24) & 0xFF;
        if comp_code != 1 {
            return Err(UsbError::BotCommandFailed);
        }

        // 3. Data Stage on Bulk-In (if any)
        if transfer_len > 0 && dir_in {
            let d_dw0 = (self.phys_dma_buf & 0xFFFF_FFFF) as u32;
            let d_dw1 = (self.phys_dma_buf >> 32) as u32;
            let d_dw2 = transfer_len;
            let d_dw3 = (1 << 10) | (1 << 5);
            self.in_ring.push_trb(self.hhdm, d_dw0, d_dw1, d_dw2, d_dw3);

            unsafe {
                mmio_write32(self.db_base + (self.slot_id as u64) * 4, self.dci_in as u32);
            }

            let (_, _, dev_dw2, _) = loop {
                let (dw0, dw1, dw2, dw3) = self.wait_event()?;
                let d_type = (dw3 >> 10) & 0x3F;
                if d_type == 32 {
                    break (dw0, dw1, dw2, dw3);
                }
            };
            let d_comp = (dev_dw2 >> 24) & 0xFF;
            if d_comp != 1 {
                return Err(UsbError::BotCommandFailed);
            }
        }

        // 4. CSW on Bulk-In (13 bytes)
        let s_dw0 = (self.phys_csw & 0xFFFF_FFFF) as u32;
        let s_dw1 = (self.phys_csw >> 32) as u32;
        let s_dw2 = 13u32;
        let s_dw3 = (1 << 10) | (1 << 5);
        self.in_ring.push_trb(self.hhdm, s_dw0, s_dw1, s_dw2, s_dw3);

        unsafe {
            mmio_write32(self.db_base + (self.slot_id as u64) * 4, self.dci_in as u32);
        }

        let (_, _, sev_dw2, _) = loop {
            let (dw0, dw1, dw2, dw3) = self.wait_event()?;
            let s_type = (dw3 >> 10) & 0x3F;
            if s_type == 32 {
                break (dw0, dw1, dw2, dw3);
            }
        };
        let s_comp = (sev_dw2 >> 24) & 0xFF;
        if s_comp != 1 {
            return Err(UsbError::BotCommandFailed);
        }

        // 5. Verify CSW
        let csw_ptr = (self.hhdm + self.phys_csw) as *const u8;
        unsafe {
            let p32 = csw_ptr as *const u32;
            let sig = core::ptr::read_volatile(p32);
            let ret_tag = core::ptr::read_volatile(p32.add(1));
            let status = *csw_ptr.add(12);

            if sig != 0x5342_5355 || ret_tag != tag || status != 0 {
                return Err(UsbError::CswStatusFailed);
            }
        }

        Ok(())
    }

    pub fn scsi_inquiry(&mut self) -> Result<(), UsbError> {
        let cdb = [0x12u8, 0, 0, 0, 36, 0];
        self.bot_exec(&cdb, true, 36)?;

        let dma_ptr = (self.hhdm + self.phys_dma_buf) as *const u8;
        unsafe {
            for i in 0..8 {
                self.vendor_str[i] = *dma_ptr.add(8 + i);
            }
            self.vendor_str[8] = 0;
            for i in 0..16 {
                self.product_str[i] = *dma_ptr.add(16 + i);
            }
            self.product_str[16] = 0;
        }
        Ok(())
    }

    pub fn scsi_read_capacity(&mut self) -> Result<(), UsbError> {
        let cdb = [0x25u8, 0, 0, 0, 0, 0, 0, 0, 0, 0];
        self.bot_exec(&cdb, true, 8)?;

        let dma_ptr = (self.hhdm + self.phys_dma_buf) as *const u8;
        unsafe {
            let last_lba = ((*dma_ptr as u32) << 24)
                | ((*dma_ptr.add(1) as u32) << 16)
                | ((*dma_ptr.add(2) as u32) << 8)
                | (*dma_ptr.add(3) as u32);
            let bsz = ((*dma_ptr.add(4) as u32) << 24)
                | ((*dma_ptr.add(5) as u32) << 16)
                | ((*dma_ptr.add(6) as u32) << 8)
                | (*dma_ptr.add(7) as u32);

            self.total_sectors = (last_lba as u64) + 1;
            self.sector_size = bsz;
        }
        Ok(())
    }

    pub fn scsi_read10(&mut self, lba: u64, count: u16, out: &mut [u8]) -> Result<(), UsbError> {
        if (count as usize) * 512 > out.len() {
            return Err(UsbError::BufferTooSmall);
        }

        let lba32 = lba as u32;
        let cdb = [
            0x28u8,
            0,
            (lba32 >> 24) as u8,
            (lba32 >> 16) as u8,
            (lba32 >> 8) as u8,
            lba32 as u8,
            0,
            (count >> 8) as u8,
            count as u8,
            0,
        ];

        let bytes = (count as u32) * 512;
        self.bot_exec(&cdb, true, bytes)?;

        let dma_ptr = (self.hhdm + self.phys_dma_buf) as *const u8;
        unsafe {
            for (i, slot) in out.iter_mut().enumerate().take(bytes as usize) {
                *slot = *dma_ptr.add(i);
            }
        }
        Ok(())
    }
}

pub fn init() -> Result<(), UsbError> {
    // 1. Locate xHCI Controller via PCI (Class 0x0C, Subclass 0x03, ProgIF 0x30)
    let pci_dev = match pci::find_device(0x0C, 0x03, 0x30) {
        Some(d) => d,
        None => {
            sprintln!("xhci:       PCI device 0C:03:30 not found");
            return Err(UsbError::PciDeviceNotFound);
        }
    };
    sprintln!(
        "xhci:       found controller at B:{:02x} D:{:02x} F:{} bar0={:#x}",
        pci_dev.bus,
        pci_dev.dev,
        pci_dev.func,
        pci_dev.bar0
    );

    pci::enable_bus_master_and_memory(pci_dev.bus, pci_dev.dev, pci_dev.func);
    let phys_bar0 = pci_dev.bar0;
    let virt_base = crate::paging::private_base() + XHCI_BASE_OFFSET;

    // Map 32 pages (128 KiB) for xHCI MMIO
    for i in 0..32 {
        crate::paging::map_page(virt_base + i * 4096, phys_bar0 + i * 4096);
    }

    let hhdm = crate::limine::hhdm_offset().expect("HHDM required for xHCI DMA");

    // 2. Read Capability Registers
    let caplength = unsafe { (mmio_read32(virt_base) & 0xFF) as u64 };
    let op_base = virt_base + caplength;
    let hcsparams1 = unsafe { mmio_read32(virt_base + 0x04) };
    let max_slots = (hcsparams1 & 0xFF) as usize;
    let max_ports = ((hcsparams1 >> 24) & 0xFF) as usize;

    let hcsparams2 = unsafe { mmio_read32(virt_base + 0x08) };
    let max_scratchpad =
        (((hcsparams2 >> 27) & 0x1F) | (((hcsparams2 >> 21) & 0x1F) << 5)) as usize;

    let hccparams1 = unsafe { mmio_read32(virt_base + 0x10) };
    let ctx_size = if (hccparams1 & (1 << 2)) != 0 { 64 } else { 32 };

    let dboff = unsafe { (mmio_read32(virt_base + 0x14) & !0x03) as u64 };
    let db_base = virt_base + dboff;

    let rtsoff = unsafe { (mmio_read32(virt_base + 0x18) & !0x1F) as u64 };
    let rts_base = virt_base + rtsoff;

    // 3. Reset Controller
    unsafe {
        let sts = mmio_read32(op_base + 0x04);
        if (sts & 1) == 0 {
            // Stop controller if running
            let mut cmd = mmio_read32(op_base);
            cmd &= !1;
            mmio_write32(op_base, cmd);
            for _ in 0..200_000 {
                if (mmio_read32(op_base + 0x04) & 1) != 0 {
                    break;
                }
                ports::io_wait();
            }
        }

        // Issue HCRST
        mmio_write32(op_base, 2);
        for _ in 0..200_000 {
            if (mmio_read32(op_base) & 2) == 0 {
                break;
            }
            ports::io_wait();
        }

        // Wait for CNR (Controller Not Ready) to clear
        for _ in 0..200_000 {
            if (mmio_read32(op_base + 0x04) & (1 << 11)) == 0 {
                break;
            }
            ports::io_wait();
        }

        // Set CONFIG MaxSlotsEn
        let slots_en = core::cmp::min(max_slots, 16) as u32;
        mmio_write32(op_base + 0x38, slots_en);
    }

    // 4. DCBAA and Scratchpad Buffers
    let phys_dcbaa = pmm::alloc_frame().expect("DCBAA frame");
    pmm::zero_frame(phys_dcbaa);

    if max_scratchpad > 0 {
        let phys_scratch_arr = pmm::alloc_frame().expect("Scratchpad array frame");
        pmm::zero_frame(phys_scratch_arr);
        for i in 0..max_scratchpad {
            let sbuf = pmm::alloc_frame().expect("Scratchpad buffer");
            pmm::zero_frame(sbuf);
            let ptr = (hhdm + phys_scratch_arr + (i as u64) * 8) as *mut u64;
            unsafe {
                core::ptr::write_volatile(ptr, sbuf);
            }
        }
        let dcbaa_p0 = (hhdm + phys_dcbaa) as *mut u64;
        unsafe {
            core::ptr::write_volatile(dcbaa_p0, phys_scratch_arr);
        }
    }

    unsafe {
        mmio_write64(op_base + 0x30, phys_dcbaa);
    }

    // 5. Command Ring
    let phys_cmd_ring = pmm::alloc_frame().expect("Cmd Ring frame");
    pmm::zero_frame(phys_cmd_ring);
    unsafe {
        mmio_write64(op_base + 0x18, phys_cmd_ring | 1); // RCS = 1
    }

    // 6. Event Ring & Interrupter 0
    let phys_event_ring = pmm::alloc_frame().expect("Event Ring frame");
    pmm::zero_frame(phys_event_ring);

    let phys_erst = pmm::alloc_frame().expect("ERST frame");
    pmm::zero_frame(phys_erst);

    unsafe {
        let erst_ptr = (hhdm + phys_erst) as *mut u64;
        core::ptr::write_volatile(erst_ptr, phys_event_ring);
        let erst_sz = (hhdm + phys_erst + 8) as *mut u32;
        core::ptr::write_volatile(erst_sz, 256);

        let intr0 = rts_base + 0x20;
        mmio_write32(intr0 + 0x08, 1); // ERSTSZ = 1
        mmio_write64(intr0 + 0x18, phys_event_ring); // ERDP
        mmio_write64(intr0 + 0x10, phys_erst); // ERSTBA
        mmio_write32(intr0, 3); // IMAN: IP | IE
    }

    // 7. Start Controller (R/S = 1)
    unsafe {
        let mut cmd = mmio_read32(op_base);
        cmd |= 1 | 4; // Run/Stop + Interrupter Enable
        mmio_write32(op_base, cmd);

        for _ in 0..200_000 {
            if (mmio_read32(op_base + 0x04) & 1) == 0 {
                break;
            }
            ports::io_wait();
        }
    }

    // 8. Port Detection & Reset
    let mut connected_port = 0u8;
    let mut port_speed = 0u32;

    for p in 1..=max_ports {
        let portsc_addr = op_base + 0x400 + ((p - 1) as u64) * 0x10;
        let mut portsc = unsafe { mmio_read32(portsc_addr) };
        if (portsc & 1) != 0 {
            // Initiate Port Reset: set bit 4 (PR), preserve PP (bit 9), clear change bits
            let write_val = (portsc & !(0x7F << 17)) | (1 << 4);
            unsafe {
                mmio_write32(portsc_addr, write_val);
            }

            for _ in 0..200_000 {
                portsc = unsafe { mmio_read32(portsc_addr) };
                if (portsc & (1 << 4)) == 0 {
                    break;
                }
                ports::io_wait();
            }

            if (portsc & (1 << 1)) != 0 {
                connected_port = p as u8;
                port_speed = (portsc >> 10) & 0xF;
                break;
            }
        }
    }

    if connected_port == 0 {
        return Err(UsbError::NoDeviceConnected);
    }

    // Allocate DMA buffers
    let phys_cbw = pmm::alloc_frame().expect("CBW frame");
    pmm::zero_frame(phys_cbw);
    let phys_csw = pmm::alloc_frame().expect("CSW frame");
    pmm::zero_frame(phys_csw);
    let phys_dma_buf = pmm::alloc_frame().expect("DMA buffer frame");
    pmm::zero_frame(phys_dma_buf);

    let phys_ep0_ring = pmm::alloc_frame().expect("EP0 ring frame");
    pmm::zero_frame(phys_ep0_ring);

    let mut controller = XhciController {
        virt_base,
        op_base,
        db_base,
        rts_base,
        ctx_size,
        hhdm,
        phys_dcbaa,
        phys_cmd_ring,
        cmd_cycle: 1,
        cmd_idx: 0,
        phys_event_ring,
        event_cycle: 1,
        event_idx: 0,

        slot_id: 0,
        dci_out: 0,
        dci_in: 0,
        out_ring: TransferRing::new(0),
        in_ring: TransferRing::new(0),
        ep0_ring: TransferRing::new(phys_ep0_ring),

        phys_cbw,
        phys_csw,
        phys_dma_buf,
        tag_counter: 100,

        total_sectors: 0,
        sector_size: 512,
        vendor_str: [0; 9],
        product_str: [0; 17],
        ready: false,
    };

    // 9. Enable Slot Command (TRB Type 9)
    let (_, _, dw2, dw3) = controller.send_command(0, 0, 9 << 10)?;
    let comp_code = (dw2 >> 24) & 0xFF;
    sprintln!(
        "xhci:       Enable Slot comp_code={} dw3={:#x}",
        comp_code,
        dw3
    );
    if comp_code != 1 {
        return Err(UsbError::EnableSlotFailed);
    }
    let slot_id = ((dw3 >> 24) & 0xFF) as u8;
    controller.slot_id = slot_id;

    // 10. Address Device Command (TRB Type 11)
    let phys_input_ctx = pmm::alloc_frame().expect("Input Context frame");
    pmm::zero_frame(phys_input_ctx);

    let phys_dev_ctx = pmm::alloc_frame().expect("Device Context frame");
    pmm::zero_frame(phys_dev_ctx);

    unsafe {
        let dcbaa_slot = (hhdm + phys_dcbaa + (slot_id as u64) * 8) as *mut u64;
        core::ptr::write_volatile(dcbaa_slot, phys_dev_ctx);

        // Input Control Context: add Slot (bit 0) and EP0 (bit 1)
        core::ptr::write_volatile((hhdm + phys_input_ctx + 4) as *mut u32, 3);

        // Slot Context:
        let slot_dw0 = ((port_speed & 0xF) << 20) | (1 << 27);
        let slot_dw1 = (connected_port as u32) << 16;
        core::ptr::write_volatile(
            (hhdm + phys_input_ctx + ctx_size as u64) as *mut u32,
            slot_dw0,
        );
        core::ptr::write_volatile(
            (hhdm + phys_input_ctx + ctx_size as u64 + 4) as *mut u32,
            slot_dw1,
        );

        // EP0 Context:
        let mps: u32 = if port_speed == 4 { 512 } else { 64 };
        let ep0_dw1 = (4 << 3) | (3 << 1) | (mps << 16);
        let ep0_dw2 = (phys_ep0_ring as u32) | 1; // DCS = 1
        let ep0_dw3 = (phys_ep0_ring >> 32) as u32;
        let ep0_dw4 = 8u32;
        let ep0_base = hhdm + phys_input_ctx + (2 * ctx_size) as u64;
        core::ptr::write_volatile((ep0_base + 4) as *mut u32, ep0_dw1);
        core::ptr::write_volatile((ep0_base + 8) as *mut u32, ep0_dw2);
        core::ptr::write_volatile((ep0_base + 12) as *mut u32, ep0_dw3);
        core::ptr::write_volatile((ep0_base + 16) as *mut u32, ep0_dw4);
    }

    sprintln!("xhci:       sending Address Device (slot_id={})", slot_id);
    let control = ((slot_id as u32) << 24) | (11 << 10);
    let (_, _, dw2, _) = controller.send_command(phys_input_ctx, 0, control)?;
    let addr_comp = (dw2 >> 24) & 0xFF;
    sprintln!("xhci:       Address Device comp_code={}", addr_comp);
    if addr_comp != 1 {
        return Err(UsbError::AddressDeviceFailed);
    }

    // 11. Read Device Descriptor (GET_DESCRIPTOR Device, 18 bytes)
    controller.control_transfer(0x80, 6, 0x0100, 0, 18, true)?;

    // 12. Read Configuration Descriptor (GET_DESCRIPTOR Config, 9 bytes then total)
    controller.control_transfer(0x80, 6, 0x0200, 0, 9, true)?;
    let dma_ptr = (hhdm + controller.phys_dma_buf) as *const u8;
    let total_len = unsafe { (*dma_ptr.add(2) as u16) | ((*dma_ptr.add(3) as u16) << 8) };

    let total_len = core::cmp::min(total_len, 512);
    controller.control_transfer(0x80, 6, 0x0200, 0, total_len, true)?;

    // Parse Configuration, Interface, and Endpoint Descriptors
    let mut config_val = 1u8;
    let mut ep_in_num = 0u8;
    let mut ep_in_mps = 512u16;
    let mut ep_out_num = 0u8;
    let mut ep_out_mps = 512u16;
    let mut found_mass_storage = false;

    unsafe {
        let mut idx = 0usize;
        let buf_len = total_len as usize;
        while idx + 2 <= buf_len {
            let len = *dma_ptr.add(idx) as usize;
            let dtype = *dma_ptr.add(idx + 1);
            if len == 0 || idx + len > buf_len {
                break;
            }

            if dtype == 2 {
                // Configuration Descriptor
                config_val = *dma_ptr.add(idx + 5);
            } else if dtype == 4 {
                // Interface Descriptor
                let class = *dma_ptr.add(idx + 5);
                let subclass = *dma_ptr.add(idx + 6);
                let proto = *dma_ptr.add(idx + 7);
                if class == 0x08 && subclass == 0x06 && proto == 0x50 {
                    found_mass_storage = true;
                }
            } else if dtype == 5 && found_mass_storage {
                // Endpoint Descriptor
                let ep_addr = *dma_ptr.add(idx + 2);
                let attr = *dma_ptr.add(idx + 3);
                let mps = (*dma_ptr.add(idx + 4) as u16) | ((*dma_ptr.add(idx + 5) as u16) << 8);

                if (attr & 3) == 2 {
                    // Bulk endpoint
                    if (ep_addr & 0x80) != 0 {
                        ep_in_num = ep_addr & 0x0F;
                        ep_in_mps = mps;
                    } else {
                        ep_out_num = ep_addr & 0x0F;
                        ep_out_mps = mps;
                    }
                }
            }
            idx += len;
        }
    }

    if !found_mass_storage || ep_in_num == 0 || ep_out_num == 0 {
        return Err(UsbError::MassStorageNotFound);
    }

    // 13. SET_CONFIGURATION
    controller.control_transfer(0x00, 9, config_val as u16, 0, 0, false)?;

    // 14. Configure Endpoint Command (TRB Type 12)
    let dci_out = ep_out_num * 2;
    let dci_in = ep_in_num * 2 + 1;
    let max_dci = core::cmp::max(dci_out, dci_in);

    controller.dci_out = dci_out;
    controller.dci_in = dci_in;

    let phys_out_ring = pmm::alloc_frame().expect("Out Ring frame");
    pmm::zero_frame(phys_out_ring);
    controller.out_ring = TransferRing::new(phys_out_ring);

    let phys_in_ring = pmm::alloc_frame().expect("In Ring frame");
    pmm::zero_frame(phys_in_ring);
    controller.in_ring = TransferRing::new(phys_in_ring);

    pmm::zero_frame(phys_input_ctx);
    unsafe {
        // Add flags: slot (bit 0) | dci_out | dci_in
        let add_flags = 1 | (1 << dci_out) | (1 << dci_in);
        core::ptr::write_volatile((hhdm + phys_input_ctx + 4) as *mut u32, add_flags);

        // Slot Context:
        let slot_dw0 = ((port_speed & 0xF) << 20) | ((max_dci as u32) << 27);
        core::ptr::write_volatile(
            (hhdm + phys_input_ctx + ctx_size as u64) as *mut u32,
            slot_dw0,
        );

        // Bulk-Out Endpoint Context:
        let out_ctx_base = hhdm + phys_input_ctx + ((dci_out as usize + 1) * ctx_size) as u64;
        let out_dw1 = (2 << 3) | (3 << 1) | ((ep_out_mps as u32) << 16); // Bulk Out
        let out_dw2 = (phys_out_ring as u32) | 1;
        let out_dw3 = (phys_out_ring >> 32) as u32;
        let out_dw4 = ep_out_mps as u32;
        core::ptr::write_volatile((out_ctx_base + 4) as *mut u32, out_dw1);
        core::ptr::write_volatile((out_ctx_base + 8) as *mut u32, out_dw2);
        core::ptr::write_volatile((out_ctx_base + 12) as *mut u32, out_dw3);
        core::ptr::write_volatile((out_ctx_base + 16) as *mut u32, out_dw4);

        // Bulk-In Endpoint Context:
        let in_ctx_base = hhdm + phys_input_ctx + ((dci_in as usize + 1) * ctx_size) as u64;
        let in_dw1 = (6 << 3) | (3 << 1) | ((ep_in_mps as u32) << 16); // Bulk In
        let in_dw2 = (phys_in_ring as u32) | 1;
        let in_dw3 = (phys_in_ring >> 32) as u32;
        let in_dw4 = ep_in_mps as u32;
        core::ptr::write_volatile((in_ctx_base + 4) as *mut u32, in_dw1);
        core::ptr::write_volatile((in_ctx_base + 8) as *mut u32, in_dw2);
        core::ptr::write_volatile((in_ctx_base + 12) as *mut u32, in_dw3);
        core::ptr::write_volatile((in_ctx_base + 16) as *mut u32, in_dw4);
    }

    let control = ((slot_id as u32) << 24) | (12 << 10);
    let (_, _, dw2, _) = controller.send_command(phys_input_ctx, 0, control)?;
    if ((dw2 >> 24) & 0xFF) != 1 {
        return Err(UsbError::ConfigureEndpointFailed);
    }

    // 15. SCSI INQUIRY & READ CAPACITY
    controller.scsi_inquiry()?;
    controller.scsi_read_capacity()?;
    controller.ready = true;

    unsafe {
        CONTROLLER = Some(controller);
    }

    Ok(())
}

pub fn is_ready() -> bool {
    unsafe {
        match (&raw const CONTROLLER).as_ref() {
            Some(Some(c)) => c.ready,
            _ => false,
        }
    }
}

pub fn get_vendor() -> &'static str {
    unsafe {
        match (&raw const CONTROLLER).as_ref() {
            Some(Some(c)) => core::str::from_utf8(&c.vendor_str[..8]).unwrap_or("?"),
            _ => "",
        }
    }
}

pub fn get_product() -> &'static str {
    unsafe {
        match (&raw const CONTROLLER).as_ref() {
            Some(Some(c)) => core::str::from_utf8(&c.product_str[..16]).unwrap_or("?"),
            _ => "",
        }
    }
}

pub fn get_total_sectors() -> u64 {
    unsafe {
        match (&raw const CONTROLLER).as_ref() {
            Some(Some(c)) => c.total_sectors,
            _ => 0,
        }
    }
}

pub fn get_sector_size() -> u32 {
    unsafe {
        match (&raw const CONTROLLER).as_ref() {
            Some(Some(c)) => c.sector_size,
            _ => 512,
        }
    }
}

pub fn read_blocks(lba: u64, count: u32, buf: &mut [u8]) -> Result<(), UsbError> {
    unsafe {
        let c = match (&raw mut CONTROLLER).as_mut() {
            Some(Some(c)) => c,
            _ => return Err(UsbError::PciDeviceNotFound),
        };

        let mut current_lba = lba;
        let mut offset = 0usize;
        let mut remaining = count;

        while remaining > 0 {
            // Transfer up to 64 sectors (32 KiB) at a time
            let chunk = core::cmp::min(remaining, 64) as u16;
            let chunk_bytes = (chunk as usize) * 512;
            c.scsi_read10(current_lba, chunk, &mut buf[offset..offset + chunk_bytes])?;
            current_lba += chunk as u64;
            offset += chunk_bytes;
            remaining -= chunk as u32;
        }

        Ok(())
    }
}

pub fn read_sector_512(lba: u64, buf: &mut [u8; 512]) -> bool {
    read_blocks(lba, 1, buf).is_ok()
}
