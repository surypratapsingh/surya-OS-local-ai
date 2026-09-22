//! Nucleus — the from-scratch kernel of the NOVA local-first AI OS.
//!
//! K2: boot via Limine, serial + framebuffer + console, CPU exception gates,
//! PS/2 keyboard, and an interactive shell. `novatest` on the kernel command
//! line (or typed at the prompt) exits cleanly under QEMU for CI.

#![no_std]
#![no_main]
#![forbid(non_ascii_idents)]
// K3+ API surface (ports, extra glyphs, exit helpers) is intentionally ahead
// of its use sites; dead_code is re-enabled per-module as things come online.
#![allow(dead_code)]

#[macro_use]
mod serial;
mod console;
mod draw;
mod font;
mod framebuffer;
mod idt;
mod keyboard;
mod limine;
mod panic;
mod ports;
mod qemu_exit;
mod shell;

// Entry point: Limine jumps here with the boot-info pointer in `rdi`
// (System V first argument). Global asm keeps us on the stable toolchain.
core::arch::global_asm!(
    ".globl _start",
    "_start:",
    "and rsp, -16",
    "call {kmain}",
    "ud2",
    kmain = sym kmain
);

#[no_mangle]
extern "sysv64" fn kmain(_boot_info: *const u64) -> ! {
    serial::init();

    sprintln!();
    sprintln!("NOVA/Nucleus K2 boot report");
    sprintln!("==========================");
    sprintln!(
        "kernel:      nucleus {} ({})",
        env!("CARGO_PKG_VERSION"),
        build_profile()
    );
    sprintln!("arch:        x86-64, Limine boot protocol base revision 3");

    if limine::base_revision_supported() {
        sprintln!("base rev:    accepted by bootloader");
    } else {
        sprintln!("base rev:    NOT accepted (older bootloader?)");
    }

    if let Some((phys, virt)) = limine::executable_address() {
        sprintln!("loaded at:   phys={:#x} virt={:#x}", phys, virt);
    }
    if let Some(cmd) = limine::cmdline() {
        sprintln!("cmdline:     {}", cmd);
    }
    let autotest = limine::cmdline()
        .map(|c| c.split_whitespace().any(|t| t == "novatest"))
        .unwrap_or(false);

    // Memory map summary (usable / total).
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
    sprintln!(
        "memory:      {} entries, usable {} KiB / {} KiB",
        entries,
        usable / 1024,
        total / 1024
    );

    // CPU exception gates go up before we touch anything adventurous.
    idt::init();
    sprintln!("idt:         exception gates armed");

    // Framebuffer.
    let fb_info = match limine::first_framebuffer() {
        Some(info) => info,
        None => shell::run_headless("no framebuffer"),
    };
    sprintln!(
        "framebuffer: {}x{} @ {}bpp, pitch={}, model={}",
        fb_info.width,
        fb_info.height,
        fb_info.bpp,
        fb_info.pitch,
        if fb_info.memory_model == 1 {
            "RGB"
        } else {
            "other"
        }
    );

    let fb = match framebuffer::Framebuffer::from_info(&fb_info) {
        Some(fb) => fb,
        None => shell::run_headless("unsupported framebuffer mode"),
    };
    panic::set_framebuffer(fb);

    // Structural sanity check on the embedded atlas: not a comparison against
    // its own source (that can never fail), but invariants that catch truncation
    // and bit rot. The byte-exact oracle is tests/test_font_ref.py at build time.
    let font_problems = font::verify_against_reference();
    if font_problems == 0 {
        sprintln!(
            "font:        embedded 8x8 atlas structurally valid ({} glyphs)",
            font::GLYPH_COUNT
        );
    } else {
        sprintln!(
            "font:        {} GLYPH TABLE PROBLEMS - regenerate via tools/gen-font.py",
            font_problems
        );
    }

    // Boot art fills the top half; the console takes over below it.
    fb.clear(&framebuffer::BG_COLOR);
    draw::draw_logo(&fb, &font::FONT);
    draw::draw_boot_line(&fb, &font::FONT, "NOVA Nucleus K2 - kernel is alive");
    draw::draw_version_tag(
        &fb,
        &font::FONT,
        concat!("nucleus ", env!("CARGO_PKG_VERSION"), " (K2)"),
    );

    // Reserve the art region for the console's scroll region.
    let art_rows = (fb.height / 2 + 64) / console::CELL_H + 1;
    let mut con = console::Console::new(fb, &font::FONT, art_rows as usize);
    con.draw_status("NOVA Nucleus K2 - ready");

    // Memory map detail comes last so the serial log reads top-down.
    limine::for_each_memmap_entry(|e| {
        sprintln!(
            "  memmap:    base={:#018x} len={:#x} {}",
            e.base,
            e.length,
            limine::memmap_type_name(e.ty)
        );
    });

    sprintln!("NOVA_BOOT_OK");

    // Keyboard last: once the shell runs, the console is live.
    let mut kb = keyboard::Keyboard::new();
    kb.raw_echo = limine::cmdline()
        .map(|c| c.split_whitespace().any(|t| t == "novaraw"))
        .unwrap_or(false);
    kb.init();
    sprintln!("keyboard:    PS/2 poller ready");

    shell::run(&mut kb, con, autotest);
}

fn build_profile() -> &'static str {
    if cfg!(debug_assertions) {
        "debug"
    } else {
        "release"
    }
}
