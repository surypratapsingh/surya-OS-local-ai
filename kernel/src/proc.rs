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
    spawn_user(pkg.id, pkg.caps, payload)
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
        if let Some(hhdm) = crate::limine::hhdm_offset() {
            let kstack_frame = proc.kstack_top - 4096 - hhdm;
            pmm::free_frame(kstack_frame);
        }
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

        // Allocate kernel stack for TSS.rsp0 transitions
        let kstack_frame = pmm::alloc_frame().ok_or("failed to alloc kernel stack")?;
        pmm::zero_frame(kstack_frame);
        let kstack_top = hhdm + kstack_frame + 4096;

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

        let kstack_frame = pmm::alloc_frame().ok_or("failed to alloc kernel stack")?;
        proc.kstack_top = crate::limine::hhdm_offset().unwrap() + kstack_frame + 4096;

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
    "    mov fs, ax",
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
    "    mov fs, ax",
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
        CURRENT_PID = pid;
        KERNEL_PML4_PHYS = paging::kernel_pml4();
        let proc = &mut PROCESS_TABLE[pid];
        proc.state = ProcessState::Running;

        // Arm TSS.rsp0 with this process's kernel stack
        crate::gdt::set_tss_rsp0(proc.kstack_top);

        // Switch to process PML4
        core::arch::asm!("mov cr3, {}", in(reg) proc.cr3, options(nostack, preserves_flags));

        // Enter user mode (returns when return_to_kernel is invoked)
        let _ = enter_user_mode(proc.context.rip, proc.context.rsp, proc.context.rflags);

        // Restored to kernel context
        CURRENT_PID = PID_KERNEL;
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

pub const SYS_YIELD: u64 = 1;
pub const SYS_EXIT: u64 = 2;
pub const SYS_REQUEST_RESOURCE: u64 = 3;
pub const SYS_OPEN: u64 = 4;
pub const SYS_READ: u64 = 5;
pub const SYS_CLOSE: u64 = 6;
pub const SYS_BRK: u64 = 12;
pub const SYS_MMAP: u64 = 9;
pub const SYS_MUNMAP: u64 = 11;

pub const PROT_NONE: u64 = 0;
pub const PROT_READ: u64 = 1;
pub const PROT_WRITE: u64 = 2;
pub const PROT_EXEC: u64 = 4;

pub const ERR_PERM: i64 = -1; // Operation not permitted (denied by capability gate)
pub const ERR_INVAL: i64 = -2; // Invalid resource or argument

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

    let writable = (prot & PROT_WRITE) != 0 || prot == 0;
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
    frame_cs: u64,
) -> i64 {
    let cur = current_process_mut();
    cur.last_syscall_cs = frame_cs;
    match num {
        SYS_YIELD => 0,
        SYS_EXIT => unsafe {
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
        SYS_READ => sys_read(arg1, arg2, arg3),
        SYS_CLOSE => sys_close(arg1),
        SYS_BRK | 7 => sys_brk(arg1),
        SYS_MMAP | 8 => sys_mmap(arg1, arg2, arg3),
        SYS_MUNMAP | 10 => sys_munmap(arg1, arg2),
        _ => ERR_INVAL,
    }
}

/// Switch execution context to a process.
pub fn switch_to(pid: usize) {
    unsafe {
        if pid >= MAX_PROCS || PROCESS_TABLE[pid].state == ProcessState::Unused {
            return;
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
    "    mov rax, 5",
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
    "mem_snooper_user_end:"
);
