//! Direct framebuffer drawing: pixels, fills, 8×8 font text.
//!
//! Supports 32-, 24- and 16-bpp linear framebuffers with RGB channel order
//! from the Limine masks (BGR handled via the same masks); anything else is
//! rejected at construction rather than drawing garbage. No allocations, no
//! floating point.

use crate::font::FontAtlas;

/// A borrowed, ready-to-draw framebuffer. Copyable by design: it is just a
/// descriptor (raw pointer + geometry) into memory owned by the bootloader.
#[derive(Clone, Copy)]
pub struct Framebuffer<'a> {
    pub address: *mut u8,
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
    _marker: core::marker::PhantomData<&'a ()>,
}

/// RGB triple used by the K1 art.
#[derive(Clone, Copy)]
pub struct Rgb {
    pub r: u8,
    pub g: u8,
    pub b: u8,
}

impl Rgb {
    pub const fn new(r: u8, g: u8, b: u8) -> Rgb {
        Rgb { r, g, b }
    }
}

/// Palette for the boot screen.
pub const BG_COLOR: Rgb = Rgb::new(0x0A, 0x0E, 0x14);
pub const FG_COLOR: Rgb = Rgb::new(0xE6, 0xED, 0xF3);
pub const ORB_COLOR: Rgb = Rgb::new(0x4C, 0xCF, 0xAF);
pub const DIM_COLOR: Rgb = Rgb::new(0x39, 0x4B, 0x5C);

impl<'a> Framebuffer<'a> {
    /// Build from a Limine framebuffer info struct. Returns None for modes we
    /// cannot draw correctly (non-RGB memory model, or fewer than 16 bpp).
    pub fn from_info(info: &crate::limine::FramebufferInfo) -> Option<Framebuffer<'static>> {
        if info.memory_model != 1 || info.bpp < 16 || info.bpp > 32 {
            return None;
        }
        Some(Framebuffer {
            address: info.address as *mut u8,
            width: info.width,
            height: info.height,
            pitch: info.pitch,
            bpp: info.bpp,
            memory_model: info.memory_model,
            red_mask_size: info.red_mask_size,
            red_mask_shift: info.red_mask_shift,
            green_mask_size: info.green_mask_size,
            green_mask_shift: info.green_mask_shift,
            blue_mask_size: info.blue_mask_size,
            blue_mask_shift: info.blue_mask_shift,
            _marker: core::marker::PhantomData,
        })
    }

    fn bytes_per_pixel(&self) -> u64 {
        (self.bpp as u64).div_ceil(8)
    }

    /// Scale an 8-bit channel into `size` bits at `shift`.
    fn channel(v: u8, size: u8, shift: u8) -> u32 {
        if size == 0 || size > 8 {
            return 0;
        }
        let max = (1u32 << size) - 1;
        (((v as u32 * max) + 127) / 255) << shift
    }

    /// Pack an RGB triple into the framebuffer's pixel format (masks honored
    /// for any depth 16/24/32, RGB or BGR).
    fn pack(&self, c: &Rgb) -> u32 {
        Self::channel(c.r, self.red_mask_size, self.red_mask_shift)
            | Self::channel(c.g, self.green_mask_size, self.green_mask_shift)
            | Self::channel(c.b, self.blue_mask_size, self.blue_mask_shift)
    }

    /// Write one pixel (bounds-checked; no-op outside the screen). The write
    /// width matches the mode's bytes-per-pixel exactly.
    pub fn pixel(&self, x: u64, y: u64, c: &Rgb) {
        if x >= self.width || y >= self.height {
            return;
        }
        let value = self.pack(c);
        let base = self.address as usize + (y * self.pitch + x * self.bytes_per_pixel()) as usize;
        unsafe {
            match self.bytes_per_pixel() {
                4 => core::ptr::write_volatile(base as *mut u32, value),
                3 => {
                    let p = base as *mut u8;
                    core::ptr::write_volatile(p, value as u8);
                    core::ptr::write_volatile(p.add(1), (value >> 8) as u8);
                    core::ptr::write_volatile(p.add(2), (value >> 16) as u8);
                }
                2 => core::ptr::write_volatile(base as *mut u16, value as u16),
                _ => {}
            }
        }
    }

    /// Fill a rectangle.
    pub fn fill_rect(&self, x: u64, y: u64, w: u64, h: u64, c: &Rgb) {
        let mut row = y;
        while row < y + h {
            let mut col = x;
            while col < x + w {
                self.pixel(col, row, c);
                col += 1;
            }
            row += 1;
        }
    }

    /// Fill the entire screen.
    pub fn clear(&self, c: &Rgb) {
        self.fill_rect(0, 0, self.width, self.height, c);
    }

    /// Draw one 8×8 glyph; returns the next x position.
    pub fn draw_glyph(&self, ch: char, x: u64, y: u64, fg: &Rgb, atlas: &FontAtlas) -> u64 {
        let code = if (ch as u32) <= 0x7F { ch as u8 } else { b'?' };
        if let Some(rows) = atlas.glyph(code) {
            for (row, bits) in rows.iter().enumerate() {
                for col in 0..8 {
                    if bits & (1 << col) != 0 {
                        self.pixel(x + col as u64, y + row as u64, fg);
                    }
                }
            }
        }
        x + 8
    }

    /// Draw a string with 1px spacing; returns the next x position.
    pub fn draw_text(&self, text: &str, x: u64, y: u64, fg: &Rgb, atlas: &FontAtlas) -> u64 {
        let mut cx = x;
        for ch in text.chars() {
            if ch == '\n' {
                break;
            }
            cx = self.draw_glyph(ch, cx, y, fg, atlas) + 1;
        }
        cx
    }

    /// Measure a string's width with the embedded font.
    pub fn text_width(&self, text: &str) -> u64 {
        let chars = text.chars().count() as u64;
        chars * 9 - 1
    }

    /// Centered text helper; returns the x it started at.
    pub fn draw_text_centered(&self, text: &str, y: u64, fg: &Rgb, atlas: &FontAtlas) -> u64 {
        let w = self.text_width(text);
        let x = (self.width.saturating_sub(w)) / 2;
        self.draw_text(text, x, y, fg, atlas);
        x
    }

    /// Filled circle (midpoint algorithm), used for the status orb.
    pub fn fill_circle(&self, cx: u64, cy: u64, radius: u64, c: &Rgb) {
        if radius == 0 {
            self.pixel(cx, cy, c);
            return;
        }
        let (mut x, mut y, mut d) = (0i64, radius as i64, 1 - radius as i64);
        while x <= y {
            for (dx, dy) in [
                (x, y), (y, x), (-x, y), (-y, x), (x, -y), (y, -x), (-x, -y), (-y, -x),
            ] {
                let px = cx as i64 + dx;
                let py = cy as i64 + dy;
                if px >= 0 && py >= 0 {
                    self.pixel(px as u64, py as u64, c);
                }
            }
            if d < 0 {
                d += 2 * x + 3;
            } else {
                d += 2 * (x - y) + 5;
                y -= 1;
            }
            x += 1;
        }
    }
}
