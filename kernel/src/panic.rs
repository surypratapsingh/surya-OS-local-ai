//! Panic + fault handling. K1 keeps this honest and simple: report on serial
//! first (always available), then mirror the report on the framebuffer if one
//! was captured, then halt with interrupts disabled.

use crate::framebuffer;

static mut LAST_FB: Option<framebuffer::Framebuffer<'static>> = None;

/// Record the framebuffer so panics can paint the screen. `Framebuffer` is
/// `Copy`, so `main` keeps its own copy after handing one to us.
pub fn set_framebuffer(fb: framebuffer::Framebuffer<'static>) {
    unsafe {
        core::ptr::addr_of_mut!(LAST_FB).write(Some(fb));
    }
}

/// Run `f` with the captured framebuffer, if any (raw-pointer access keeps us
/// clear of `static_mut_refs` on all toolchains).
fn with_fb(f: impl FnOnce(&framebuffer::Framebuffer<'static>)) {
    unsafe {
        let slot = core::ptr::addr_of_mut!(LAST_FB);
        if let Some(fb) = (*slot).as_ref() {
            f(fb);
        }
    }
}

#[panic_handler]
fn panic(info: &core::panic::PanicInfo) -> ! {
    sprintln!("\nNOVA_PANIC: {}", info);
    sprintln!("NOVA: halted.");

    with_fb(|fb| {
        let red = framebuffer::Rgb::new(0x8B, 0x2F, 0x2F);
        fb.clear(&framebuffer::BG_COLOR);
        fb.draw_text_centered("NUCLEUS FAULT", fb.height / 3, &red, &crate::font::FONT);
        fb.draw_text_centered(
            "see serial log",
            fb.height / 3 + 24,
            &red,
            &crate::font::FONT,
        );
    });

    loop {
        #[cfg(target_arch = "x86_64")]
        unsafe {
            core::arch::asm!("cli; hlt", options(nomem, nostack, preserves_flags));
        }
    }
}
