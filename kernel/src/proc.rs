//! K4 Process Table, Isolation, and Syscall Mediation.
//!
//! Enforces: "A process cannot touch a resource absent from its capability set
//! — proven by a test that attempts it and is denied."
//!
//! Process model:
//! - Fixed process table (MAX_PROCS = 8) with explicit PIDs.
//! - PID 1 is `novad` (the supervisor daemon), initialized with capabilities
//!   granted by its signed manifest.
//! - Dedicated per-process PML4 address spaces isolating user memory in Ring 3.
//! - Capability mediation on every kernel gate/syscall.

use crate::cap::{CapSet, Resource};
use crate::paging;
use crate::pmm;
use core::sync::atomic::{AtomicU64, Ordering};

pub static BOOT_TSC: AtomicU64 = AtomicU64::new(0);
pub static TSC_FREQ_HZ: AtomicU64 = AtomicU64::new(0);
pub static RTC_BASE_EPOCH: AtomicU64 = AtomicU64::new(0);
pub static LAST_MONO_NS: AtomicU64 = AtomicU64::new(0);

#[inline]
pub fn rdtsc() -> u64 {
    let low: u32;
    let high: u32;
    unsafe {
        core::arch::asm!(
            "rdtsc",
            out("eax") low,
            out("edx") high,
            options(nomem, nostack, preserves_flags)
        );
    }
    ((high as u64) << 32) | (low as u64)
}

pub const MAX_PROCS: usize = 8;
pub const USER_STACK_SIZE: usize = 16 * 1024; // 16 KiB user stack
pub const KERNEL_STACK_SIZE: usize = 16 * 1024; // 16 KiB per-process kernel stack

pub const PID_KERNEL: usize = 0;
pub const PID_NOVAD: usize = 1;

pub const MAX_FDS: usize = 16;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct OpenFile {
    pub first_cluster: u32,
    pub size: u32,
    pub seek_offset: u32,
    pub is_dir: bool,
}

pub const USER_CODE_VADDR: u64 = 0x0040_0000;
pub const USER_HEAP_BASE: u64 = 0x0050_0000;
pub const USER_HEAP_MAX: u64 = 0x0070_0000;
pub const USER_MMAP_BASE: u64 = 0x0070_0000;
pub const USER_MMAP_MAX: u64 = 0x007E_F000;
pub const USER_GUARD_PAGE: u64 = 0x007F_E000;
pub const USER_STACK_PAGE: u64 = 0x007F_F000;
pub const USER_STACK_TOP: u64 = 0x0080_0000;

// Linux Auxiliary Vector (auxv) Tags (gABI / Linux x86_64 ABI)
pub const AT_NULL: u64 = 0;
pub const AT_IGNORE: u64 = 1;
pub const AT_EXECFD: u64 = 2;
pub const AT_PHDR: u64 = 3;
pub const AT_PHENT: u64 = 4;
pub const AT_PHNUM: u64 = 5;
pub const AT_PAGESZ: u64 = 6;
pub const AT_BASE: u64 = 7;
pub const AT_FLAGS: u64 = 8;
pub const AT_ENTRY: u64 = 9;
pub const AT_NOTELF: u64 = 10;
pub const AT_UID: u64 = 11;
pub const AT_EUID: u64 = 12;
pub const AT_GID: u64 = 13;
pub const AT_EGID: u64 = 14;
pub const AT_CLKTCK: u64 = 17;
pub const AT_RANDOM: u64 = 25;
pub const AT_EXECFN: u64 = 31;

/// Auxiliary vector for Linux ELF process startup.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct LinuxAuxv {
    pub phdr: u64,
    pub phent: u64,
    pub phnum: u64,
    pub pagesz: u64,
    pub entry: u64,
}

impl Default for LinuxAuxv {
    fn default() -> Self {
        Self {
            phdr: 0,
            phent: 56,
            phnum: 0,
            pagesz: 4096,
            entry: USER_CODE_VADDR,
        }
    }
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum ProcessState {
    Unused,
    Ready,
    Running,
    Blocked,
    Exited,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum ProcessExitReason {
    None,
    Exited(u64),
    Fault {
        vector: u64,
        code: u64,
        rip: u64,
        cr2: u64,
        cs: u64,
    },
}

#[derive(Clone, Copy, Debug)]
#[repr(C)]
pub struct ProcessContext {
    pub rax: u64,
    pub rbx: u64,
    pub rcx: u64,
    pub rdx: u64,
    pub rsi: u64,
    pub rdi: u64,
    pub rbp: u64,
    pub r8: u64,
    pub r9: u64,
    pub r10: u64,
    pub r11: u64,
    pub r12: u64,
    pub r13: u64,
    pub r14: u64,
    pub r15: u64,
    pub rip: u64,
    pub rsp: u64,
    pub rflags: u64,
}

impl ProcessContext {
    pub const fn zero() -> Self {
        Self {
            rax: 0,
            rbx: 0,
            rcx: 0,
            rdx: 0,
            rsi: 0,
            rdi: 0,
            rbp: 0,
            r8: 0,
            r9: 0,
            r10: 0,
            r11: 0,
            r12: 0,
            r13: 0,
            r14: 0,
            r15: 0,
            rip: 0,
            rsp: 0,
            rflags: 0x202, // IF (interrupts enabled)
        }
    }
}

pub struct Process {
    pub pid: usize,
    pub name: [u8; 16],
    pub state: ProcessState,
    pub exit_reason: ProcessExitReason,
    pub cr3: u64,
    pub caps: CapSet,
    pub context: ProcessContext,
    pub kstack_top: u64,
    pub kstack_cur_rsp: u64,
    pub ustack_top: u64,
    pub ustack_frame: u64,
    pub brk: u64,
    pub mmap_cur: u64,
    pub fds: [Option<OpenFile>; MAX_FDS],
    pub last_syscall_cs: u64,
    pub last_fault_cs: u64,
    pub is_authenticated: bool,
    pub pkg_sha256: [u8; 32],
    pub pkg_sequence: u64,
    pub fs_base: u64,
    pub clear_child_tid: u64,
}

impl Process {
    pub const fn new(pid: usize) -> Self {
        Self {
            pid,
            name: [0; 16],
            state: ProcessState::Unused,
            exit_reason: ProcessExitReason::None,
            cr3: 0,
            caps: CapSet::new(),
            context: ProcessContext::zero(),
            kstack_top: 0,
            kstack_cur_rsp: 0,
            ustack_top: 0,
            ustack_frame: 0,
            brk: USER_HEAP_BASE,
            mmap_cur: USER_MMAP_BASE,
            fds: [None; MAX_FDS],
            last_syscall_cs: 0,
            last_fault_cs: 0,
            is_authenticated: false,
            pkg_sha256: [0; 32],
            pkg_sequence: 0,
            fs_base: 0,
            clear_child_tid: 0,
        }
    }

    pub const fn new_kernel() -> Self {
        let mut p = Self::new(PID_KERNEL);
        p.state = ProcessState::Running;
        p.name = *b"kernel\0\0\0\0\0\0\0\0\0\0";
        p
    }

    pub fn set_name(&mut self, s: &str) {
        let bytes = s.as_bytes();
        let len = if bytes.len() > 15 { 15 } else { bytes.len() };
        self.name[..len].copy_from_slice(&bytes[..len]);
        self.name[len] = 0;
    }

    pub fn name_str(&self) -> &str {
        let mut len = 0;
        while len < self.name.len() && self.name[len] != 0 {
            len += 1;
        }
        core::str::from_utf8(&self.name[..len]).unwrap_or("unknown")
    }
}

static mut PROCESS_TABLE: [Process; MAX_PROCS] = [
    Process::new_kernel(),
    Process::new(1),
    Process::new(2),
    Process::new(3),
    Process::new(4),
    Process::new(5),
    Process::new(6),
    Process::new(7),
];

static mut CURRENT_PID: usize = PID_KERNEL;

/// Current active PID.
pub fn current_pid() -> usize {
    unsafe { CURRENT_PID }
}

/// Set active PID.
pub fn set_current_pid(pid: usize) {
    unsafe {
        CURRENT_PID = pid;
    }
}

/// Spawn a verified user process from an authenticated package.
pub fn spawn_verified_user(
    pkg: &crate::trust::VerifiedPackage,
    payload: &[u8],
) -> Result<usize, &'static str> {
    // 1. Verify payload SHA-256 matches the package's authenticated SHA-256
    let actual_sha256 = crate::trust::sha256(payload);
    if actual_sha256 != pkg.sha256 {
        return Err("payload sha256 does not match verified package sha256");
    }
    // 2. Package sequence must be strictly positive (sequence 0 is uninitialized/unverified)
    if pkg.sequence == 0 {
        return Err("package sequence must be positive");
    }
    // 3. Package must have passed root verification: its sequence must match recorded trust sequence
    if pkg.sequence != crate::trust::current_sequence() {
        return Err("package sequence does not match recorded trust sequence");
    }

    let pid = spawn_user(pkg.id, pkg.caps, payload)?;
    unsafe {
        let proc = &mut PROCESS_TABLE[pid];
        proc.is_authenticated = true;
        proc.pkg_sha256 = pkg.sha256;
        proc.pkg_sequence = pkg.sequence;
    }
    Ok(pid)
}

pub fn current_process() -> &'static Process {
    unsafe { &PROCESS_TABLE[CURRENT_PID] }
}

pub fn current_process_mut() -> &'static mut Process {
    unsafe { &mut PROCESS_TABLE[CURRENT_PID] }
}

pub fn get_process(pid: usize) -> Option<&'static Process> {
    if pid < MAX_PROCS {
        unsafe {
            if PROCESS_TABLE[pid].state != ProcessState::Unused {
                return Some(&PROCESS_TABLE[pid]);
            }
        }
    }
    None
}

pub fn get_process_mut(pid: usize) -> Option<&'static mut Process> {
    if pid < MAX_PROCS {
        unsafe {
            if PROCESS_TABLE[pid].state != ProcessState::Unused {
                return Some(&mut PROCESS_TABLE[pid]);
            }
        }
    }
    None
}

const KSTACK_BASE_OFFSET: u64 = 4 << 30; // PDPTE index 4 under private_base (unused by heap/ELF/MMIO)
const KSTACK_PAGES: u64 = (KERNEL_STACK_SIZE as u64) / 4096;
const KSTACK_SLOT_STRIDE: u64 = (KSTACK_PAGES + 2) * 4096; // 1 guard page below + stack + 1 guard above

/// Allocate and map a guarded `KERNEL_STACK_SIZE` (16 KiB) kernel stack for `pid`.
fn alloc_process_kstack(pid: usize) -> Result<u64, &'static str> {
    if pid == PID_KERNEL || pid >= MAX_PROCS {
        return Err("invalid pid for kernel stack");
    }
    let slot_base = paging::private_base() + KSTACK_BASE_OFFSET + (pid as u64) * KSTACK_SLOT_STRIDE;
    let stack_base = slot_base + 4096; // slot_base..stack_base stays unmapped as a guard page
    for i in 0..KSTACK_PAGES {
        let frame = match pmm::alloc_frame() {
            Some(f) => f,
            None => {
                for j in 0..i {
                    let _ = paging::unmap_page(stack_base + j * 4096);
                }
                return Err("failed to alloc kernel stack");
            }
        };
        pmm::zero_frame(frame);
        paging::map_page(stack_base + i * 4096, frame);
    }
    Ok(stack_base + KERNEL_STACK_SIZE as u64)
}

/// Unmap and free all frames backing a process kernel stack.
fn free_process_kstack(kstack_top: u64) {
    if kstack_top == 0 {
        return;
    }
    let stack_base = kstack_top - KERNEL_STACK_SIZE as u64;
    for i in 0..KSTACK_PAGES {
        let _ = paging::unmap_page(stack_base + i * 4096);
    }
}

