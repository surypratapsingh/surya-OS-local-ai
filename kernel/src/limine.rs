//! Limine boot protocol interface for Nucleus (protocol base revision 3).
//!
//! Implements the request anchors and response parsing described in
//! `.freebuff/ref/limine/limine-12.9.0/limine-protocol/PROTOCOL.md` (Limine v12.9.0).
//! Only what K1 needs: framebuffer, memory map, stack size, executable address.

use core::cell::UnsafeCell;
use core::ptr;

/// Common magic shared by every Limine feature ID.
const COMMON_MAGIC: (u64, u64) = (0xc7b1dd30df4c8b88, 0x0a82e883a194f07b);

/// Requests-start marker (4 x u64), from PROTOCOL.md "Requests Delimiters".
#[used]
#[link_section = ".limine_requests_start"]
static REQUESTS_START_MARKER: [u64; 4] = [
    0xf6b8f4b39de7d1ae,
    0xfab91a6940fcb9cf,
    0x785c6ed015d3e316,
    0x181e920a7852b9d9,
];

/// Requests-end marker (2 x u64).
#[used]
#[link_section = ".limine_requests_end"]
static REQUESTS_END_MARKER: [u64; 2] = [0xadc0e0531bb10d03, 0x9572709f31764c62];

/// Base revision tag for protocol base revision 3. The bootloader overwrites
/// the third word with 0 when it booted us with a supported revision.
#[used]
#[link_section = ".limine_requests"]
static BASE_REVISION: [u64; 3] = [0xf9562b2d5c95a6c8, 0x6a7b384944536bdc, 3];

/// Compile-time guard that a request struct lies inside the markers.
macro_rules! linker_assert {
    ($e:expr, $msg:expr) => {
        const _: () = core::assert!($e, $msg);
    };
}

/// A Limine feature ID (2 common magic words + 2 feature words).
#[repr(C, align(8))]
struct RequestId([u64; 4]);

impl RequestId {
    const fn new(a: u64, b: u64) -> Self {
        RequestId([COMMON_MAGIC.0, COMMON_MAGIC.1, a, b])
    }
}

/// Common request header: id, revision, response pointer.
#[repr(C, align(8))]
pub struct RequestHeader {
    id: RequestId,
    revision: u64,
    pub response: AtomicPtr,
}

/// Response pointer cell. Limine fills it; we read it later.
#[repr(transparent)]
pub struct AtomicPtr {
    ptr: UnsafeCell<u64>,
}
unsafe impl Sync for AtomicPtr {}
impl AtomicPtr {
    pub const fn new() -> Self {
        AtomicPtr {
            ptr: UnsafeCell::new(0),
        }
    }
    fn store(&self, v: u64) {
        unsafe { (*self.ptr.get()) = v };
    }
    pub fn get(&self) -> u64 {
        unsafe { core::ptr::read_volatile(self.ptr.get()) }
    }
}

// ---- Feature IDs (from PROTOCOL.md) ----

impl RequestHeader {
    const fn new(a: u64, b: u64) -> Self {
        RequestHeader {
            id: RequestId::new(a, b),
            revision: 0,
            response: AtomicPtr::new(),
        }
    }
}

macro_rules! feature_request {
    ($name:ident, $doc:expr, $a:expr, $b:expr) => {
        #[doc = $doc]
        #[used]
        #[link_section = ".limine_requests"]
        pub static $name: RequestHeader = RequestHeader::new($a, $b);
    };
}

/// Stack-size request carries an extra `stack_size` payload word (4 MiB)
/// inside the request struct itself, per PROTOCOL.md.
#[repr(C, align(8))]
pub struct StackSizeRequest {
    id: RequestId,
    revision: u64,
    pub response: AtomicPtr,
    pub stack_size: u64,
}

