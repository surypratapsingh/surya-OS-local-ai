//! K4 Cooperative & Preemptive Scheduler.
//!
//! Owns process scheduling and boots `novad` as PID 1 in Ring 3.
//! Capability boundary enforced on every syscall, with hardware isolation
//! via Ring 3 and per-process PML4 address spaces.

use crate::cap::CapSet;
use crate::proc::{self, ProcessState, MAX_PROCS, PID_KERNEL, PID_NOVAD};

static mut SCHEDULER_INITIALIZED: bool = false;

/// Initialize the scheduler and spawn `novad` (PID 1).
pub fn init() {
    unsafe {
        if SCHEDULER_INITIALIZED {
            return;
        }

        // Initialize PID 0 (kernel / idle task)
        let kernel_proc = proc::get_process_mut(PID_KERNEL).expect("kernel process missing");
        kernel_proc.set_name("kernel_idle");
        kernel_proc.state = ProcessState::Running;
        kernel_proc.cr3 = crate::paging::kernel_pml4();

        // Spawn PID 1: `novad` with default supervisor capabilities from manifest
        let novad_caps = CapSet::from_manifest(&[
            ("fs:read", true),
            ("compute:expression", true),
            ("compute:verify", true),
        ])
        .expect("novad manifest valid");

        let pid = proc::spawn_user("novad", novad_caps, proc::novad_code_slice())
            .expect("failed to spawn novad");
        assert!(pid == PID_NOVAD, "novad must receive PID 1");

        SCHEDULER_INITIALIZED = true;
        sprintln!("sched:       scheduler initialized; PID 1 (novad) spawned in ring 3");
    }
}

/// Pick the next ready process and switch to it.
pub fn schedule() {
    unsafe {
        if !SCHEDULER_INITIALIZED {
            return;
        }

        let cur = proc::current_process().pid;
        let mut next = None;

        // Round-robin search for next Ready or Running process
        for i in 1..MAX_PROCS {
            let candidate = (cur + i) % MAX_PROCS;
            if let Some(p) = proc::get_process(candidate) {
                if p.state == ProcessState::Ready || p.state == ProcessState::Running {
                    next = Some(candidate);
                    break;
                }
            }
        }

        if let Some(next_pid) = next {
            if next_pid != cur {
                proc::switch_to(next_pid);
            }
        }
    }
}

/// Yield execution to the next process.
pub fn yield_now() {
    schedule();
}