/// Free resources owned by an exited process and reset its slot to `Process::new(pid)`.
pub unsafe fn cleanup_process(pid: usize) {
    if pid == PID_KERNEL || pid >= MAX_PROCS {
        return;
    }
    let proc = &mut PROCESS_TABLE[pid];
    if proc.cr3 != 0 && proc.cr3 != paging::kernel_pml4() {
        paging::destroy_user_space(proc.cr3);
        proc.cr3 = 0;
    }
    if proc.kstack_top != 0 {
        free_process_kstack(proc.kstack_top);
        proc.kstack_top = 0;
    }
    proc.ustack_frame = 0;
    proc.fds = [None; MAX_FDS];
    *proc = Process::new(pid);
}

/// Explicitly reap an exited process, freeing its resources.
pub fn reap_process(pid: usize) -> Result<(), &'static str> {
    if pid == PID_KERNEL || pid >= MAX_PROCS {
        return Err("invalid pid");
    }
    unsafe {
        if PROCESS_TABLE[pid].state != ProcessState::Exited {
            return Err("process is not exited");
        }
        cleanup_process(pid);
        Ok(())
    }
}

/// Find a free process slot. First searches for an `Unused` slot.
/// If all slots are in use, searches for and recycles an `Exited` slot.
unsafe fn alloc_process_slot() -> Result<usize, &'static str> {
    let table_ptr = core::ptr::addr_of!(PROCESS_TABLE) as *const Process;
    // Pass 1: search for an Unused slot
    for i in 1..MAX_PROCS {
        if (*table_ptr.add(i)).state == ProcessState::Unused {
            return Ok(i);
        }
    }
    // Pass 2: reuse an Exited slot
    for i in 1..MAX_PROCS {
        if (*table_ptr.add(i)).state == ProcessState::Exited {
            cleanup_process(i);
            return Ok(i);
        }
    }
    Err("no free process slots")
}

/// Create a dedicated PML4 address space for a process.
/// Inherits the higher-half kernel mapping and direct map so kernel routines survive CR3 switch.
/// The lower canonical half (PML4 indices 0..255) is zeroed so user mode has zero kernel access.
pub fn create_process_pml4() -> Option<u64> {
    let pml4_frame = pmm::alloc_frame()?;
    pmm::zero_frame(pml4_frame);
    let hhdm = crate::limine::hhdm_offset().expect("proc: HHDM required");
    let kernel_pml4 = paging::kernel_pml4();
    unsafe {
        for i in 256..512u64 {
            let entry = pmm::read_va(hhdm + kernel_pml4 + i * 8);
            pmm::write_va(hhdm + pml4_frame + i * 8, entry);
        }
    }
    Some(pml4_frame)
}

/// Spawn a user process with a given capability set and user code slice.
pub fn spawn_user(name: &str, caps: CapSet, code: &[u8]) -> Result<usize, &'static str> {
    if code.len() > 4096 {
        return Err("user code exceeds 4096 bytes");
    }
    unsafe {
        let pid = alloc_process_slot()?;
        let pml4 = create_process_pml4().ok_or("failed to allocate process PML4")?;

        // Allocate frame for user code and copy instructions
        let code_frame = pmm::alloc_frame().ok_or("failed to alloc code frame")?;
        pmm::zero_frame(code_frame);
        let hhdm = crate::limine::hhdm_offset().ok_or("HHDM required")?;
        core::ptr::copy_nonoverlapping(code.as_ptr(), (hhdm + code_frame) as *mut u8, code.len());
        paging::map_user_page(pml4, USER_CODE_VADDR, code_frame, false)?;

        // Allocate frame for user stack (4 KiB page at USER_STACK_PAGE = 0x007F_F000)
        let ustack_frame = pmm::alloc_frame().ok_or("failed to alloc user stack")?;
        pmm::zero_frame(ustack_frame);
        paging::map_user_page(pml4, USER_STACK_PAGE, ustack_frame, true)?;

        // Allocate guarded kernel stack for TSS.rsp0 transitions
        let kstack_top = alloc_process_kstack(pid)?;

        let proc = &mut PROCESS_TABLE[pid];
        proc.pid = pid;
        proc.set_name(name);
        proc.state = ProcessState::Ready;
        proc.exit_reason = ProcessExitReason::None;
        proc.cr3 = pml4;
        proc.caps = caps;
        proc.kstack_top = kstack_top;
        proc.ustack_top = USER_STACK_TOP;
        proc.ustack_frame = ustack_frame;
        proc.brk = USER_HEAP_BASE;
        proc.mmap_cur = USER_MMAP_BASE;
        proc.fs_base = 0;
        proc.clear_child_tid = 0;

        proc.context = ProcessContext::zero();
        proc.context.rip = USER_CODE_VADDR;
        proc.context.rsp = USER_STACK_TOP;
        proc.context.rflags = 0x202;

        // Initialize kernel stack with initial IRETQ frame (160 bytes):
        // [kstack_top - 40..0]: [rip, cs, rflags, rsp, ss]
        // [kstack_top - 160..-40]: 15 zeroed GPRs (rax..r15)
        let frame_ptr = (kstack_top - 160) as *mut u64;
        for i in 0..15 {
            core::ptr::write(frame_ptr.add(i), 0);
        }
        core::ptr::write(frame_ptr.add(15), USER_CODE_VADDR); // rip
        core::ptr::write(frame_ptr.add(16), 0x3B); // cs (USER_CODE64)
        core::ptr::write(frame_ptr.add(17), 0x202); // rflags (IF=1)
        core::ptr::write(frame_ptr.add(18), USER_STACK_TOP); // rsp
        core::ptr::write(frame_ptr.add(19), 0x33); // ss (USER_DATA64)
        proc.kstack_cur_rsp = kstack_top - 160;

        Ok(pid)
    }
}

/// Setup Linux ABI initial stack frame (argc, argv, envp, auxv, and random bytes).
/// The stack page is mapped at USER_STACK_PAGE (0x007F_F000) to USER_STACK_TOP (0x0080_0000).
/// Guarantees that the returned user_rsp is 16-byte aligned (rsp % 16 == 0), pointing to argc.
pub fn setup_linux_stack(
    ustack_frame: u64,
    argv: &[&str],
    envp: &[&str],
    auxv: &LinuxAuxv,
) -> Result<u64, &'static str> {
    let hhdm = crate::limine::hhdm_offset().ok_or("HHDM required")?;
    let k_buf = (hhdm + ustack_frame) as *mut u8;

    // 1. Place 16 random bytes at the top of the stack page (offset 4080..4096)
    // User virtual address: USER_STACK_TOP - 16 = 0x007F_FFF0.
    let at_random_vaddr = USER_STACK_TOP - 16;
    let random_bytes: [u8; 16] = [
        0xd4, 0x1d, 0x8c, 0xd9, 0x8f, 0x00, 0xb2, 0x04, 0xe9, 0x80, 0x09, 0x98, 0xec, 0xf8, 0x42,
        0x7e,
    ];
    unsafe {
        core::ptr::copy_nonoverlapping(random_bytes.as_ptr(), k_buf.add(4096 - 16), 16);
    }

    let mut cur_offset = 4096 - 16;
    let mut env_vaddrs = [0u64; 16];
    if envp.len() > env_vaddrs.len() {
        return Err("too many env variables");
    }
    for (i, env_str) in envp.iter().enumerate() {
        let bytes = env_str.as_bytes();
        let s_len = bytes.len() + 1; // null-terminated
        if cur_offset < s_len {
            return Err("stack overflow writing envp strings");
        }
        cur_offset -= s_len;
        unsafe {
            core::ptr::copy_nonoverlapping(bytes.as_ptr(), k_buf.add(cur_offset), bytes.len());
            *k_buf.add(cur_offset + bytes.len()) = 0;
        }
        env_vaddrs[i] = USER_STACK_PAGE + cur_offset as u64;
    }

    let mut argv_vaddrs = [0u64; 16];
    if argv.len() > argv_vaddrs.len() {
        return Err("too many argv arguments");
    }
    for (i, arg_str) in argv.iter().enumerate() {
        let bytes = arg_str.as_bytes();
        let s_len = bytes.len() + 1; // null-terminated
        if cur_offset < s_len {
            return Err("stack overflow writing argv strings");
        }
        cur_offset -= s_len;
        unsafe {
            core::ptr::copy_nonoverlapping(bytes.as_ptr(), k_buf.add(cur_offset), bytes.len());
            *k_buf.add(cur_offset + bytes.len()) = 0;
        }
        argv_vaddrs[i] = USER_STACK_PAGE + cur_offset as u64;
    }

    // Align down to 8-byte boundary
    cur_offset &= !7;

    // Total words = argc (1) + argv pointers (argv.len()) + NULL (1) +
    //               envp pointers (envp.len()) + NULL (1) +
    //               15 auxv pairs (30 words)
    let total_words = 1 + argv.len() + 1 + envp.len() + 1 + 30;
    let total_bytes = total_words * 8;
    if cur_offset < total_bytes {
        return Err("stack overflow writing pointer table");
    }

    // Align table start so user_rsp is 16-byte aligned (rsp % 16 == 0)
    let table_start_offset = (cur_offset - total_bytes) & !15;
    if table_start_offset < 128 {
        return Err("insufficient stack space remaining");
    }

    let w_ptr = unsafe { k_buf.add(table_start_offset) as *mut u64 };
    let mut idx = 0;

    unsafe {
        // Write argc
        core::ptr::write(w_ptr.add(idx), argv.len() as u64);
        idx += 1;

        // Write argv pointers
        for &va in argv_vaddrs.iter().take(argv.len()) {
            core::ptr::write(w_ptr.add(idx), va);
            idx += 1;
        }
        // NULL terminating argv
        core::ptr::write(w_ptr.add(idx), 0);
        idx += 1;

        // Write envp pointers
        for &va in env_vaddrs.iter().take(envp.len()) {
            core::ptr::write(w_ptr.add(idx), va);
            idx += 1;
        }
        // NULL terminating envp
        core::ptr::write(w_ptr.add(idx), 0);
        idx += 1;

        // Write Auxv vector
        let execfn_vaddr = if !argv.is_empty() { argv_vaddrs[0] } else { 0 };
        let auxv_entries: [(u64, u64); 15] = [
            (AT_PHDR, auxv.phdr),
            (AT_PHENT, auxv.phent),
            (AT_PHNUM, auxv.phnum),
            (AT_PAGESZ, auxv.pagesz),
            (AT_BASE, 0),
            (AT_FLAGS, 0),
            (AT_ENTRY, auxv.entry),
            (AT_UID, 1000),
            (AT_EUID, 1000),
            (AT_GID, 1000),
            (AT_EGID, 1000),
            (AT_CLKTCK, 100),
            (AT_RANDOM, at_random_vaddr),
            (AT_EXECFN, execfn_vaddr),
            (AT_NULL, 0),
        ];

        for (tag, val) in auxv_entries {
            core::ptr::write(w_ptr.add(idx), tag);
            idx += 1;
            core::ptr::write(w_ptr.add(idx), val);
            idx += 1;
        }
    }

    assert_eq!(idx, total_words, "table word count mismatch");

    let user_rsp = USER_STACK_PAGE + table_start_offset as u64;
    assert_eq!(user_rsp % 16, 0, "user_rsp must be 16-byte aligned");

    Ok(user_rsp)
}

