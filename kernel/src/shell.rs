//! The K2 shell: line editing on the console, a handful of commands, and the
//! self-test exit path used by CI. Commands: help, clear, mem, ver, reboot,
//! halt, novatest.

use crate::console::Console;
use crate::keyboard::Keyboard;
use crate::limine;
use crate::ports::outb;
use crate::qemu_exit;

const LINE_MAX: usize = 96;

pub fn run_headless(reason: &str) -> ! {
    sprintln!("NOVA: {} — continuing serial-only; halting.", reason);
    loop {
        unsafe {
            core::arch::asm!("cli; hlt", options(nomem, nostack, preserves_flags));
        }
    }
}

/// The shell never returns. Polling loop spins (no interrupts configured yet).
pub fn run(kb: &mut Keyboard, mut con: Console, autotest: bool) -> ! {
    con.puts("NOVA Nucleus K3 console. Type `help`.\n");
    let mut line = [0u8; LINE_MAX];
    let mut len = 0usize;
    print_prompt(&mut con);

    if autotest {
        // CI mode: the boot itself is the test; exit cleanly without input.
        sprintln!("NOVA_SELFTEST_OK");
        qemu_exit::success();
    }

    loop {
        match kb.poll() {
            Some(ev) => {
                if let Some(ch) = ev.ch {
                    match ch {
                        b'\r' => {
                            con.put(b'\n');
                            let mut cmd = [0u8; LINE_MAX];
                            cmd[..len].copy_from_slice(&line[..len]);
                            dispatch(&cmd[..len], &mut con);
                            len = 0;
                            print_prompt(&mut con);
                        }
                        0x08 => {
                            if len > 0 {
                                len -= 1;
                                con.put(0x08);
                            }
                        }
                        0x1B => {
                            // Cancel the current line.
                            if len > 0 {
                                con.puts("^C\n");
                                len = 0;
                                print_prompt(&mut con);
                            }
                        }
                        b'\t' => {}
                        0x20..=0x7E if len < LINE_MAX => {
                            line[len] = ch;
                            len += 1;
                            con.put(ch);
                        }
                        _ => {}
                    }
                }
            }
            None => core::hint::spin_loop(),
        }
    }
}

fn print_prompt(con: &mut Console) {
    con.puts("nova> ");
}

fn dispatch(cmd: &[u8], con: &mut Console) {
    let cmd_str = core::str::from_utf8(cmd).unwrap_or("");
    let cmd_trimmed = cmd_str.trim();
    let (name, arg) = match cmd_trimmed.split_once(' ') {
        Some((n, a)) => (n, a.trim()),
        None => (cmd_trimmed, ""),
    };
    match name {
        "" => {}
        "help" => {
            con.puts("commands: help clear mem ver reboot halt\n");
            con.puts("  mem     physical memory map summary\n");
            con.puts("  novatest also accepted under CI\n");
        }
        "clear" => con.clear(),
        "ver" => {
            con.puts(concat!(
                "nucleus ",
                env!("CARGO_PKG_VERSION"),
                " (K3a) x86-64, Limine boot protocol rev 3\n"
            ));
        }
        "mem" => {
            let mut usable: u64 = 0;
            let mut total: u64 = 0;
            let mut entries: u32 = 0;
            limine::for_each_memmap_entry(|e| {
                entries += 1;
                total += e.length;
                if e.ty == limine::MEMMAP_USABLE {
                    usable += e.length;
                }
            });
            con_puts_u64(con, "entries: ", entries as u64);
            con.put(b'\n');
            con_puts_u64(con, "usable:  ", usable / 1024);
            con.puts(" KiB\n");
            con_puts_u64(con, "total:   ", total / 1024);
            con.puts(" KiB\n");
            if !arg.is_empty() && arg == "all" {
                limine::for_each_memmap_entry(|e| {
                    con.puts("  ");
                    print_hex(con, e.base);
                    con.puts(" +");
                    print_hex(con, e.length);
                    con.puts(" ");
                    con.puts(limine::memmap_type_name(e.ty));
                    con.put(b'\n');
                });
            }
        }
        "reboot" => {
            con.puts("rebooting...\n");
            sprintln!("NOVA: reboot requested");
            // 8042 pulse reset.
            outb(0x64, 0xFE);
            loop {
                unsafe {
                    core::arch::asm!("cli; hlt", options(nomem, nostack, preserves_flags));
                }
            }
        }
        "halt" => {
            con.puts("halted.\n");
            sprintln!("NOVA_HALT");
            loop {
                unsafe {
                    core::arch::asm!("cli; hlt", options(nomem, nostack, preserves_flags));
                }
            }
        }
        "novatest" => {
            sprintln!("NOVA_SELFTEST_OK");
            qemu_exit::success();
        }
        other => {
            con.puts("unknown command: ");
            con.puts(other);
            con.puts(" (try help)\n");
        }
    }
}

fn con_puts_u64(con: &mut Console, prefix: &str, v: u64) {
    con.puts(prefix);
    let mut buf = [0u8; 20];
    let mut i = buf.len();
    let mut v = v;
    if v == 0 {
        con.puts("0");
        return;
    }
    while v > 0 {
        i -= 1;
        buf[i] = b'0' + (v % 10) as u8;
        v /= 10;
    }
    con.puts(core::str::from_utf8(&buf[i..]).unwrap_or("?"));
}

fn print_hex(con: &mut Console, v: u64) {
    con.puts("0x");
    let mut started = false;
    let mut shift = 60;
    loop {
        let nib = (v >> shift) & 0xF;
        if nib != 0 || shift == 0 || started {
            started = true;
            con.put(if nib < 10 {
                b'0' + nib as u8
            } else {
                b'a' + (nib - 10) as u8
            });
        }
        if shift == 0 {
            break;
        }
        shift -= 4;
    }
}
