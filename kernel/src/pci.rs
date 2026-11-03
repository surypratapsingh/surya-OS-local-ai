//! PCI Configuration Space and Bus Enumeration.
//!
//! Provides read/write access to PCI configuration registers via x86 I/O
//! ports 0xCF8 (CONFIG_ADDRESS) and 0xCFC (CONFIG_DATA).
//! Derives device information (Vendor ID, Device ID, Class Code, Subclass,
//! ProgIF, BARs) and enables bus mastering and memory space access.

use crate::ports;

const PCI_CONFIG_ADDRESS: u16 = 0x0CF8;
const PCI_CONFIG_DATA: u16 = 0x0CFC;

#[derive(Copy, Clone, Debug)]
pub struct PciDevice {
    pub bus: u8,
    pub dev: u8,
    pub func: u8,
    pub vendor_id: u16,
    pub device_id: u16,
    pub class_code: u8,
    pub subclass: u8,
    pub prog_if: u8,
    pub bar0: u64,
}

#[inline]
fn pci_address(bus: u8, dev: u8, func: u8, offset: u8) -> u32 {
    0x8000_0000
        | ((bus as u32) << 16)
        | ((dev as u32) << 11)
        | ((func as u32) << 8)
        | ((offset as u32) & 0xFC)
}

pub fn read_config_u32(bus: u8, dev: u8, func: u8, offset: u8) -> u32 {
    ports::outl(PCI_CONFIG_ADDRESS, pci_address(bus, dev, func, offset));
    ports::inl(PCI_CONFIG_DATA)
}

pub fn write_config_u32(bus: u8, dev: u8, func: u8, offset: u8, val: u32) {
    ports::outl(PCI_CONFIG_ADDRESS, pci_address(bus, dev, func, offset));
    ports::outl(PCI_CONFIG_DATA, val);
}

pub fn read_config_u16(bus: u8, dev: u8, func: u8, offset: u8) -> u16 {
    let dword = read_config_u32(bus, dev, func, offset);
    let shift = ((offset & 2) as usize) * 8;
    ((dword >> shift) & 0xFFFF) as u16
}

pub fn write_config_u16(bus: u8, dev: u8, func: u8, offset: u8, val: u16) {
    let aligned_offset = offset & !3;
    let shift = ((offset & 2) as usize) * 8;
    let current = read_config_u32(bus, dev, func, aligned_offset);
    let mask = !(0xFFFF << shift);
    let new_val = (current & mask) | (((val as u32) & 0xFFFF) << shift);
    write_config_u32(bus, dev, func, aligned_offset, new_val);
}

pub fn read_config_u8(bus: u8, dev: u8, func: u8, offset: u8) -> u8 {
    let dword = read_config_u32(bus, dev, func, offset);
    let shift = ((offset & 3) as usize) * 8;
    ((dword >> shift) & 0xFF) as u8
}

/// Enable Bus Mastering (bit 2) and Memory Space (bit 1) in PCI Command register.
pub fn enable_bus_master_and_memory(bus: u8, dev: u8, func: u8) {
    let cmd = read_config_u16(bus, dev, func, 0x04);
    let new_cmd = cmd | (1 << 1) | (1 << 2);
    write_config_u16(bus, dev, func, 0x04, new_cmd);
}

/// Read BAR0 (supports 32-bit and 64-bit MMIO).
pub fn read_bar0(bus: u8, dev: u8, func: u8) -> u64 {
    let bar0 = read_config_u32(bus, dev, func, 0x10);
    if (bar0 & 1) != 0 {
        // I/O space BAR, not MMIO
        return (bar0 & !0x03) as u64;
    }
    let bar_type = (bar0 >> 1) & 0x03;
    if bar_type == 2 {
        // 64-bit memory space
        let bar1 = read_config_u32(bus, dev, func, 0x14);
        ((bar1 as u64) << 32) | ((bar0 as u64) & !0x0F)
    } else {
        // 32-bit memory space
        (bar0 & !0x0F) as u64
    }
}

/// Scan PCI bus for a controller matching (class_code, subclass, prog_if).
/// For example, xHCI is (0x0C, 0x03, 0x30).
pub fn find_device(class_code: u8, subclass: u8, prog_if: u8) -> Option<PciDevice> {
    for bus in 0..=31 {
        for dev in 0..32 {
            let vendor_id = read_config_u16(bus, dev, 0, 0x00);
            if vendor_id == 0xFFFF || vendor_id == 0x0000 {
                continue;
            }

            let header_type = read_config_u8(bus, dev, 0, 0x0E);
            let max_func = if (header_type & 0x80) != 0 { 8 } else { 1 };

            for func in 0..max_func {
                let vid = read_config_u16(bus, dev, func, 0x00);
                if vid == 0xFFFF || vid == 0x0000 {
                    continue;
                }

                let did = read_config_u16(bus, dev, func, 0x02);
                let cc = read_config_u8(bus, dev, func, 0x0B);
                let sc = read_config_u8(bus, dev, func, 0x0A);
                let pi = read_config_u8(bus, dev, func, 0x09);

                if cc == class_code && sc == subclass && pi == prog_if {
                    let bar0 = read_bar0(bus, dev, func);
                    return Some(PciDevice {
                        bus,
                        dev,
                        func,
                        vendor_id: vid,
                        device_id: did,
                        class_code: cc,
                        subclass: sc,
                        prog_if: pi,
                        bar0,
                    });
                }
            }
        }
    }
    None
}

/// Scan PCI bus for a device matching (class_code, subclass) regardless of prog_if.
pub fn find_device_by_class(class_code: u8, subclass: u8) -> Option<PciDevice> {
    for bus in 0..=31 {
        for dev in 0..32 {
            let vendor_id = read_config_u16(bus, dev, 0, 0x00);
            if vendor_id == 0xFFFF || vendor_id == 0x0000 {
                continue;
            }

            let header_type = read_config_u8(bus, dev, 0, 0x0E);
            let max_func = if (header_type & 0x80) != 0 { 8 } else { 1 };

            for func in 0..max_func {
                let vid = read_config_u16(bus, dev, func, 0x00);
                if vid == 0xFFFF || vid == 0x0000 {
                    continue;
                }

                let did = read_config_u16(bus, dev, func, 0x02);
                let cc = read_config_u8(bus, dev, func, 0x0B);
                let sc = read_config_u8(bus, dev, func, 0x0A);
                let pi = read_config_u8(bus, dev, func, 0x09);

                if cc == class_code && sc == subclass {
                    let bar0 = read_bar0(bus, dev, func);
                    return Some(PciDevice {
                        bus,
                        dev,
                        func,
                        vendor_id: vid,
                        device_id: did,
                        class_code: cc,
                        subclass: sc,
                        prog_if: pi,
                        bar0,
                    });
                }
            }
        }
    }
    None
}