#[used]
#[link_section = ".limine_requests"]
pub static STACK_SIZE_REQUEST: StackSizeRequest = StackSizeRequest {
    id: RequestId::new(0x224ef0460a8e8926, 0xe1cb0fc25f46ea3d),
    revision: 0,
    response: AtomicPtr::new(),
    stack_size: 4 << 20,
};

linker_assert!(
    (core::mem::size_of::<RequestHeader>() == 48),
    "RequestHeader must stay 48 bytes (4x u64 id + revision + response)"
);

feature_request!(
    FRAMEBUFFER_REQUEST,
    "Framebuffer feature (one or more linear framebuffers).",
    0x9d5827dcd881dd75,
    0xa3148604f6fab11b
);
feature_request!(
    MEMMAP_REQUEST,
    "Memory map feature (physical memory inventory).",
    0x67cf3d9d378a806f,
    0xe304acdfc50c3c62
);
feature_request!(
    EXEC_ADDR_REQUEST,
    "Executable address feature (where Limine placed us).",
    0x71ba76863cc55f63,
    0xb2644a48c516a487
);
feature_request!(
    CMDLINE_REQUEST,
    "Executable command line feature (kernel command line string).",
    0x4b161536e598651e,
    0xb390ad4a2f1f303a
);

// ---- Response structs (only the fields K1 reads) ----

/// limine_framebuffer (PROTOCOL.md "Framebuffer Feature").
#[repr(C)]
pub struct RawFramebuffer {
    pub address: u64,
    pub width: u64,
    pub height: u64,
    pub pitch: u64,
    pub bpp: u16,
    pub memory_model: u8,
    pub red_mask_size: u8,
    pub red_mask_shift: u8,
    pub green_mask_size: u8,
    pub green_mask_shift: u8,
    pub blue_mask_size: u8,
    pub blue_mask_shift: u8,
    pub _unused: [u8; 7],
    pub edid_size: u64,
    pub edid: u64,
    // response revision 1 fields (mode_count, modes) ignored
}

/// limine_memmap_entry.
#[repr(C)]
pub struct RawMemmapEntry {
    pub base: u64,
    pub length: u64,
    pub ty: u64,
}

pub const MEMMAP_USABLE: u64 = 0;
pub const MEMMAP_RESERVED: u64 = 1;
pub const MEMMAP_ACPI_RECLAIMABLE: u64 = 2;
pub const MEMMAP_ACPI_NVS: u64 = 3;
pub const MEMMAP_BAD_MEMORY: u64 = 4;
pub const MEMMAP_BOOTLOADER_RECLAIMABLE: u64 = 5;
pub const MEMMAP_EXECUTABLE_AND_MODULES: u64 = 6;
pub const MEMMAP_FRAMEBUFFER: u64 = 7;
pub const MEMMAP_RESERVED_MAPPED: u64 = 8;

/// Read a u64 via volatile from a (possibly bootloader-written) pointer.
fn rd(base: u64, offset: usize) -> u64 {
    unsafe { ptr::read_volatile((base as *const u64).add(offset)) }
}

/// Borrowed view of one framebuffer from the framebuffer response.
pub struct FramebufferInfo {
    pub address: u64,
    pub width: u64,
    pub height: u64,
    pub pitch: u64,
    pub bpp: u16,
    pub memory_model: u8,
    pub red_mask_size: u8,
    pub red_mask_shift: u8,
    pub green_mask_size: u8,
    pub green_mask_shift: u8,
    pub blue_mask_size: u8,
    pub blue_mask_shift: u8,
}