/// Spawn a user process with Linux ABI stack layout (argc, argv, envp, auxv).
pub fn spawn_user_linux(
    name: &str,
    caps: CapSet,
    code: &[u8],
    argv: &[&str],
    envp: &[&str],
    auxv: &LinuxAuxv,
) -> Result<usize, &'static str> {
    if code.len() > 4096 {
        return Err("user code exceeds 4096 bytes");
    }
    unsafe {
        let pid = alloc_process_slot()?;
        let pml4 = create_process_pml4().ok_or("failed to allocate process PML4")?;

        let code_frame = pmm::alloc_frame().ok_or("failed to alloc code frame")?;
        pmm::zero_frame(code_frame);
        let hhdm = crate::limine::hhdm_offset().ok_or("HHDM required")?;
        core::ptr::copy_nonoverlapping(code.as_ptr(), (hhdm + code_frame) as *mut u8, code.len());
        paging::map_user_page(pml4, USER_CODE_VADDR, code_frame, false)?;

        let ustack_frame = pmm::alloc_frame().ok_or("failed to alloc user stack")?;
        pmm::zero_frame(ustack_frame);
        paging::map_user_page(pml4, USER_STACK_PAGE, ustack_frame, true)?;

        let user_rsp = setup_linux_stack(ustack_frame, argv, envp, auxv)?;

        let kstack_top = alloc_process_kstack(pid)?;

        let proc = &mut PROCESS_TABLE[pid];
        proc.pid = pid;
        proc.set_name(name);
        proc.state = ProcessState::Ready;
        proc.exit_reason = ProcessExitReason::None;
        proc.cr3 = pml4;
        proc.caps = caps;
        proc.kstack_top = kstack_top;
        proc.ustack_top = USER_STACK_TOP;
        proc.ustack_frame = ustack_frame;
        proc.brk = USER_HEAP_BASE;
        proc.mmap_cur = USER_MMAP_BASE;
        proc.fs_base = 0;
        proc.clear_child_tid = 0;

        proc.context = ProcessContext::zero();
        proc.context.rip = USER_CODE_VADDR;
        proc.context.rsp = user_rsp;
        proc.context.rflags = 0x202;

        let frame_ptr = (kstack_top - 160) as *mut u64;
        for i in 0..15 {
            core::ptr::write(frame_ptr.add(i), 0);
        }
        core::ptr::write(frame_ptr.add(15), USER_CODE_VADDR);
        core::ptr::write(frame_ptr.add(16), 0x3B);
        core::ptr::write(frame_ptr.add(17), 0x202);
        core::ptr::write(frame_ptr.add(18), user_rsp);
        core::ptr::write(frame_ptr.add(19), 0x33);
        proc.kstack_cur_rsp = kstack_top - 160;

        Ok(pid)
    }
}

/// Spawn a process with a given capability set (legacy helper).
pub fn spawn(name: &str, caps: CapSet, entry_point: u64) -> Result<usize, &'static str> {
    unsafe {
        let pid = alloc_process_slot()?;
        let proc = &mut PROCESS_TABLE[pid];

        let pml4 = create_process_pml4().ok_or("failed to allocate process PML4")?;
        proc.pid = pid;
        proc.set_name(name);
        proc.state = ProcessState::Ready;
        proc.exit_reason = ProcessExitReason::None;
        proc.cr3 = pml4;
        proc.caps = caps;

        proc.kstack_top = alloc_process_kstack(pid)?;

        let ustack_frame = pmm::alloc_frame().ok_or("failed to alloc user stack")?;
        proc.ustack_top = crate::limine::hhdm_offset().unwrap() + ustack_frame + 4096;

        proc.context = ProcessContext::zero();
        proc.context.rip = entry_point;
        proc.context.rsp = proc.ustack_top;

        Ok(pid)
    }
}

// ---------------------------------------------------------------------------
// Execution Transition & Run Loop
// ---------------------------------------------------------------------------

pub const MSR_FS_BASE: u32 = 0xC000_0100;

#[inline]
pub unsafe fn rdmsr(msr: u32) -> u64 {
    let low: u32;
    let high: u32;
    core::arch::asm!(
        "rdmsr",
        in("ecx") msr,
        out("eax") low,
        out("edx") high,
        options(nomem, nostack, preserves_flags)
    );
    ((high as u64) << 32) | (low as u64)
}

#[inline]
pub unsafe fn wrmsr(msr: u32, val: u64) {
    let low = val as u32;
    let high = (val >> 32) as u32;
    core::arch::asm!(
        "wrmsr",
        in("ecx") msr,
        in("eax") low,
        in("edx") high,
        options(nomem, nostack, preserves_flags)
    );
}

static mut KERNEL_SAVED_RSP: u64 = 0;
static mut KERNEL_PML4_PHYS: u64 = 0;

extern "C" {
    fn enter_user_mode(rip: u64, rsp: u64, rflags: u64) -> u64;
    pub fn return_to_kernel(code: u64) -> !;
}

core::arch::global_asm!(
    ".section .text",
    ".globl enter_user_mode",
    "enter_user_mode:",
    "    push rbp",
    "    push rbx",
    "    push r12",
    "    push r13",
    "    push r14",
    "    push r15",
    "    mov [rip + {k_saved_rsp}], rsp",
    "    mov ax, 0x33",
    "    mov ds, ax",
    "    mov es, ax",
    "    mov gs, ax",
    "    push 0x33",
    "    push rsi",
    "    push rdx",
    "    push 0x3B",
    "    push rdi",
    "    xor rax, rax",
    "    xor rbx, rbx",
    "    xor rcx, rcx",
    "    xor rdx, rdx",
    "    xor rsi, rsi",
    "    xor rdi, rdi",
    "    xor rbp, rbp",
    "    xor r8, r8",
    "    xor r9, r9",
    "    xor r10, r10",
    "    xor r11, r11",
    "    xor r12, r12",
    "    xor r13, r13",
    "    xor r14, r14",
    "    xor r15, r15",
    "    iretq",
    "",
    ".globl return_to_kernel",
    "return_to_kernel:",
    "    mov ax, 0x10",
    "    mov ds, ax",
    "    mov es, ax",
    "    mov gs, ax",
    "    mov ss, ax",
    "    mov rax, [rip + {k_pml4}]",
    "    mov cr3, rax",
    "    mov rsp, [rip + {k_saved_rsp}]",
    "    pop r15",
    "    pop r14",
    "    pop r13",
    "    pop r12",
    "    pop rbx",
    "    pop rbp",
    "    mov rax, rdi",
    "    ret",
    k_saved_rsp = sym KERNEL_SAVED_RSP,
    k_pml4 = sym KERNEL_PML4_PHYS
);

/// Run a user process in Ring 3 until it exits or faults.
pub fn run_user_process(pid: usize) -> ProcessExitReason {
    unsafe {
        assert!(pid < MAX_PROCS, "invalid pid");
        KERNEL_PML4_PHYS = paging::kernel_pml4();

        // Switch to target process (sets TSS.rsp0, CR3, and restores MSR_FS_BASE)
        switch_to(pid);

        let proc = &mut PROCESS_TABLE[pid];

        // Enter user mode (returns when return_to_kernel is invoked)
        let _ = enter_user_mode(proc.context.rip, proc.context.rsp, proc.context.rflags);

        // Switch back to kernel context (restores kernel PML4 and resets FS base to 0)
        switch_to(PID_KERNEL);

        PROCESS_TABLE[pid].exit_reason
    }
}

/// Handle a fault originating from Ring 3 (user mode).
pub unsafe fn handle_user_fault(vec: u64, code: u64, rip: u64, cr2: u64, cs: u64) -> ! {
    let pid = CURRENT_PID;
    if pid < MAX_PROCS {
        let proc = &mut PROCESS_TABLE[pid];
        proc.state = ProcessState::Exited;
        proc.last_fault_cs = cs;
        proc.exit_reason = ProcessExitReason::Fault {
            vector: vec,
            code,
            rip,
            cr2,
            cs,
        };
        sprintln!(
            "proc: PID {} ({}) killed by fault: vector={} code={:#x} rip={:#x} cr2={:#x} cs={:#x}",
            pid,
            proc.name_str(),
            vec,
            code,
            rip,
            cr2,
            cs
        );
    } else {
        sprintln!(
            "proc: unknown user process killed by fault: vector={} code={:#x} rip={:#x} cr2={:#x} cs={:#x}",
            vec,
            code,
            rip,
            cr2,
            cs
        );
    }
    return_to_kernel(1);
}

// ---------------------------------------------------------------------------
// Syscall Dispatch & Capability Enforcement
// ---------------------------------------------------------------------------

pub const SYS_LINUX_SCHED_YIELD: u64 = 24;
pub const SYS_EXIT: u64 = 2;
pub const SYS_REQUEST_RESOURCE: u64 = 3;
pub const SYS_OPEN: u64 = 4;
pub const SYS_READ: u64 = 5;
pub const SYS_CLOSE: u64 = 6;
pub const SYS_BRK: u64 = 12;
pub const SYS_MMAP: u64 = 9;
pub const SYS_MUNMAP: u64 = 11;

// Linux x86_64 ABI syscall numbers
pub const SYS_LINUX_READ: u64 = 0;
pub const SYS_LINUX_WRITE: u64 = 1;
pub const SYS_LINUX_CLOSE: u64 = 3;
pub const SYS_LINUX_FSTAT: u64 = 5;
pub const SYS_LINUX_MMAP: u64 = 9;
pub const SYS_LINUX_MPROTECT: u64 = 10;
pub const SYS_LINUX_MUNMAP: u64 = 11;
pub const SYS_LINUX_BRK: u64 = 12;
pub const SYS_LINUX_IOCTL: u64 = 16;
pub const SYS_LINUX_EXIT: u64 = 60;
pub const SYS_LINUX_ARCH_PRCTL: u64 = 158;
pub const SYS_LINUX_SET_TID_ADDRESS: u64 = 218;
pub const SYS_LINUX_CLOCK_GETTIME: u64 = 228;
pub const SYS_LINUX_EXIT_GROUP: u64 = 231;
pub const SYS_LINUX_NEWFSTATAT: u64 = 262;
pub const SYS_LINUX_PRLIMIT64: u64 = 302;
pub const SYS_LINUX_GETRANDOM: u64 = 318;

pub const ARCH_SET_GS: u64 = 0x1001;
pub const ARCH_SET_FS: u64 = 0x1002;
pub const ARCH_GET_FS: u64 = 0x1003;
pub const ARCH_GET_GS: u64 = 0x1004;

pub const PROT_NONE: u64 = 0;
pub const PROT_READ: u64 = 1;
pub const PROT_WRITE: u64 = 2;
pub const PROT_EXEC: u64 = 4;

pub const AT_FDCWD: i64 = -100;
pub const AT_EMPTY_PATH: u64 = 0x1000;

pub const S_IFREG: u32 = 0o100000;
pub const S_IFDIR: u32 = 0o040000;
pub const S_IFCHR: u32 = 0o020000;

pub const ERR_PERM: i64 = -1; // Operation not permitted (denied by capability gate)
pub const ERR_ENOENT: i64 = -2; // No such file or directory (-ENOENT)
pub const ERR_INVAL: i64 = -2; // Invalid resource or argument
pub const ERR_BADF: i64 = -9; // Bad file descriptor (-EBADF)
pub const ERR_FAULT: i64 = -14; // Bad address (-EFAULT)
pub const ERR_LINUX_EINVAL: i64 = -22; // Invalid argument (-EINVAL)
pub const ERR_NOTTY: i64 = -25; // Not a typewriter (-ENOTTY)
pub const ERR_NOSYS: i64 = -38; // Function not implemented (-ENOSYS)

#[repr(C)]
#[derive(Copy, Clone, Default, Debug, PartialEq, Eq)]
pub struct LinuxStat {
    pub st_dev: u64,
    pub st_ino: u64,
    pub st_nlink: u64,
    pub st_mode: u32,
    pub st_uid: u32,
    pub st_gid: u32,
    pub __pad0: u32,
    pub st_rdev: u64,
    pub st_size: i64,
    pub st_blksize: i64,
    pub st_blocks: i64,
    pub st_atime: i64,
    pub st_atime_nsec: i64,
    pub st_mtime: i64,
    pub st_mtime_nsec: i64,
    pub st_ctime: i64,
    pub st_ctime_nsec: i64,
    pub __unused: [i64; 3],
}

/// Copy memory from user space virtual address into kernel buffer.
/// Translates virtual addresses to physical pages and verifies range < 0x0000_8000_0000_0000.
pub fn copy_from_user(user_src: u64, dest: &mut [u8]) -> Result<(), i64> {
    if user_src >= 0x0000_8000_0000_0000 {
        return Err(ERR_INVAL);
    }
    let end = user_src.checked_add(dest.len() as u64).ok_or(ERR_INVAL)?;
    if end > 0x0000_8000_0000_0000 {
        return Err(ERR_INVAL);
    }
    let hhdm = crate::limine::hhdm_offset().ok_or(ERR_INVAL)?;
    for (i, b) in dest.iter_mut().enumerate() {
        let va = user_src + (i as u64);
        let pa = paging::translate(va).ok_or(ERR_INVAL)?;
        let byte = unsafe { core::ptr::read_volatile((hhdm + pa) as *const u8) };
        *b = byte;
    }
    Ok(())
}

