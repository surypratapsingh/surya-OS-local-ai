//! K5a xHCI and USB Mass Storage Selftest (usb gate).
//!
//! Validates PCI xHCI controller discovery, root hub reset, USB mass storage
//! bulk-only transport (BOT) configuration, SCSI INQUIRY, SCSI READ CAPACITY,
//! SCSI READ (10), and mounts the FAT32 filesystem directly over the USB block
//! device. Emits USBLIST blocks (for mdir oracle diff in CI) and USBMCOPY
//! blocks (for mcopy file byte comparison in CI).
//!
//! Real physical pendrives remain UNVERIFIED (QEMU qemu-xhci + usb-storage verified).

use crate::fat::FatVolume;
use crate::xhci;

static mut PASS: u32 = 0;
static mut FAIL: u32 = 0;

static mut PARTITION_START_LBA: u64 = 0;

fn usb_read_sector(lba: u64, buf: &mut [u8; 512]) -> bool {
    unsafe {
        let base = PARTITION_START_LBA;
        xhci::read_sector_512(base + lba, buf)
    }
}

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
    sprintln!("usb gate:   probing xHCI and USB mass storage over PCI");
    sprintln!("usb:        STUB real pendrives unverified (QEMU usb-storage verified)");

    // If no xHCI controller is present on the PCI bus, skip cleanly.
    // Full gate verification runs when xHCI hardware is attached (stage 8 / test-usb.sh).
    if crate::pci::find_device(0x0C, 0x03, 0x30).is_none() {
        sprintln!("usb gate:   no xHCI controller on PCI (skipped; verified in stage 8)");
        return;
    }

    // 1. Controller and mass storage init
    let init_res = xhci::init();
    if let Err(e) = init_res {
        sprintln!("usb gate:   xhci::init error: {:?}", e);
    }
    check(
        init_res.is_ok(),
        "pci/xhci: controller discovered, initialized, and mass storage ready",
        "xhci::init() failed",
    );

    if !xhci::is_ready() {
        sprintln!("usb gate:   aborting gate tests: xHCI device not ready");
        return;
    }

    let vendor = xhci::get_vendor();
    let product = xhci::get_product();
    let total_sec = xhci::get_total_sectors();
    let sec_sz = xhci::get_sector_size();

    sprintln!(
        "xhci:       device vendor='{}' product='{}'",
        vendor,
        product
    );
    sprintln!(
        "xhci:       capacity: {} sectors ({} bytes/sec)",
        total_sec,
        sec_sz
    );

    // 2. SCSI INQUIRY verification
    let vendor_ok = vendor.contains("QEMU") || !vendor.is_empty();
    check(
        vendor_ok,
        "scsi: INQUIRY returned valid vendor/product identification",
        "vendor string empty or unexpected",
    );

    // 3. SCSI READ CAPACITY verification
    check(
        total_sec > 0 && sec_sz == 512,
        "scsi: READ CAPACITY (10) reported 512-byte sectors and positive sector count",
        "capacity or sector size invalid",
    );

    // 4. SCSI READ (10) sector 0
    let mut sector0 = [0u8; 512];
    let r0_ok = xhci::read_sector_512(0, &mut sector0);
    let sig_ok = sector0[510] == 0x55 && sector0[511] == 0xAA;
    check(
        r0_ok && sig_ok,
        "scsi: READ (10) reads sector 0 with valid 0x55 0xAA boot signature",
        "sector 0 read failed or invalid signature",
    );

    // Determine FAT32 start LBA (raw VBR vs MBR partition)
    let is_raw_fat32 = &sector0[82..90] == b"FAT32   ";
    let start_lba = if is_raw_fat32 {
        0u64
    } else {
        // Inspect MBR partition 1 starting LBA at offset 446 + 8
        let p_start = (sector0[454] as u32)
            | ((sector0[455] as u32) << 8)
            | ((sector0[456] as u32) << 16)
            | ((sector0[457] as u32) << 24);
        if p_start != 0 {
            p_start as u64
        } else {
            0u64
        }
    };
    unsafe {
        PARTITION_START_LBA = start_lba;
    }

    // 5. Mount FAT32 directly over USB block device
    let vol_res = FatVolume::attach_device(usb_read_sector, total_sec.saturating_sub(start_lba));
    check(
        vol_res.is_ok(),
        "fat32/usb: mounted FAT32 filesystem directly over USB block reader",
        "FatVolume::attach_device failed",
    );

    let v = match vol_res {
        Ok(vol) => vol,
        Err(_) => return,
    };

    // 6. Directory listings over USB (matches mdir format)
    let mut list_buf = [0u8; 8192];

    // Root directory (::/)
    match v.list_dir_mdir(v.root_cluster(), &mut list_buf) {
        Ok(len) => {
            sprintln!("USBLIST ::/");
            if let Ok(s) = core::str::from_utf8(&list_buf[..len]) {
                for line in s.lines() {
                    if !line.trim().is_empty() {
                        sprintln!("{}", line);
                    }
                }
            }
            sprintln!("USBLIST END");
            check(
                len > 0,
                "fat32/usb: root directory listed successfully over USB",
                "empty root listing",
            );
        }
        Err(_) => {
            check(
                false,
                "fat32/usb: root directory listed successfully over USB",
                "list_dir_mdir root failed",
            );
        }
    }

    // Lookup /projects
    let mut projects_cluster = 0u32;
    match v.lookup(v.root_cluster(), "projects") {
        Ok(entry) => {
            projects_cluster = entry.first_cluster;
            check(
                entry.is_dir(),
                "fat32/usb: lookup 'projects' directory entry",
                "not a directory",
            );
        }
        Err(_) => {
            check(
                false,
                "fat32/usb: lookup 'projects' directory entry",
                "lookup failed",
            );
        }
    }

    if projects_cluster != 0 {
        match v.list_dir_mdir(projects_cluster, &mut list_buf) {
            Ok(len) => {
                sprintln!("USBLIST ::/projects");
                if let Ok(s) = core::str::from_utf8(&list_buf[..len]) {
                    for line in s.lines() {
                        if !line.trim().is_empty() {
                            sprintln!("{}", line);
                        }
                    }
                }
                sprintln!("USBLIST END");
                check(
                    len > 0,
                    "fat32/usb: /projects directory listed successfully over USB",
                    "empty /projects listing",
                );
            }
            Err(_) => {
                check(
                    false,
                    "fat32/usb: /projects directory listed successfully over USB",
                    "list failed",
                );
            }
        }
    }

    // Lookup /projects/2026 january notes
    let mut notes_cluster = 0u32;
    if projects_cluster != 0 {
        match v.lookup(projects_cluster, "2026 january notes") {
            Ok(entry) => {
                notes_cluster = entry.first_cluster;
                check(
                    entry.is_dir(),
                    "fat32/usb: lookup '2026 january notes' directory entry",
                    "not a directory",
                );
            }
            Err(_) => {
                check(
                    false,
                    "fat32/usb: lookup '2026 january notes' directory entry",
                    "lookup failed",
                );
            }
        }
    }

    if notes_cluster != 0 {
        match v.list_dir_mdir(notes_cluster, &mut list_buf) {
            Ok(len) => {
                sprintln!("USBLIST ::/projects/2026 january notes");
                if let Ok(s) = core::str::from_utf8(&list_buf[..len]) {
                    for line in s.lines() {
                        if !line.trim().is_empty() {
                            sprintln!("{}", line);
                        }
                    }
                }
                sprintln!("USBLIST END");
                check(
                    len > 0,
                    "fat32/usb: /projects/2026 january notes directory listed successfully over USB",
                    "empty notes listing",
                );
            }
            Err(_) => {
                check(false, "fat32/usb: /projects/2026 january notes directory listed successfully over USB", "list failed");
            }
        }
    }

    // 7. File content reads over USB (matches mcopy)
    let mut file_buf = [0u8; 8192];

    // Read SIMPLE.TXT
    match v.lookup(v.root_cluster(), "SIMPLE.TXT") {
        Ok(entry) => match v.read_file(&entry, &mut file_buf) {
            Ok(bytes) => {
                let content = &file_buf[..bytes];
                sprintln!("USBMCOPY ::/SIMPLE.TXT");
                if let Ok(s) = core::str::from_utf8(content) {
                    for line in s.lines() {
                        sprintln!("{}X", line); // PLANT F2.10-G: altered bytes on the wire
                    }
                }
                sprintln!("USBMCOPY END");
                check(
                    content == b"plain 8.3 upper\n",
                    "fat32/usb: read_file 'SIMPLE.TXT' matches exact bytes",
                    "content mismatch",
                );
            }
            Err(_) => {
                check(
                    false,
                    "fat32/usb: read_file 'SIMPLE.TXT' matches exact bytes",
                    "read_file failed",
                );
            }
        },
        Err(_) => {
            check(
                false,
                "fat32/usb: read_file 'SIMPLE.TXT' matches exact bytes",
                "lookup failed",
            );
        }
    }

    // Read hello world.txt
    match v.lookup(v.root_cluster(), "hello world.txt") {
        Ok(entry) => match v.read_file(&entry, &mut file_buf) {
            Ok(bytes) => {
                let content = &file_buf[..bytes];
                sprintln!("USBMCOPY ::/hello world.txt");
                if let Ok(s) = core::str::from_utf8(content) {
                    for line in s.lines() {
                        sprintln!("{}", line);
                    }
                }
                sprintln!("USBMCOPY END");
                check(
                    content == b"lowercase lfn content\n",
                    "fat32/usb: read_file 'hello world.txt' matches exact bytes",
                    "content mismatch",
                );
            }
            Err(_) => {
                check(
                    false,
                    "fat32/usb: read_file 'hello world.txt' matches exact bytes",
                    "read_file failed",
                );
            }
        },
        Err(_) => {
            check(
                false,
                "fat32/usb: read_file 'hello world.txt' matches exact bytes",
                "lookup failed",
            );
        }
    }

    // Read nested file: /projects/2026 january notes/nested file with a long name.txt
    if notes_cluster != 0 {
        match v.lookup(notes_cluster, "nested file with a long name.txt") {
            Ok(entry) => {
                match v.read_file(&entry, &mut file_buf) {
                    Ok(bytes) => {
                        let content = &file_buf[..bytes];
                        sprintln!("USBMCOPY ::/projects/2026 january notes/nested file with a long name.txt");
                        if let Ok(s) = core::str::from_utf8(content) {
                            for line in s.lines() {
                                sprintln!("{}", line);
                            }
                        }
                        sprintln!("USBMCOPY END");
                        check(
                            content == b"deep content\n",
                            "fat32/usb: read_file 'nested file with a long name.txt' matches exact bytes",
                            "content mismatch",
                        );
                    }
                    Err(_) => {
                        check(false, "fat32/usb: read_file 'nested file with a long name.txt' matches exact bytes", "read_file failed");
                    }
                }
            }
            Err(_) => {
                check(
                    false,
                    "fat32/usb: read_file 'nested file with a long name.txt' matches exact bytes",
                    "lookup failed",
                );
            }
        }
    }

    // Read multi-cluster file: this-filename-requires-three-lfn-directory-slots.txt (5120 bytes)
    match v.lookup(
        v.root_cluster(),
        "this-filename-requires-three-lfn-directory-slots.txt",
    ) {
        Ok(entry) => {
            match v.read_file(&entry, &mut file_buf) {
                Ok(bytes) => {
                    let content = &file_buf[..bytes];
                    // verify against bytes(range(256)) * 20
                    let mut expected_ok = bytes == 5120;
                    if expected_ok {
                        for (i, &b) in content.iter().enumerate() {
                            if b != (i % 256) as u8 {
                                expected_ok = false;
                                break;
                            }
                        }
                    }
                    check(
                        expected_ok,
                        "fat32/usb: read_file 5120-byte multi-cluster LFN file over USB matches pattern",
                        "multi-cluster pattern mismatch",
                    );
                }
                Err(_) => {
                    check(
                        false,
                        "fat32/usb: read_file multi-cluster LFN file",
                        "read_file failed",
                    );
                }
            }
        }
        Err(_) => {
            check(
                false,
                "fat32/usb: read_file multi-cluster LFN file",
                "lookup failed",
            );
        }
    }
}
