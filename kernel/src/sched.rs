//! K4 Cooperative & Preemptive Scheduler.
//!
//! Owns process scheduling and boots `novad` as PID 1 in Ring 3 from an
//! authenticated package. Local APIC timer drives preemptive round-robin
//! scheduling across user processes.

use crate::cap::CapSet;
use crate::proc::{self, ProcessState, MAX_PROCS, PID_KERNEL, PID_NOVAD};

static mut SCHEDULER_INITIALIZED: bool = false;
static mut PREEMPTION_ACTIVE: bool = false;
static mut PREEMPTION_COUNT: u64 = 0;

/// Initialize the scheduler and spawn `novad` (PID 1) from an authenticated package.
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

        // K4-T: Authenticate novad package against root authority
        let pkg_res = crate::trust::verify_and_authenticate_package(
            &crate::test_root_key::TEST_ROOT_PUBLIC_KEY,
            crate::test_root_key::MANIFEST_SEQ1_JSON,
            &crate::test_root_key::MANIFEST_SEQ1_SIG,
            &crate::test_root_key::NOVAD_PAYLOAD,
        );

        match pkg_res {
            Ok(pkg) => {
                match proc::spawn_verified_user(&pkg, &crate::test_root_key::NOVAD_PAYLOAD) {
                    Ok(pid) => {
                        assert!(pid == PID_NOVAD, "novad must receive PID 1");
                        SCHEDULER_INITIALIZED = true;
                        sprintln!("sched:       scheduler initialized; PID 1 (novad) spawned from verified package in ring 3");
                    }
                    Err(e) => {
                        sprintln!("sched:       REFUSED to spawn novad: {}", e);
                    }
                }
            }
            Err(e) => {
                sprintln!(
                    "sched:       REFUSED to spawn novad: package authentication failed: {:?}",
                    e
                );
            }
        }
    }
}

pub fn set_preemption_active(active: bool) {
    unsafe {
        PREEMPTION_ACTIVE = active;
    }
}

pub fn preemption_count() -> u64 {
    unsafe { PREEMPTION_COUNT }
}

/// APIC Timer ISR callback for preemptive scheduling.
pub unsafe fn preempt_timer_tick(saved_rsp: u64) -> u64 {
    if !PREEMPTION_ACTIVE {
        return saved_rsp;
    }

    let cur = proc::current_pid();
    if cur == PID_KERNEL {
        return saved_rsp;
    }

    // Save current process stack pointer and mark ready
    if let Some(p) = proc::get_process_mut(cur) {
        if p.state == ProcessState::Running {
            p.kstack_cur_rsp = saved_rsp;
            p.state = ProcessState::Ready;
        }
    }

    // Find next ready process in round-robin order
    let mut next = None;
    for i in 1..MAX_PROCS {
        let cand = (cur + i) % MAX_PROCS;
        if cand == PID_KERNEL {
            continue;
        }
        if let Some(p) = proc::get_process(cand) {
            if p.state == ProcessState::Ready {
                next = Some(cand);
                break;
            }
        }
    }

    let next_pid = match next {
        Some(pid) => pid,
        None => {
            if let Some(p) = proc::get_process_mut(cur) {
                p.state = ProcessState::Running;
            }
            return saved_rsp;
        }
    };

    PREEMPTION_COUNT += 1;
    let next_proc = proc::get_process_mut(next_pid).expect("next proc");
    next_proc.state = ProcessState::Running;
    proc::set_current_pid(next_pid);

    // Update TSS.rsp0 and CR3 to next process
    crate::gdt::set_tss_rsp0(next_proc.kstack_top);
    proc::switch_to(next_pid);

    next_proc.kstack_cur_rsp
}

/// Run preemption verification test (K4-T):
/// Spawns an unyielding Ring 3 spinner and a worker process.
/// Proves that the APIC timer preempts the spinner and executes the worker.
pub fn run_preemption_test() -> (u64, proc::ProcessExitReason) {
    unsafe {
        // Spawn PID 2: spinner (infinite loop in Ring 3, never calls yield)
        let spinner_caps = CapSet::new();
        let pid_spin = proc::spawn_user("spinner", spinner_caps, proc::spin_user_code_slice())
            .expect("spawn spinner");

        // Spawn PID 3: worker (runs in Ring 3, calls sys_exit(42))
        let worker_caps = CapSet::from_manifest(&[("fs:read", true)]).expect("caps");
        let pid_worker = proc::spawn_user("worker", worker_caps, proc::worker_user_code_slice())
            .expect("spawn worker");

        PREEMPTION_COUNT = 0;
        PREEMPTION_ACTIVE = true;

        // Arm APIC timer with countdown (10,000 ticks)
        crate::apic::start_timer(10_000);

        // Run spinner in Ring 3. Spinner spins forever until preempted!
        let _ = proc::run_user_process(pid_spin);

        // Worker finished and exited back to kernel
        crate::apic::stop_timer();
        PREEMPTION_ACTIVE = false;

        let worker_exit = proc::get_process(pid_worker)
            .map(|p| p.exit_reason)
            .unwrap_or(proc::ProcessExitReason::None);

        if let Some(p) = proc::get_process_mut(pid_spin) {
            p.state = proc::ProcessState::Exited;
        }

        (PREEMPTION_COUNT, worker_exit)
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
