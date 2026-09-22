//! K1 boot screen: the NOVA wordmark and status orb.
//!
//! The logo is drawn with the 8×8 font at 6× scale (48px tall), with a filled
//! orb to its left — the same orb motif the future Eyes shell will use.

use crate::font::{FontAtlas, GLYPH_H};
use crate::framebuffer::{Framebuffer, Rgb, BG_COLOR, DIM_COLOR, FG_COLOR, ORB_COLOR};

/// Scale factor for the wordmark glyphs.
const SCALE: u64 = 6;

/// Draw the NOVA logo: orb + scaled wordmark, centered horizontally.
pub fn draw_logo(fb: &Framebuffer, atlas: &FontAtlas) {
    let text = "NOVA";
    let glyph_px = 8 * SCALE;
    let text_w = text.chars().count() as u64 * (glyph_px + SCALE) - SCALE;
    let orb_r = glyph_px / 2 + SCALE;
    let total_w = orb_r * 2 + SCALE * 4 + text_w;
    let x0 = fb.width.saturating_sub(total_w) / 2;
    let cy = fb.height.saturating_sub(3 * SCALE) / 2 + 3 * SCALE;

    // Status orb with a subtle ring.
    let cx = x0 + orb_r;
    fb.fill_circle(cx, cy, orb_r, &ORB_COLOR);
    fb.fill_circle(cx, cy, orb_r / 2, &BG_COLOR);
    fb.fill_circle(cx, cy, orb_r / 4, &ORB_COLOR);

    // Wordmark via scaled glyphs.
    let mut x = x0 + orb_r * 2 + SCALE * 4;
    for ch in text.chars() {
        draw_glyph_scaled(fb, atlas, ch, x, cy - glyph_px / 2, &FG_COLOR, SCALE);
        x += glyph_px + SCALE;
    }
}

/// Blit one glyph scaled by `scale` in both axes.
fn draw_glyph_scaled(fb: &Framebuffer, atlas: &FontAtlas, ch: char, x: u64, y: u64, fg: &Rgb, scale: u64) {
    let code = if (ch as u32) <= 0x7F { ch as u8 } else { b'?' };
    if let Some(rows) = atlas.glyph(code) {
        for (row, bits) in rows.iter().enumerate() {
            for col in 0..8 {
                if bits & (1 << col) != 0 {
                    fb.fill_rect(
                        x + col as u64 * scale,
                        y + row as u64 * scale,
                        scale,
                        scale,
                        fg,
                    );
                }
            }
        }
    }
}

/// Footer line drawn under the logo.
pub fn draw_boot_line(fb: &Framebuffer, atlas: &FontAtlas, text: &str) {
    let y = fb.height / 2 + 4 * (GLYPH_H as u64 + 4);
    fb.draw_text_centered(text, y, &DIM_COLOR, atlas);
}

/// Version tag drawn in the corner.
pub fn draw_version_tag(fb: &Framebuffer, atlas: &FontAtlas, text: &str) {
    fb.draw_text(text, 8, fb.height - GLYPH_H as u64 - 8, &DIM_COLOR, atlas);
}