/// Copy memory from kernel buffer into user space virtual address.
/// Translates virtual addresses to physical pages and verifies range < 0x0000_8000_0000_0000.
pub fn copy_to_user(user_dest: u64, src: &[u8]) -> Result<(), i64> {
    if user_dest >= 0x0000_8000_0000_0000 {
        return Err(ERR_INVAL);
    }
    let end = user_dest.checked_add(src.len() as u64).ok_or(ERR_INVAL)?;
    if end > 0x0000_8000_0000_0000 {
        return Err(ERR_INVAL);
    }
    let hhdm = crate::limine::hhdm_offset().ok_or(ERR_INVAL)?;
    for (i, &b) in src.iter().enumerate() {
        let va = user_dest + (i as u64);
        let pa = paging::translate(va).ok_or(ERR_INVAL)?;
        unsafe {
            core::ptr::write_volatile((hhdm + pa) as *mut u8, b);
        }
    }
    Ok(())
}

/// Open a file read-only from Ring 3 through the FsRead capability gate.
pub fn sys_open(path_ptr: u64, path_len: u64, flags: u64) -> i64 {
    let cur = current_process();
    if !cur.caps.grants(Resource::FsRead) {
        sprintln!(
            "cap gate: PID {} ({}) DENIED resource {:?}",
            cur.pid,
            cur.name_str(),
            Resource::FsRead
        );
        return ERR_PERM;
    }
    sprintln!(
        "cap gate: PID {} ({}) GRANTED resource {:?}",
        cur.pid,
        cur.name_str(),
        Resource::FsRead
    );

    if path_len == 0 || path_len > 256 || flags != 0 {
        return ERR_INVAL;
    }

    let mut path_buf = [0u8; 256];
    let path_bytes = &mut path_buf[..path_len as usize];
    if copy_from_user(path_ptr, path_bytes).is_err() {
        return ERR_INVAL;
    }

    let path_str = match core::str::from_utf8(path_bytes) {
        Ok(s) => s,
        Err(_) => return ERR_INVAL,
    };

    let vol = match crate::fat::get_active_fat_volume() {
        Some(v) => v,
        None => return ERR_INVAL,
    };

    let entry = match vol.lookup_path(path_str) {
        Ok(e) => e,
        Err(_) => return ERR_INVAL,
    };

    let cur = current_process_mut();
    let mut fd_opt = None;
    for i in 3..MAX_FDS {
        if cur.fds[i].is_none() {
            fd_opt = Some(i);
            break;
        }
    }
    let fd = match fd_opt {
        Some(idx) => idx,
        None => return ERR_INVAL,
    };

    cur.fds[fd] = Some(OpenFile {
        first_cluster: entry.first_cluster,
        size: entry.size,
        seek_offset: 0,
        is_dir: entry.is_dir(),
    });

    fd as i64
}

/// Read from an open file descriptor into user memory.
pub fn sys_read(fd: u64, buf_ptr: u64, count: u64) -> i64 {
    let cur = current_process();
    if !cur.caps.grants(Resource::FsRead) {
        return ERR_PERM;
    }

    let fd_idx = fd as usize;
    if fd_idx >= MAX_FDS {
        return ERR_INVAL;
    }

    let file = match cur.fds[fd_idx] {
        Some(f) => f,
        None => return ERR_INVAL,
    };

    if file.is_dir {
        return ERR_INVAL;
    }

    if count == 0 {
        return 0;
    }

    let vol = match crate::fat::get_active_fat_volume() {
        Some(v) => v,
        None => return ERR_INVAL,
    };

    let to_read = core::cmp::min(count as usize, 512);
    let mut kbuf = [0u8; 512];
    let chunk = &mut kbuf[..to_read];
    let bytes_read = match vol.read_file_offset(
        file.first_cluster,
        file.size,
        file.is_dir,
        file.seek_offset as usize,
        chunk,
    ) {
        Ok(n) => n,
        Err(_) => return ERR_INVAL,
    };

    if bytes_read > 0 && copy_to_user(buf_ptr, &chunk[..bytes_read]).is_err() {
        return ERR_INVAL;
    }

    let cur = current_process_mut();
    if let Some(f) = &mut cur.fds[fd_idx] {
        f.seek_offset = f.seek_offset.saturating_add(bytes_read as u32);
    }

    bytes_read as i64
}

/// Write to a file descriptor.
/// K6-3 (A2.24): File descriptors 1 (stdout) and 2 (stderr) go to the console and serial port.
/// Any other descriptor returns -EBADF (-9).
pub fn sys_write(fd: u64, buf_ptr: u64, count: u64) -> i64 {
    if fd != 1 && fd != 2 {
        return ERR_BADF;
    }
    if count == 0 {
        return 0;
    }
    let mut kbuf = [0u8; 512];
    let mut remaining = count as usize;
    let mut current_ptr = buf_ptr;
    let mut total_written: usize = 0;

    while remaining > 0 {
        let chunk_sz = remaining.min(512);
        if copy_from_user(current_ptr, &mut kbuf[..chunk_sz]).is_err() {
            if total_written > 0 {
                return total_written as i64;
            }
            return ERR_FAULT;
        }
        crate::console::write_bytes(&kbuf[..chunk_sz]);
        total_written += chunk_sz;
        current_ptr = match current_ptr.checked_add(chunk_sz as u64) {
            Some(p) => p,
            None => break,
        };
        remaining -= chunk_sz;
    }
    total_written as i64
}

/// Close an open file descriptor.
pub fn sys_close(fd: u64) -> i64 {
    let fd_idx = fd as usize;
    if fd_idx >= MAX_FDS {
        return ERR_INVAL;
    }
    let cur = current_process_mut();
    if cur.fds[fd_idx].is_none() {
        return ERR_INVAL;
    }
    cur.fds[fd_idx] = None;
    0
}

/// Adjust process data segment break (heap allocation).
/// Passing new_brk == 0 queries current brk.
pub fn sys_brk(new_brk: u64) -> i64 {
    let cur = current_process_mut();
    if new_brk == 0 || new_brk < USER_HEAP_BASE {
        return cur.brk as i64;
    }
    if new_brk > USER_HEAP_MAX {
        return cur.brk as i64;
    }

    let old_page_end = (cur.brk.saturating_add(4095)) & !4095;
    let new_page_end = (new_brk.saturating_add(4095)) & !4095;

    if new_page_end > old_page_end {
        let mut vpage = old_page_end;
        while vpage < new_page_end {
            let frame = match pmm::alloc_frame() {
                Some(f) => f,
                None => return cur.brk as i64,
            };
            pmm::zero_frame(frame);
            if paging::map_user_page(cur.cr3, vpage, frame, true).is_err() {
                pmm::free_frame(frame);
                return cur.brk as i64;
            }
            vpage += 4096;
        }
    } else if new_page_end < old_page_end {
        let mut vpage = new_page_end;
        while vpage < old_page_end {
            paging::unmap_user_page(cur.cr3, vpage);
            vpage += 4096;
        }
    }

    cur.brk = new_brk;
    new_brk as i64
}

/// Allocate anonymous memory pages in user address space.
pub fn sys_mmap(addr: u64, len: u64, prot: u64) -> i64 {
    if len == 0 {
        return ERR_INVAL;
    }
    let cur = current_process_mut();
    let aligned_len = match len.checked_add(4095) {
        Some(l) => l & !4095,
        None => return ERR_INVAL,
    };

    let alloc_addr = if addr == 0 {
        let a = cur.mmap_cur;
        let next = match a.checked_add(aligned_len) {
            Some(n) => n,
            None => return -12, // ENOMEM
        };
        if next > USER_MMAP_MAX {
            return -12; // ENOMEM
        }
        cur.mmap_cur = next;
        a
    } else {
        if !addr.is_multiple_of(4096) || addr < USER_MMAP_BASE {
            return ERR_INVAL;
        }
        let end = match addr.checked_add(aligned_len) {
            Some(e) => e,
            None => return ERR_INVAL,
        };
        if end > USER_MMAP_MAX {
            return -12; // ENOMEM
        }
        if end > cur.mmap_cur {
            cur.mmap_cur = end;
        }
        addr
    };

    // Decode protection bits (N4):
    // PROT_WRITE (bit 1) controls page writability.
    // Mappings without PROT_WRITE (e.g. PROT_READ = 1 or PROT_NONE = 0) are mapped read-only.
    let writable = (prot & PROT_WRITE) != 0;
    let mut vpage = alloc_addr;
    let end = alloc_addr + aligned_len;
    while vpage < end {
        let frame = match pmm::alloc_frame() {
            Some(f) => f,
            None => return -12,
        };
        pmm::zero_frame(frame);
        if paging::map_user_page(cur.cr3, vpage, frame, writable).is_err() {
            pmm::free_frame(frame);
            return -12;
        }
        vpage += 4096;
    }

    alloc_addr as i64
}

/// Unmap anonymous memory pages in user address space.
pub fn sys_munmap(addr: u64, len: u64) -> i64 {
    if len == 0 || !addr.is_multiple_of(4096) || !(USER_HEAP_BASE..USER_GUARD_PAGE).contains(&addr)
    {
        return ERR_INVAL;
    }
    let aligned_len = match len.checked_add(4095) {
        Some(l) => l & !4095,
        None => return ERR_INVAL,
    };
    let cur = current_process();
    let mut vpage = addr;
    let end = addr.saturating_add(aligned_len);
    while vpage < end && vpage < USER_GUARD_PAGE {
        paging::unmap_user_page(cur.cr3, vpage);
        vpage += 4096;
    }
    0
}

/// mprotect - set protection on a region of memory (syscall 10).
/// K6-6 (A2.27): Supports changing memory protection between PROT_READ and PROT_WRITE.
pub fn sys_mprotect(addr: u64, len: u64, prot: u64) -> i64 {
    if len == 0 {
        return 0;
    }
    if !addr.is_multiple_of(4096) || !(USER_HEAP_BASE..USER_GUARD_PAGE).contains(&addr) {
        return ERR_LINUX_EINVAL;
    }
    let aligned_len = match len.checked_add(4095) {
        Some(l) => l & !4095,
        None => return ERR_LINUX_EINVAL,
    };
    let end = match addr.checked_add(aligned_len) {
        Some(e) => e,
        None => return ERR_LINUX_EINVAL,
    };
    if end > USER_GUARD_PAGE {
        return ERR_LINUX_EINVAL;
    }
    let cur = current_process();
    let writable = (prot & PROT_WRITE) != 0;
    let mut vpage = addr;
    while vpage < end {
        let ppage = match paging::translate_cr3(cur.cr3, vpage) {
            Some(p) => p,
            None => return -12, // -ENOMEM
        };
        if paging::map_user_page(cur.cr3, vpage, ppage, writable).is_err() {
            return -12;
        }
        vpage += 4096;
    }
    paging::tlb_flush_all();
    0
}

/// ioctl - control device (syscall 16).
/// K6-6 (A2.27): Terminal / device attribute queries return -ENOTTY (-25).
pub fn sys_ioctl(fd: u64, _cmd: u64, _arg: u64) -> i64 {
    sprintln!("sys_ioctl: fd={} -> -ENOTTY", fd);
    ERR_NOTTY
}