/// Fetch the first framebuffer, if Limine gave us one.
pub fn first_framebuffer() -> Option<FramebufferInfo> {
    let resp = FRAMEBUFFER_REQUEST.response.get();
    if resp == 0 {
        return None;
    }
    let count = rd(resp, 1); // revision, framebuffer_count, framebuffers
    if count == 0 {
        return None;
    }
    let fbs = rd(resp, 2);
    let fb_ptr = rd(fbs, 0);
    if fb_ptr == 0 {
        return None;
    }
    // limine_framebuffer is not all-u64: bpp(u16) and the eight mask bytes
    // pack into the 5th and 6th words. Decode per the C layout:
    //   word4 = bpp | memory_model | red_size | red_shift | green_size | green_shift | blue_size
    //   word5 = blue_shift | unused[7]
    let w4 = rd(fb_ptr, 4);
    let w5 = rd(fb_ptr, 5);
    Some(FramebufferInfo {
        address: rd(fb_ptr, 0),
        width: rd(fb_ptr, 1),
        height: rd(fb_ptr, 2),
        pitch: rd(fb_ptr, 3),
        bpp: (w4 & 0xFFFF) as u16,
        memory_model: ((w4 >> 16) & 0xFF) as u8,
        red_mask_size: ((w4 >> 24) & 0xFF) as u8,
        red_mask_shift: ((w4 >> 32) & 0xFF) as u8,
        green_mask_size: ((w4 >> 40) & 0xFF) as u8,
        green_mask_shift: ((w4 >> 48) & 0xFF) as u8,
        blue_mask_size: ((w4 >> 56) & 0xFF) as u8,
        blue_mask_shift: (w5 & 0xFF) as u8,
    })
}

/// A single memory map entry copied out of the response array.
pub struct MemmapEntry {
    pub base: u64,
    pub length: u64,
    pub ty: u64,
}

/// Iterate the memory map (up to `max` entries), invoking `f` per entry.
pub fn for_each_memmap_entry(mut f: impl FnMut(&MemmapEntry)) {
    let resp = MEMMAP_REQUEST.response.get();
    if resp == 0 {
        return;
    }
    let count = rd(resp, 1);
    let entries = rd(resp, 2);
    for i in 0..count {
        let e = rd(entries, i as usize);
        if e == 0 {
            continue;
        }
        f(&MemmapEntry {
            base: rd(e, 0),
            length: rd(e, 1),
            ty: rd(e, 2),
        });
    }
}

/// Where the bootloader placed our ELF (physical, virtual).
pub fn executable_address() -> Option<(u64, u64)> {
    let resp = EXEC_ADDR_REQUEST.response.get();
    if resp == 0 {
        return None;
    }
    Some((rd(resp, 1), rd(resp, 2)))
}

/// The kernel command line, if the bootloader supplied one.
pub fn cmdline() -> Option<&'static str> {
    let resp = CMDLINE_REQUEST.response.get();
    if resp == 0 {
        return None;
    }
    let p = rd(resp, 1);
    if p == 0 {
        return None;
    }
    unsafe {
        let base = p as *const u8;
        let mut len = 0usize;
        while len < 4096 && *base.add(len) != 0 {
            len += 1;
        }
        let slice = core::slice::from_raw_parts(base, len);
        core::str::from_utf8(slice).ok()
    }
}

/// Did the bootloader accept our base revision request?
pub fn base_revision_supported() -> bool {
    // The third word of BASE_REVISION is zeroed by the bootloader on success.
    unsafe {
        let p = &BASE_REVISION as *const u64;
        ptr::read_volatile(p.add(2)) == 0
    }
}

/// Human-readable memory map type name (for the boot report).
pub fn memmap_type_name(ty: u64) -> &'static str {
    match ty {
        MEMMAP_USABLE => "usable",
        MEMMAP_RESERVED => "reserved",
        MEMMAP_ACPI_RECLAIMABLE => "acpi-reclaimable",
        MEMMAP_ACPI_NVS => "acpi-nvs",
        MEMMAP_BAD_MEMORY => "bad-memory",
        MEMMAP_BOOTLOADER_RECLAIMABLE => "bootloader-reclaimable",
        MEMMAP_EXECUTABLE_AND_MODULES => "executable+modules",
        MEMMAP_FRAMEBUFFER => "framebuffer",
        MEMMAP_RESERVED_MAPPED => "reserved-mapped",
        _ => "unknown",
    }
}
