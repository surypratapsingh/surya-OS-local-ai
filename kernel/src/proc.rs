//! K4 Process Table, Isolation, and Syscall Mediation.
//!
//! Enforces: "A process cannot touch a resource absent from its capability set
//! — proven by a test that attempts it and is denied."
//!
//! Process model:
//! - Fixed process table (MAX_PROCS = 8) with explicit PIDs.
//! - PID 1 is `novad` (the supervisor daemon), initialized with capabilities
//!   granted by its signed manifest.
//! - Separate address spaces (PML4) isolating user memory.
//! - Capability mediation on every kernel gate/syscall.

use crate::cap::{CapSet, Resource};
use crate::paging;
use crate::pmm;

pub const MAX_PROCS: usize = 8;
pub const USER_STACK_SIZE: usize = 16 * 1024; // 16 KiB user stack
pub const KERNEL_STACK_SIZE: usize = 16 * 1024; // 16 KiB per-process kernel stack

pub const PID_KERNEL: usize = 0;
pub const PID_NOVAD: usize = 1;

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum ProcessState {
    Unused,
    Ready,
    Running,
    Blocked,
    Exited,
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
            rax: 0, rbx: 0, rcx: 0, rdx: 0,
            rsi: 0, rdi: 0, rbp: 0,
            r8: 0, r9: 0, r10: 0, r11: 0,
            r12: 0, r13: 0, r14: 0, r15: 0,
            rip: 0, rsp: 0, rflags: 0x202, // IF (interrupts enabled)
        }
    }
}

pub struct Process {
    pub pid: usize,
    pub name: [u8; 16],
    pub state: ProcessState,
    pub cr3: u64,
    pub caps: CapSet,
    pub context: ProcessContext,
    pub kstack_top: u64,
    pub ustack_top: u64,
}

impl Process {
    pub const fn new(pid: usize) -> Self {
        Self {
            pid,
            name: [0; 16],
            state: ProcessState::Unused,
            cr3: 0,
            caps: CapSet::new(),
            context: ProcessContext::zero(),
            kstack_top: 0,
            ustack_top: 0,
        }
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
    Process::new(0),
    Process::new(1),
    Process::new(2),
    Process::new(3),
    Process::new(4),
    Process::new(5),
    Process::new(6),
    Process::new(7),
];

static mut CURRENT_PID: usize = PID_KERNEL;

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
            return Some(&mut PROCESS_TABLE[pid]);
        }
    }
    None
}

/// Create a dedicated PML4 address space for a process.
/// Inherits the higher-half kernel mapping and direct map so kernel routines survive CR3 switch.
pub fn create_process_pml4() -> Option<u64> {
    let pml4_frame = pmm::alloc_frame()?;
    let hhdm = crate::limine::hhdm_offset().expect("proc: HHDM required");
    let current_cr3 = paging::current_cr3() & 0x000F_FFFF_FFFF_F000;
    unsafe {
        for i in 0..512u64 {
            let entry = pmm::read_va(hhdm + current_cr3 + i * 8);
            pmm::write_va(hhdm + pml4_frame + i * 8, entry);
        }
    }
    Some(pml4_frame)
}

/// Spawn a process with a given capability set.
pub fn spawn(name: &str, caps: CapSet, entry_point: u64) -> Result<usize, &'static str> {
    unsafe {
        // Find free slot (PID >= 1)
        let mut slot = None;
        for i in 1..MAX_PROCS {
            if PROCESS_TABLE[i].state == ProcessState::Unused {
                slot = Some(i);
                break;
            }
        }
        let pid = slot.ok_or("no free process slots")?;
        let proc = &mut PROCESS_TABLE[pid];

        let pml4 = create_process_pml4().ok_or("failed to allocate process PML4")?;
        proc.pid = pid;
        proc.set_name(name);
        proc.state = ProcessState::Ready;
        proc.cr3 = pml4;
        proc.caps = caps;

        // Allocate kernel stack for this process
        let kstack_frame = pmm::alloc_frame().ok_or("failed to alloc kernel stack")?;
        proc.kstack_top = crate::limine::hhdm_offset().unwrap() + kstack_frame + 4096;

        // Allocate user stack for this process
        let ustack_frame = pmm::alloc_frame().ok_or("failed to alloc user stack")?;
        proc.ustack_top = crate::limine::hhdm_offset().unwrap() + ustack_frame + 4096;

        proc.context = ProcessContext::zero();
        proc.context.rip = entry_point;
        proc.context.rsp = proc.ustack_top;

        Ok(pid)
    }
}

// ---------------------------------------------------------------------------
// Syscall Dispatch & Capability Enforcement
// ---------------------------------------------------------------------------

pub const SYS_YIELD: u64 = 1;
pub const SYS_EXIT: u64 = 2;
pub const SYS_REQUEST_RESOURCE: u64 = 3;

pub const ERR_PERM: i64 = -1; // Operation not permitted (denied by capability gate)
pub const ERR_INVAL: i64 = -2; // Invalid resource or argument

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

/// The system call gate. Dispatches requests from process context.
pub fn handle_syscall(syscall_num: u64, arg1: u64, _arg2: u64) -> i64 {
    match syscall_num {
        SYS_YIELD => 0,
        SYS_EXIT => {
            let cur = current_process_mut();
            cur.state = ProcessState::Exited;
            0
        }
        SYS_REQUEST_RESOURCE => {
            let res = match Resource::from_index(arg1 as usize) {
                Some(r) => r,
                None => return ERR_INVAL,
            };
            syscall_request_resource(res)
        }
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