/// fstat - get file status by file descriptor (syscall 5).
/// K6-6 (A2.27): Reports size and file mode from FAT32 / console descriptors.
pub fn sys_fstat(fd: u64, statbuf_ptr: u64) -> i64 {
    if statbuf_ptr == 0 || statbuf_ptr >= 0x0000_8000_0000_0000 {
        return ERR_FAULT;
    }
    if fd <= 2 {
        let stat = LinuxStat {
            st_dev: 1,
            st_ino: fd + 1,
            st_nlink: 1,
            st_mode: S_IFCHR | 0o620,
            st_uid: 0,
            st_gid: 0,
            __pad0: 0,
            st_rdev: 5,
            st_size: 0,
            st_blksize: 512,
            st_blocks: 0,
            st_atime: 0,
            st_atime_nsec: 0,
            st_mtime: 0,
            st_mtime_nsec: 0,
            st_ctime: 0,
            st_ctime_nsec: 0,
            __unused: [0; 3],
        };
        let slice = unsafe {
            core::slice::from_raw_parts(
                &stat as *const _ as *const u8,
                core::mem::size_of::<LinuxStat>(),
            )
        };
        if copy_to_user(statbuf_ptr, slice).is_err() {
            return ERR_FAULT;
        }
        return 0;
    }
    if (fd as usize) >= MAX_FDS {
        return ERR_BADF;
    }
    let cur = current_process();
    match &cur.fds[fd as usize] {
        Some(f) => {
            let is_dir = f.is_dir;
            let size = f.size as i64;
            let mode = if is_dir {
                S_IFDIR | 0o755
            } else {
                S_IFREG | 0o644
            };
            let stat = LinuxStat {
                st_dev: 1,
                st_ino: f.first_cluster as u64,
                st_nlink: 1,
                st_mode: mode,
                st_uid: 0,
                st_gid: 0,
                __pad0: 0,
                st_rdev: 0,
                st_size: size,
                st_blksize: 512,
                st_blocks: (size + 511) / 512,
                st_atime: 0,
                st_atime_nsec: 0,
                st_mtime: 0,
                st_mtime_nsec: 0,
                st_ctime: 0,
                st_ctime_nsec: 0,
                __unused: [0; 3],
            };
            let slice = unsafe {
                core::slice::from_raw_parts(
                    &stat as *const _ as *const u8,
                    core::mem::size_of::<LinuxStat>(),
                )
            };
            if copy_to_user(statbuf_ptr, slice).is_err() {
                return ERR_FAULT;
            }
            0
        }
        None => ERR_BADF,
    }
}

/// newfstatat - get file status relative to directory fd (syscall 262).
/// K6-6 (A2.27): Reports size and file mode from FAT32 volume.
pub fn sys_newfstatat(dirfd: i64, path_ptr: u64, statbuf_ptr: u64, flags: u64) -> i64 {
    if statbuf_ptr == 0 || statbuf_ptr >= 0x0000_8000_0000_0000 {
        return ERR_FAULT;
    }
    if path_ptr == 0 {
        if (flags & AT_EMPTY_PATH) != 0 {
            return sys_fstat(dirfd as u64, statbuf_ptr);
        }
        return ERR_FAULT;
    }

    let mut path_buf = [0u8; 256];
    let mut path_len = 0;
    let mut found_null = false;
    for (i, slot) in path_buf.iter_mut().enumerate() {
        let mut b = [0u8; 1];
        if copy_from_user(path_ptr + (i as u64), &mut b).is_err() {
            return ERR_FAULT;
        }
        if b[0] == 0 {
            path_len = i;
            found_null = true;
            break;
        }
        *slot = b[0];
    }
    if !found_null && path_len == 0 {
        return ERR_LINUX_EINVAL;
    }
    if path_len == 0 {
        if (flags & AT_EMPTY_PATH) != 0 {
            return sys_fstat(dirfd as u64, statbuf_ptr);
        }
        return ERR_ENOENT;
    }

    let cur = current_process();
    if !cur.caps.grants(Resource::FsRead) {
        sprintln!(
            "cap gate: PID {} ({}) DENIED resource {:?}",
            cur.pid,
            cur.name_str(),
            Resource::FsRead
        );
        return ERR_PERM;
    }

    let path_str = match core::str::from_utf8(&path_buf[..path_len]) {
        Ok(s) => s,
        Err(_) => return ERR_LINUX_EINVAL,
    };

    let vol = match crate::fat::get_active_fat_volume() {
        Some(v) => v,
        None => return ERR_INVAL,
    };

    let entry = match vol.lookup_path(path_str) {
        Ok(e) => e,
        Err(_) => return ERR_ENOENT,
    };

    let is_dir = entry.is_dir();
    let size = entry.size as i64;
    let mode = if is_dir {
        S_IFDIR | 0o755
    } else {
        S_IFREG | 0o644
    };
    let stat = LinuxStat {
        st_dev: 1,
        st_ino: entry.first_cluster as u64,
        st_nlink: 1,
        st_mode: mode,
        st_uid: 0,
        st_gid: 0,
        __pad0: 0,
        st_rdev: 0,
        st_size: size,
        st_blksize: 512,
        st_blocks: (size + 511) / 512,
        st_atime: 0,
        st_atime_nsec: 0,
        st_mtime: 0,
        st_mtime_nsec: 0,
        st_ctime: 0,
        st_ctime_nsec: 0,
        __unused: [0; 3],
    };
    let slice = unsafe {
        core::slice::from_raw_parts(
            &stat as *const _ as *const u8,
            core::mem::size_of::<LinuxStat>(),
        )
    };
    if copy_to_user(statbuf_ptr, slice).is_err() {
        return ERR_FAULT;
    }
    0
}

/// arch_prctl - set architecture-specific thread state.
/// K6-4 (A2.25): Supports ARCH_SET_FS (0x1002) and ARCH_GET_FS (0x1003).
pub fn sys_arch_prctl(code: u64, addr: u64) -> i64 {
    let cur = current_process_mut();
    match code {
        ARCH_SET_FS => {
            cur.fs_base = addr;
            unsafe {
                wrmsr(MSR_FS_BASE, addr);
            }
            0
        }
        ARCH_GET_FS => {
            let bytes = cur.fs_base.to_ne_bytes();
            if copy_to_user(addr, &bytes).is_err() {
                return ERR_FAULT;
            }
            0
        }
        _ => ERR_LINUX_EINVAL,
    }
}

/// set_tid_address - set pointer to thread ID.
/// K6-4 (A2.25): Records child TID address and returns current PID.
pub fn sys_set_tid_address(tidptr: u64) -> i64 {
    let cur = current_process_mut();
    cur.clear_child_tid = tidptr;
    cur.pid as i64
}

#[repr(C)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct LinuxTimespec {
    pub tv_sec: i64,
    pub tv_nsec: i64,
}

pub const CLOCK_REALTIME: u64 = 0;
pub const CLOCK_MONOTONIC: u64 = 1;
pub const CLOCK_PROCESS_CPUTIME_ID: u64 = 2;
pub const CLOCK_THREAD_CPUTIME_ID: u64 = 3;
pub const CLOCK_MONOTONIC_RAW: u64 = 4;
pub const CLOCK_REALTIME_COARSE: u64 = 5;
pub const CLOCK_MONOTONIC_COARSE: u64 = 6;
pub const CLOCK_BOOTTIME: u64 = 7;

/// Calibrate TSC frequency against the CMOS RTC.
/// Measures the number of TSC cycles between two consecutive RTC second transitions (1.0 second).
pub fn calibrate_timer() {
    let t_start = rdtsc();
    BOOT_TSC.store(t_start, Ordering::SeqCst);

    let base_epoch = crate::rtc::read_datetime()
        .map(|dt| dt.epoch())
        .unwrap_or(0);
    RTC_BASE_EPOCH.store(base_epoch, Ordering::SeqCst);

    // Synchronize to the start of an RTC second
    let s0 = match crate::rtc::read_datetime() {
        Ok(dt) => dt.second,
        Err(_) => 0,
    };
    loop {
        if let Ok(dt) = crate::rtc::read_datetime() {
            if dt.second != s0 {
                break;
            }
        }
        core::hint::spin_loop();
    }
    let t0 = rdtsc();
    let s1 = match crate::rtc::read_datetime() {
        Ok(dt) => dt.second,
        Err(_) => 0,
    };
    // Wait until next RTC second transition (exactly 1.0 second)
    loop {
        if let Ok(dt) = crate::rtc::read_datetime() {
            if dt.second != s1 {
                break;
            }
        }
        core::hint::spin_loop();
    }
    let t1 = rdtsc();
    let cycles = t1.saturating_sub(t0);
    let freq = if (10_000_000..=10_000_000_000).contains(&cycles) {
        cycles
    } else {
        2_500_000_000
    };
    TSC_FREQ_HZ.store(freq, Ordering::SeqCst);
    sprintln!("timer: calibrated TSC frequency = {} Hz", freq);
}

pub fn monotonic_now() -> (u64, u64) {
    if TSC_FREQ_HZ.load(Ordering::Relaxed) == 0 {
        calibrate_timer();
    }
    let freq = TSC_FREQ_HZ.load(Ordering::Relaxed);
    let freq = if freq == 0 { 2_500_000_000 } else { freq };
    let now = rdtsc();
    let boot = BOOT_TSC.load(Ordering::Relaxed);
    let elapsed = now.saturating_sub(boot);

    let sec = elapsed / freq;
    let rem = elapsed % freq;
    let nsec = ((rem as u128 * 1_000_000_000) / (freq as u128)) as u64;

    let total_ns = sec.saturating_mul(1_000_000_000).saturating_add(nsec);
    let mut last = LAST_MONO_NS.load(Ordering::Relaxed);
    loop {
        let target = if total_ns > last { total_ns } else { last + 1 };
        match LAST_MONO_NS.compare_exchange_weak(last, target, Ordering::SeqCst, Ordering::Relaxed)
        {
            Ok(_) => {
                let s = target / 1_000_000_000;
                let ns = target % 1_000_000_000;
                return (s, ns);
            }
            Err(actual) => last = actual,
        }
    }
}

/// clock_gettime - retrieve the time of the specified clock.
/// K6-5 (A2.26): Supports CLOCK_REALTIME (0), CLOCK_MONOTONIC (1),
/// CLOCK_MONOTONIC_RAW (4), CLOCK_BOOTTIME (7).
pub fn sys_clock_gettime(clock_id: u64, tp_ptr: u64) -> i64 {
    if tp_ptr == 0 {
        return ERR_FAULT;
    }
    let (sec, nsec) = match clock_id {
        CLOCK_REALTIME | CLOCK_REALTIME_COARSE => {
            let base = RTC_BASE_EPOCH.load(Ordering::Relaxed);
            let (m_sec, m_nsec) = monotonic_now();
            (base.saturating_add(m_sec), m_nsec)
        }
        CLOCK_MONOTONIC | CLOCK_MONOTONIC_RAW | CLOCK_BOOTTIME | CLOCK_MONOTONIC_COARSE => {
            monotonic_now()
        }
        _ => return ERR_LINUX_EINVAL,
    };

    let ts = LinuxTimespec {
        tv_sec: sec as i64,
        tv_nsec: nsec as i64,
    };
    let ts_bytes = unsafe {
        core::slice::from_raw_parts(
            &ts as *const LinuxTimespec as *const u8,
            core::mem::size_of::<LinuxTimespec>(),
        )
    };
    if copy_to_user(tp_ptr, ts_bytes).is_err() {
        return ERR_FAULT;
    }
    0
}

/// Mediate resource access through the process's capability table.
pub fn syscall_request_resource(resource: Resource) -> i64 {
    let cur = current_process();
    // Deny-by-default check against process's explicit capability set
    if cur.caps.grants(resource) {
        0 // Access granted
    } else {
        ERR_PERM // Denied
    }
}

pub fn get_process_syscall_cs(pid: usize) -> Option<u64> {
    if pid < MAX_PROCS {
        unsafe { Some(PROCESS_TABLE[pid].last_syscall_cs) }
    } else {
        None
    }
}

pub fn get_process_fault_cs(pid: usize) -> Option<u64> {
    if pid < MAX_PROCS {
        unsafe { Some(PROCESS_TABLE[pid].last_fault_cs) }
    } else {
        None
    }
}

pub fn get_process_ustack_frame(pid: usize) -> Option<u64> {
    if pid < MAX_PROCS {
        unsafe { Some(PROCESS_TABLE[pid].ustack_frame) }
    } else {
        None
    }
}

pub fn get_process_fd_state(pid: usize, fd: usize) -> Option<Option<OpenFile>> {
    if pid < MAX_PROCS && fd < MAX_FDS {
        unsafe { Some(PROCESS_TABLE[pid].fds[fd]) }
    } else {
        None
    }
}

pub fn get_process_cr3(pid: usize) -> Option<u64> {
    if pid < MAX_PROCS {
        unsafe { Some(PROCESS_TABLE[pid].cr3) }
    } else {
        None
    }
}

pub fn get_process_brk(pid: usize) -> Option<u64> {
    if pid < MAX_PROCS {
        unsafe { Some(PROCESS_TABLE[pid].brk) }
    } else {
        None
    }
}

/// Syscall dispatcher called from IDT gate 0x80.
#[no_mangle]
pub extern "C" fn syscall_dispatch(
    num: u64,
    arg1: u64,
    arg2: u64,
    arg3: u64,
    arg4: u64,
    frame_cs: u64,
) -> i64 {
    let cur = current_process_mut();
    cur.last_syscall_cs = frame_cs;
    match num {
        SYS_LINUX_SCHED_YIELD => 0,
        SYS_EXIT | SYS_LINUX_EXIT | SYS_LINUX_EXIT_GROUP => unsafe {
            let cur = current_process_mut();
            cur.state = ProcessState::Exited;
            cur.exit_reason = ProcessExitReason::Exited(arg1);
            sprintln!(
                "proc: PID {} ({}) exited with code {}",
                cur.pid,
                cur.name_str(),
                arg1
            );
            return_to_kernel(0);
        },
        SYS_REQUEST_RESOURCE => {
            let res = match Resource::from_index(arg1 as usize) {
                Some(r) => r,
                None => return ERR_INVAL,
            };
            let cur = current_process();
            if cur.caps.grants(res) {
                sprintln!(
                    "cap gate: PID {} ({}) GRANTED resource {:?}",
                    cur.pid,
                    cur.name_str(),
                    res
                );
                0
            } else {
                sprintln!(
                    "cap gate: PID {} ({}) DENIED resource {:?}",
                    cur.pid,
                    cur.name_str(),
                    res
                );
                ERR_PERM
            }
        }
        SYS_OPEN => sys_open(arg1, arg2, arg3),
        SYS_LINUX_READ => sys_read(arg1, arg2, arg3),
        SYS_LINUX_WRITE => sys_write(arg1, arg2, arg3),
        SYS_CLOSE => sys_close(arg1),
        SYS_LINUX_FSTAT => sys_fstat(arg1, arg2),
        SYS_BRK | 7 => sys_brk(arg1),
        SYS_MMAP | 8 => sys_mmap(arg1, arg2, arg3),
        SYS_LINUX_MPROTECT => sys_mprotect(arg1, arg2, arg3),
        SYS_MUNMAP => sys_munmap(arg1, arg2),
        SYS_LINUX_IOCTL => sys_ioctl(arg1, arg2, arg3),
        SYS_LINUX_ARCH_PRCTL => sys_arch_prctl(arg1, arg2),
        SYS_LINUX_SET_TID_ADDRESS => sys_set_tid_address(arg1),
        SYS_LINUX_CLOCK_GETTIME => sys_clock_gettime(arg1, arg2),
        SYS_LINUX_NEWFSTATAT => sys_newfstatat(arg1 as i64, arg2, arg3, arg4),
        SYS_LINUX_PRLIMIT64 | SYS_LINUX_GETRANDOM => {
            sprintln!("syscall: fallback syscall {} -> -ENOSYS", num);
            ERR_NOSYS
        }
        _ => {
            sprintln!(
                "syscall: unknown syscall {} from PID {} ({})",
                num,
                cur.pid,
                cur.name_str()
            );
            ERR_NOSYS
        }
    }
}

/// Switch execution context to a process.
pub fn switch_to(pid: usize) {
    unsafe {
        if pid >= MAX_PROCS || PROCESS_TABLE[pid].state == ProcessState::Unused {
            return;
        }

        // K6-4 (A2.25): Save FS base of current process if valid
        if CURRENT_PID < MAX_PROCS {
            PROCESS_TABLE[CURRENT_PID].fs_base = rdmsr(MSR_FS_BASE);
        }

        CURRENT_PID = pid;
        let proc = &mut PROCESS_TABLE[pid];
        proc.state = ProcessState::Running;

        // Reload TSS.rsp0 with the process's dedicated kernel stack top
        if proc.kstack_top != 0 {
            crate::gdt::set_tss_rsp0(proc.kstack_top);
        }

        // Reload CR3 with the process's address space
        if proc.cr3 != 0 {
            core::arch::asm!(
                "mov cr3, {}",
                in(reg) proc.cr3,
                options(nostack, preserves_flags)
            );
        }

        // K6-4 (A2.25): Reload FS selector with User Data (0x33) and restore FS base MSR
        core::arch::asm!(
            "mov ax, 0x33",
            "mov fs, ax",
            out("ax") _,
            options(nomem, nostack, preserves_flags)
        );
        wrmsr(MSR_FS_BASE, proc.fs_base);
    }
}

// ---------------------------------------------------------------------------
// User Test Payloads (Machine Code)
// ---------------------------------------------------------------------------

extern "C" {
    fn novad_user_entry();
    fn novad_user_end();
    fn bad_mem_user_entry();
    fn bad_mem_user_end();
    fn bad_priv_user_entry();
    fn bad_priv_user_end();
    fn spin_user_entry();
    fn spin_user_end();
    fn worker_user_entry();
    fn worker_user_end();
    fn file_reader_user_entry();
    fn file_reader_user_end();
    fn file_denied_user_entry();
    fn file_denied_user_end();
    fn mem_alloc_user_entry();
    fn mem_alloc_user_end();
    fn guard_page_user_entry();
    fn guard_page_user_end();
    fn mem_owner_user_entry();
    fn mem_owner_user_end();
    fn mem_snooper_user_entry();
    fn mem_snooper_user_end();
    fn unk_syscall_user_entry();
    fn unk_syscall_user_end();
    fn ro_mem_user_entry();
    fn ro_mem_user_end();
    fn write_syscall_user_entry();
    fn write_syscall_user_end();
    fn exit_group_user_entry();
    fn exit_group_user_end();
    fn fs_user1_entry();
    fn fs_user1_end();
    fn fs_user2_entry();
    fn fs_user2_end();
    fn fs_reader_user_entry();
    fn fs_reader_user_end();
    fn clock_monotonic_user_entry();
    fn clock_monotonic_user_end();
    fn fallback_syscall_user_entry();
    fn fallback_syscall_user_end();
    fn linux_stack_user_entry();
    fn linux_stack_user_end();
}

pub fn novad_code_slice() -> &'static [u8] {
    unsafe {
        let start = novad_user_entry as *const () as usize;
        let end = novad_user_end as *const () as usize;
        core::slice::from_raw_parts(start as *const u8, end - start)
    }
}

pub fn bad_mem_code_slice() -> &'static [u8] {
    unsafe {
        let start = bad_mem_user_entry as *const () as usize;
        let end = bad_mem_user_end as *const () as usize;
        core::slice::from_raw_parts(start as *const u8, end - start)
    }
}

pub fn bad_priv_code_slice() -> &'static [u8] {
    unsafe {
        let start = bad_priv_user_entry as *const () as usize;
        let end = bad_priv_user_end as *const () as usize;
        core::slice::from_raw_parts(start as *const u8, end - start)
    }
}

pub fn spin_user_code_slice() -> &'static [u8] {
    unsafe {
        let start = spin_user_entry as *const () as usize;
        let end = spin_user_end as *const () as usize;
        core::slice::from_raw_parts(start as *const u8, end - start)
    }
}

pub fn worker_user_code_slice() -> &'static [u8] {
    unsafe {
        let start = worker_user_entry as *const () as usize;
        let end = worker_user_end as *const () as usize;
        core::slice::from_raw_parts(start as *const u8, end - start)
    }
}

pub fn file_reader_code_slice() -> &'static [u8] {
    unsafe {
        let start = file_reader_user_entry as *const () as usize;
        let end = file_reader_user_end as *const () as usize;
        core::slice::from_raw_parts(start as *const u8, end - start)
    }
}

pub fn file_denied_code_slice() -> &'static [u8] {
    unsafe {
        let start = file_denied_user_entry as *const () as usize;
        let end = file_denied_user_end as *const () as usize;
        core::slice::from_raw_parts(start as *const u8, end - start)
    }
}

pub fn mem_alloc_code_slice() -> &'static [u8] {
    unsafe {
        let start = mem_alloc_user_entry as *const () as usize;
        let end = mem_alloc_user_end as *const () as usize;
        core::slice::from_raw_parts(start as *const u8, end - start)
    }
}

pub fn guard_page_code_slice() -> &'static [u8] {
    unsafe {
        let start = guard_page_user_entry as *const () as usize;
        let end = guard_page_user_end as *const () as usize;
        core::slice::from_raw_parts(start as *const u8, end - start)
    }
}

pub fn mem_owner_code_slice() -> &'static [u8] {
    unsafe {
        let start = mem_owner_user_entry as *const () as usize;
        let end = mem_owner_user_end as *const () as usize;
        core::slice::from_raw_parts(start as *const u8, end - start)
    }
}

pub fn mem_snooper_code_slice() -> &'static [u8] {
    unsafe {
        let start = mem_snooper_user_entry as *const () as usize;
        let end = mem_snooper_user_end as *const () as usize;
        core::slice::from_raw_parts(start as *const u8, end - start)
    }
}

pub fn unk_syscall_code_slice() -> &'static [u8] {
    unsafe {
        let start = unk_syscall_user_entry as *const () as usize;
        let end = unk_syscall_user_end as *const () as usize;
        core::slice::from_raw_parts(start as *const u8, end - start)
    }
}

pub fn ro_mem_code_slice() -> &'static [u8] {
    unsafe {
        let start = ro_mem_user_entry as *const () as usize;
        let end = ro_mem_user_end as *const () as usize;
        core::slice::from_raw_parts(start as *const u8, end - start)
    }
}

pub fn write_syscall_code_slice() -> &'static [u8] {
    unsafe {
        let start = write_syscall_user_entry as *const () as usize;
        let end = write_syscall_user_end as *const () as usize;
        core::slice::from_raw_parts(start as *const u8, end - start)
    }
}

pub fn exit_group_code_slice() -> &'static [u8] {
    unsafe {
        let start = exit_group_user_entry as *const () as usize;
        let end = exit_group_user_end as *const () as usize;
        core::slice::from_raw_parts(start as *const u8, end - start)
    }
}

pub fn fs_user1_code_slice() -> &'static [u8] {
    unsafe {
        let start = fs_user1_entry as *const () as usize;
        let end = fs_user1_end as *const () as usize;
        core::slice::from_raw_parts(start as *const u8, end - start)
    }
}

pub fn fs_user2_code_slice() -> &'static [u8] {
    unsafe {
        let start = fs_user2_entry as *const () as usize;
        let end = fs_user2_end as *const () as usize;
        core::slice::from_raw_parts(start as *const u8, end - start)
    }
}

pub fn fs_reader_code_slice() -> &'static [u8] {
    unsafe {
        let start = fs_reader_user_entry as *const () as usize;
        let end = fs_reader_user_end as *const () as usize;
        core::slice::from_raw_parts(start as *const u8, end - start)
    }
}

pub fn clock_monotonic_code_slice() -> &'static [u8] {
    unsafe {
        let start = clock_monotonic_user_entry as *const () as usize;
        let end = clock_monotonic_user_end as *const () as usize;
        core::slice::from_raw_parts(start as *const u8, end - start)
    }
}

pub fn fallback_syscall_code_slice() -> &'static [u8] {
    unsafe {
        let start = fallback_syscall_user_entry as *const () as usize;
        let end = fallback_syscall_user_end as *const () as usize;
        core::slice::from_raw_parts(start as *const u8, end - start)
    }
}

pub fn linux_stack_code_slice() -> &'static [u8] {
    unsafe {
        let start = linux_stack_user_entry as *const () as usize;
        let end = linux_stack_user_end as *const () as usize;
        core::slice::from_raw_parts(start as *const u8, end - start)
    }
}

core::arch::global_asm!(
    ".section .text",
    ".globl novad_user_entry",
    ".globl novad_user_end",
    "novad_user_entry:",
    "    mov rax, 3",
    "    mov rdi, 3",
    "    int 0x80",
    "    test rax, rax",
    "    jnz 9f",
    "    mov rax, 3",
    "    mov rdi, 6",
    "    int 0x80",
    "    test rax, rax",
    "    jnz 9f",
    "    mov rax, 3",
    "    mov rdi, 0",
    "    int 0x80",
    "    cmp rax, -1",
    "    jne 9f",
    "    mov rax, 2",
    "    xor rdi, rdi",
    "    int 0x80",
    "    ud2",
    "9:",
    "    mov rdi, 1",
    "    mov rax, 2",
    "    int 0x80",
    "    ud2",
    "novad_user_end:",
    "",
    ".globl bad_mem_user_entry",
    ".globl bad_mem_user_end",
    "bad_mem_user_entry:",
    "    mov rax, 0xffffffff80000000",
    "    mov rax, [rax]",
    "    ud2",
    "bad_mem_user_end:",
    "",
    ".globl bad_priv_user_entry",
    ".globl bad_priv_user_end",
    "bad_priv_user_entry:",
    "    cli",
    "    ud2",
    "bad_priv_user_end:",
    "",
    ".globl spin_user_entry",
    ".globl spin_user_end",
    "spin_user_entry:",
    "1:",
    "    pause",
    "    jmp 1b",
    "spin_user_end:",
    "",
    ".globl worker_user_entry",
    ".globl worker_user_end",
    "worker_user_entry:",
    "    mov rax, 3",
    "    mov rdi, 3",
    "    int 0x80",
    "    mov rax, 2",
    "    mov rdi, 42",
    "    int 0x80",
    "    ud2",
    "worker_user_end:",
    "",
    ".globl file_reader_user_entry",
    ".globl file_reader_user_end",
    "file_reader_user_entry:",
    "    sub rsp, 128",
    "    lea rdi, [rip + 3f]",
    "    mov rsi, 10",
    "    xor rdx, rdx",
    "    mov rax, 4",
    "    int 0x80",
    "    cmp rax, 3",
    "    jl 9f",
    "    mov rbx, rax",
    "    mov rdi, rbx",
    "    mov rsi, rsp",
    "    mov rdx, 16",
    "    mov rax, 0",
    "    int 0x80",
    "    cmp rax, 16",
    "    jne 9f",
    "    mov rdi, rbx",
    "    mov rax, 6",
    "    int 0x80",
    "    cmp rax, 0",
    "    jne 9f",
    "    mov rax, 2",
    "    mov rdi, 42",
    "    int 0x80",
    "    ud2",
    "9:",
    "    mov rax, 2",
    "    mov rdi, 1",
    "    int 0x80",
    "    ud2",
    "3:",
    "    .ascii \"SIMPLE.TXT\"",
    "file_reader_user_end:",
    "",
    ".globl file_denied_user_entry",
    ".globl file_denied_user_end",
    "file_denied_user_entry:",
    "    lea rdi, [rip + 2f]",
    "    mov rsi, 10",
    "    xor rdx, rdx",
    "    mov rax, 4",
    "    int 0x80",
    "    cmp rax, -1",
    "    jne 1f",
    "    mov rax, 2",
    "    mov rdi, 77",
    "    int 0x80",
    "    ud2",
    "1:",
    "    mov rax, 2",
    "    mov rdi, 1",
    "    int 0x80",
    "    ud2",
    "2:",
    "    .ascii \"SIMPLE.TXT\"",
    "file_denied_user_end:",
    "",
    ".globl mem_alloc_user_entry",
    ".globl mem_alloc_user_end",
    "mem_alloc_user_entry:",
    "    mov rax, 12",
    "    mov rdi, 0x00502000",
    "    int 0x80",
    "    cmp rax, 0x00502000",
    "    jne 9f",
    "    mov rbx, 0x00500000",
    "    mov r11, 0x1122334455667788",
    "    mov [rbx], r11",
    "    mov rdx, 0x00501000",
    "    mov r11, 0x7766554433221100",
    "    mov [rdx], r11",
    "    mov rax, 9",
    "    xor rdi, rdi",
    "    mov rsi, 4096",
    "    mov rdx, 3",
    "    int 0x80",
    "    cmp rax, 0x00700000",
    "    jne 9f",
    "    mov r11, 0x0123456789abcdef",
    "    mov [rax], r11",
    "    mov rbx, 0x00500000",
    "    mov r10, [rbx]",
    "    mov r11, 0x1122334455667788",
    "    cmp r10, r11",
    "    jne 9f",
    "    mov rdx, 0x00501000",
    "    mov r10, [rdx]",
    "    mov r11, 0x7766554433221100",
    "    cmp r10, r11",
    "    jne 9f",
    "    mov rax, 0x00700000",
    "    mov r10, [rax]",
    "    mov r11, 0x0123456789abcdef",
    "    cmp r10, r11",
    "    jne 9f",
    "    mov rax, 11",
    "    mov rdi, 0x00700000",
    "    mov rsi, 4096",
    "    int 0x80",
    "    test rax, rax",
    "    jnz 9f",
    "    mov rax, 12",
    "    mov rdi, 0x00500000",
    "    int 0x80",
    "    cmp rax, 0x00500000",
    "    jne 9f",
    "    mov rax, 2",
    "    mov rdi, 42",
    "    int 0x80",
    "    ud2",
    "9:",
    "    mov rax, 2",
    "    mov rdi, 1",
    "    int 0x80",
    "    ud2",
    "mem_alloc_user_end:",
    "",
    ".globl guard_page_user_entry",
    ".globl guard_page_user_end",
    "guard_page_user_entry:",
    "    mov rax, 0x007fe000",
    "    mov rax, [rax]",
    "    ud2",
    "guard_page_user_end:",
    "",
    ".globl mem_owner_user_entry",
    ".globl mem_owner_user_end",
    "mem_owner_user_entry:",
    "    mov rax, 12",
    "    mov rdi, 0x00501000",
    "    int 0x80",
    "    cmp rax, 0x00501000",
    "    jne 9f",
    "    mov rbx, 0x00500000",
    "    mov rcx, 0x5345435245543121",
    "    mov [rbx], rcx",
    "    mov rax, 2",
    "    mov rdi, 10",
    "    int 0x80",
    "    ud2",
    "9:",
    "    mov rax, 2",
    "    mov rdi, 1",
    "    int 0x80",
    "    ud2",
    "mem_owner_user_end:",
    "",
    ".globl mem_snooper_user_entry",
    ".globl mem_snooper_user_end",
    "mem_snooper_user_entry:",
    "    mov rax, 0x00500000",
    "    mov rax, [rax]",
    "    ud2",
    "mem_snooper_user_end:",
    "",
    ".globl unk_syscall_user_entry",
    ".globl unk_syscall_user_end",
    "unk_syscall_user_entry:",
    "    mov rax, 444",
    "    mov rdi, 0x1111",
    "    mov rsi, 0x2222",
    "    mov rdx, 0x3333",
    "    int 0x80",
    "    cmp rax, -38",
    "    jne 9f",
    "    mov rax, 60",
    "    mov rdi, 42",
    "    int 0x80",
    "    ud2",
    "9:",
    "    mov rax, 60",
    "    mov rdi, 1",
    "    int 0x80",
    "    ud2",
    "unk_syscall_user_end:",
    "",
    ".globl ro_mem_user_entry",
    ".globl ro_mem_user_end",
    "ro_mem_user_entry:",
    "    mov rax, 9",
    "    mov rdi, 0x00710000",
    "    mov rsi, 4096",
    "    mov rdx, 1",
    "    int 0x80",
    "    cmp rax, 0x00710000",
    "    jne 9f",
    "    mov rbx, 0x00710000",
    "    mov qword ptr [rbx], 0x1234",
    "    ud2",
    "9:",
    "    mov rax, 60",
    "    mov rdi, 1",
    "    int 0x80",
    "    ud2",
    "ro_mem_user_end:",
    "",
    ".globl write_syscall_user_entry",
    ".globl write_syscall_user_end",
    "write_syscall_user_entry:",
    "    sub rsp, 16",
    "    mov dword ptr [rsp], 0x54534554",
    "    mov byte ptr [rsp + 4], 0x0a",
    "    mov rax, 1",
    "    mov rdi, 1",
    "    mov rsi, rsp",
    "    mov rdx, 5",
    "    int 0x80",
    "    cmp rax, 5",
    "    jne 1f",
    "    mov rax, 1",
    "    mov rdi, 2",
    "    mov rsi, rsp",
    "    mov rdx, 5",
    "    int 0x80",
    "    cmp rax, 5",
    "    jne 2f",
    "    mov rax, 1",
    "    mov rdi, 3",
    "    mov rsi, rsp",
    "    mov rdx, 5",
    "    int 0x80",
    "    cmp rax, -9",
    "    jne 3f",
    "    mov rax, 1",
    "    mov rdi, 0",
    "    mov rsi, rsp",
    "    mov rdx, 5",
    "    int 0x80",
    "    cmp rax, -9",
    "    jne 4f",
    "    mov rax, 1",
    "    mov rdi, 1",
    "    mov rsi, rsp",
    "    mov rdx, 0",
    "    int 0x80",
    "    cmp rax, 0",
    "    jne 5f",
    "    add rsp, 16",
    "    mov rax, 60",
    "    mov rdi, 77",
    "    int 0x80",
    "    ud2",
    "1:",
    "    add rsp, 16",
    "    mov rax, 60",
    "    mov rdi, 1",
    "    int 0x80",
    "    ud2",
    "2:",
    "    add rsp, 16",
    "    mov rax, 60",
    "    mov rdi, 2",
    "    int 0x80",
    "    ud2",
    "3:",
    "    add rsp, 16",
    "    mov rax, 60",
    "    mov rdi, 3",
    "    int 0x80",
    "    ud2",
    "4:",
    "    add rsp, 16",
    "    mov rax, 60",
    "    mov rdi, 4",
    "    int 0x80",
    "    ud2",
    "5:",
    "    add rsp, 16",
    "    mov rax, 60",
    "    mov rdi, 5",
    "    int 0x80",
    "    ud2",
    "write_syscall_user_end:",
    "",
    ".globl exit_group_user_entry",
    ".globl exit_group_user_end",
    "exit_group_user_entry:",
    "    mov rax, 231",
    "    mov rdi, 88",
    "    int 0x80",
    "    ud2",
    "exit_group_user_end:",
    "",
    ".globl fs_user1_entry",
    ".globl fs_user1_end",
    "fs_user1_entry:",
    "    sub rsp, 64",
    "    mov rax, 0x1111222233334444",
    "    mov [rsp + 16], rax",
    "    mov rax, 158",
    "    mov rdi, 0x1002",
    "    lea rsi, [rsp + 16]",
    "    int 0x80",
    "    cmp rax, 0",
    "    jne 1f",
    "    mov rax, 218",
    "    lea rdi, [rsp + 24]",
    "    int 0x80",
    "    cmp rax, 0",
    "    jle 2f",
    "    mov rbx, qword ptr fs:[0]",
    "    mov rax, 0x1111222233334444",
    "    cmp rbx, rax",
    "    jne 3f",
    "    mov rax, 158",
    "    mov rdi, 0x1003",
    "    lea rsi, [rsp + 32]",
    "    int 0x80",
    "    cmp rax, 0",
    "    jne 4f",
    "    mov rax, [rsp + 32]",
    "    lea rbx, [rsp + 16]",
    "    cmp rax, rbx",
    "    jne 5f",
    "    add rsp, 64",
    "    mov rax, 60",
    "    mov rdi, 91",
    "    int 0x80",
    "    ud2",
    "1:",
    "    add rsp, 64",
    "    mov rax, 60",
    "    mov rdi, 1",
    "    int 0x80",
    "    ud2",
    "2:",
    "    add rsp, 64",
    "    mov rax, 60",
    "    mov rdi, 2",
    "    int 0x80",
    "    ud2",
    "3:",
    "    add rsp, 64",
    "    mov rax, 60",
    "    mov rdi, 3",
    "    int 0x80",
    "    ud2",
    "4:",
    "    add rsp, 64",
    "    mov rax, 60",
    "    mov rdi, 4",
    "    int 0x80",
    "    ud2",
    "5:",
    "    add rsp, 64",
    "    mov rax, 60",
    "    mov rdi, 5",
    "    int 0x80",
    "    ud2",
    "fs_user1_end:",
    "",
    ".globl fs_user2_entry",
    ".globl fs_user2_end",
    "fs_user2_entry:",
    "    sub rsp, 64",
    "    mov rax, 0x5555666677778888",
    "    mov [rsp + 32], rax",
    "    mov rax, 158",
    "    mov rdi, 0x1002",
    "    lea rsi, [rsp + 32]",
    "    int 0x80",
    "    cmp rax, 0",
    "    jne 1f",
    "    mov rax, 218",
    "    lea rdi, [rsp + 40]",
    "    int 0x80",
    "    cmp rax, 0",
    "    jle 2f",
    "    mov rbx, qword ptr fs:[0]",
    "    mov rax, 0x5555666677778888",
    "    cmp rbx, rax",
    "    jne 3f",
    "    mov rax, 158",
    "    mov rdi, 0x1003",
    "    lea rsi, [rsp + 48]",
    "    int 0x80",
    "    cmp rax, 0",
    "    jne 4f",
    "    mov rax, [rsp + 48]",
    "    lea rbx, [rsp + 32]",
    "    cmp rax, rbx",
    "    jne 5f",
    "    add rsp, 64",
    "    mov rax, 60",
    "    mov rdi, 92",
    "    int 0x80",
    "    ud2",
    "1:",
    "    add rsp, 64",
    "    mov rax, 60",
    "    mov rdi, 1",
    "    int 0x80",
    "    ud2",
    "2:",
    "    add rsp, 64",
    "    mov rax, 60",
    "    mov rdi, 2",
    "    int 0x80",
    "    ud2",
    "3:",
    "    add rsp, 64",
    "    mov rax, 60",
    "    mov rdi, 3",
    "    int 0x80",
    "    ud2",
    "4:",
    "    add rsp, 64",
    "    mov rax, 60",
    "    mov rdi, 4",
    "    int 0x80",
    "    ud2",
    "5:",
    "    add rsp, 64",
    "    mov rax, 60",
    "    mov rdi, 5",
    "    int 0x80",
    "    ud2",
    "fs_user2_end:",
    "",
    ".globl fs_reader_user_entry",
    ".globl fs_reader_user_end",
    "fs_reader_user_entry:",
    "    mov rbx, qword ptr fs:[0]",
    "    mov rax, 0x2233445566778899",
    "    cmp rbx, rax",
    "    jne 1f",
    "    mov rax, 60",
    "    mov rdi, 93",
    "    int 0x80",
    "    ud2",
    "1:",
    "    mov rax, 60",
    "    mov rdi, 1",
    "    int 0x80",
    "    ud2",
    "fs_reader_user_end:",
    "",
    ".globl clock_monotonic_user_entry",
    ".globl clock_monotonic_user_end",
    "clock_monotonic_user_entry:",
    "    sub rsp, 48",
    "    mov qword ptr [rsp + 16], 0",
    "    mov qword ptr [rsp + 24], 0",
    "    mov rcx, 1000",
    "1:",
    "    push rcx",
    "    mov rax, 228",
    "    mov rdi, 1",
    "    lea rsi, [rsp + 8]",
    "    int 0x80",
    "    pop rcx",
    "    cmp rax, 0",
    "    jne 2f",
    "    mov rax, [rsp + 8]",
    "    cmp rax, 1000000000",
    "    jae 3f",
    "    mov r8, [rsp]",
    "    mov r9, [rsp + 8]",
    "    mov r10, [rsp + 16]",
    "    mov r11, [rsp + 24]",
    "    cmp r8, r10",
    "    jb 4f",
    "    ja 5f",
    "    cmp r9, r11",
    "    jb 4f",
    "5:",
    "    mov [rsp + 16], r8",
    "    mov [rsp + 24], r9",
    "    dec rcx",
    "    jnz 1b",
    "    add rsp, 48",
    "    mov rax, 60",
    "    mov rdi, 95",
    "    int 0x80",
    "    ud2",
    "2:",
    "    add rsp, 48",
    "    mov rax, 60",
    "    mov rdi, 1",
    "    int 0x80",
    "    ud2",
    "3:",
    "    add rsp, 48",
    "    mov rax, 60",
    "    mov rdi, 2",
    "    int 0x80",
    "    ud2",
    "4:",
    "    add rsp, 48",
    "    mov rax, 60",
    "    mov rdi, 3",
    "    int 0x80",
    "    ud2",
    "clock_monotonic_user_end:",
    "",
    ".globl fallback_syscall_user_entry",
    ".globl fallback_syscall_user_end",
    "fallback_syscall_user_entry:",
    "    mov rax, 16",
    "    mov rdi, 1",
    "    mov rsi, 0x5413",
    "    mov rdx, rsp",
    "    int 0x80",
    "    cmp rax, -25",
    "    jne 1f",
    "    mov rax, 302",
    "    mov rdi, 0",
    "    mov rsi, 3",
    "    mov rdx, 0",
    "    mov r10, rsp",
    "    int 0x80",
    "    cmp rax, -38",
    "    jne 2f",
    "    mov rax, 318",
    "    mov rdi, rsp",
    "    mov rsi, 16",
    "    mov rdx, 0",
    "    int 0x80",
    "    cmp rax, -38",
    "    jne 3f",
    "    mov rax, 9",
    "    mov rdi, 0x00720000",
    "    mov rsi, 4096",
    "    mov rdx, 1",
    "    int 0x80",
    "    cmp rax, 0x00720000",
    "    jne 4f",
    "    mov rax, 10",
    "    mov rdi, 0x00720000",
    "    mov rsi, 4096",
    "    mov rdx, 3",
    "    int 0x80",
    "    cmp rax, 0",
    "    jne 5f",
    "    mov rbx, 0x00720000",
    "    mov qword ptr [rbx], 0xCAFE",
    "    mov rax, 11",
    "    mov rdi, 0x00720000",
    "    mov rsi, 4096",
    "    int 0x80",
    "    cmp rax, 0",
    "    jne 6f",
    "    mov rax, 60",
    "    mov rdi, 96",
    "    int 0x80",
    "    ud2",
    "1:",
    "    mov rax, 60",
    "    mov rdi, 1",
    "    int 0x80",
    "    ud2",
    "2:",
    "    mov rax, 60",
    "    mov rdi, 2",
    "    int 0x80",
    "    ud2",
    "3:",
    "    mov rax, 60",
    "    mov rdi, 3",
    "    int 0x80",
    "    ud2",
    "4:",
    "    mov rax, 60",
    "    mov rdi, 4",
    "    int 0x80",
    "    ud2",
    "5:",
    "    mov rax, 60",
    "    mov rdi, 5",
    "    int 0x80",
    "    ud2",
    "6:",
    "    mov rax, 60",
    "    mov rdi, 6",
    "    int 0x80",
    "    ud2",
    "fallback_syscall_user_end:",
    "",
    ".globl linux_stack_user_entry",
    ".globl linux_stack_user_end",
    "linux_stack_user_entry:",
    "    mov rax, rsp",
    "    and rax, 15",
    "    jnz 10f",
    "    mov rax, [rsp]",
    "    cmp rax, 3",
    "    jne 11f",
    "    mov rsi, [rsp + 8]",
    "    test rsi, rsi",
    "    jz 12f",
    "    cmp dword ptr [rsi], 0x74736574",
    "    jne 12f",
    "    mov rsi, [rsp + 16]",
    "    test rsi, rsi",
    "    jz 13f",
    "    cmp dword ptr [rsi], 0x31677261",
    "    jne 13f",
    "    mov rsi, [rsp + 24]",
    "    test rsi, rsi",
    "    jz 14f",
    "    cmp dword ptr [rsi], 0x32677261",
    "    jne 14f",
    "    mov rsi, [rsp + 32]",
    "    test rsi, rsi",
    "    jnz 15f",
    "    mov rsi, [rsp + 40]",
    "    test rsi, rsi",
    "    jz 16f",
    "    cmp dword ptr [rsi], 0x00563d4b",
    "    jne 16f",
    "    mov rsi, [rsp + 48]",
    "    test rsi, rsi",
    "    jnz 17f",
    "    xor r8, r8",
    "    lea rdi, [rsp + 56]",
    "2:",
    "    mov rbx, [rdi]",
    "    test rbx, rbx",
    "    jz 3f",
    "    mov rcx, [rdi + 8]",
    "    cmp rbx, 3",
    "    jne 4f",
    "    cmp rcx, 0x00400040",
    "    jne 18f",
    "    or r8, 1",
    "    jmp 9f",
    "4:",
    "    cmp rbx, 5",
    "    jne 5f",
    "    cmp rcx, 4",
    "    jne 19f",
    "    or r8, 2",
    "    jmp 9f",
    "5:",
    "    cmp rbx, 6",
    "    jne 6f",
    "    cmp rcx, 4096",
    "    jne 20f",
    "    or r8, 4",
    "    jmp 9f",
    "6:",
    "    cmp rbx, 9",
    "    jne 7f",
    "    cmp rcx, 0x00400000",
    "    jne 21f",
    "    or r8, 8",
    "    jmp 9f",
    "7:",
    "    cmp rbx, 25",
    "    jne 9f",
    "    cmp rcx, 0x007FF000",
    "    jb 22f",
    "    mov rdx, [rcx]",
    "    test rdx, rdx",
    "    jz 22f",
    "    or r8, 16",
    "9:",
    "    add rdi, 16",
    "    jmp 2b",
    "3:",
    "    cmp r8, 31",
    "    jne 23f",
    "    mov rax, 60",
    "    mov rdi, 97",
    "    int 0x80",
    "    ud2",
    "10:",
    "    mov rax, 60",
    "    mov rdi, 10",
    "    int 0x80",
    "    ud2",
    "11:",
    "    mov rax, 60",
    "    mov rdi, 11",
    "    int 0x80",
    "    ud2",
    "12:",
    "    mov rax, 60",
    "    mov rdi, 12",
    "    int 0x80",
    "    ud2",
    "13:",
    "    mov rax, 60",
    "    mov rdi, 13",
    "    int 0x80",
    "    ud2",
    "14:",
    "    mov rax, 60",
    "    mov rdi, 14",
    "    int 0x80",
    "    ud2",
    "15:",
    "    mov rax, 60",
    "    mov rdi, 15",
    "    int 0x80",
    "    ud2",
    "16:",
    "    mov rax, 60",
    "    mov rdi, 16",
    "    int 0x80",
    "    ud2",
    "17:",
    "    mov rax, 60",
    "    mov rdi, 17",
    "    int 0x80",
    "    ud2",
    "18:",
    "    mov rax, 60",
    "    mov rdi, 18",
    "    int 0x80",
    "    ud2",
    "19:",
    "    mov rax, 60",
    "    mov rdi, 19",
    "    int 0x80",
    "    ud2",
    "20:",
    "    mov rax, 60",
    "    mov rdi, 20",
    "    int 0x80",
    "    ud2",
    "21:",
    "    mov rax, 60",
    "    mov rdi, 21",
    "    int 0x80",
    "    ud2",
    "22:",
    "    mov rax, 60",
    "    mov rdi, 22",
    "    int 0x80",
    "    ud2",
    "23:",
    "    mov rax, 60",
    "    mov rdi, 23",
    "    int 0x80",
    "    ud2",
    "linux_stack_user_end:"
);
