//! Text console: an 8×8 glyph grid over the framebuffer with a scroll region
//! below the boot art, plus a serial mirror of everything printed. State is
//! a plain byte grid, so scrolling and clearing are trivial repaints.

use crate::font::{FontAtlas, GLYPH_H};
use crate::framebuffer::{Framebuffer, Rgb, BG_COLOR, DIM_COLOR, FG_COLOR};

pub const CELL_W: u64 = 9; // 8px glyph + 1px spacing
pub const CELL_H: u64 = 9;

const MAX_COLS: usize = 256;
const MAX_ROWS: usize = 128;

static mut CELLS: [u8; MAX_COLS * MAX_ROWS] = [0; MAX_COLS * MAX_ROWS];

pub struct Console<'a> {
    fb: Framebuffer<'a>,
    atlas: &'a FontAtlas,
    cols: usize,
    /// Console rows (excludes the logo region above and the status row below).
    rows: usize,
    /// Top row of the console region, in text rows from screen top.
    top: usize,
    cx: usize,
    cy: usize,
    cells: &'static mut [u8],
    fg: Rgb,
    bg: Rgb,
}

impl<'a> Console<'a> {
    /// `top_rows` text rows are left to the boot art; the bottom row is the
    /// status line owned by the shell.
    pub fn new(fb: Framebuffer<'a>, atlas: &'a FontAtlas, top_rows: usize) -> Console<'a> {
        let cols_total = (fb.width / CELL_W) as usize;
        let rows_total = (fb.height / CELL_H) as usize;
        let cols = cols_total.min(MAX_COLS);
        let top = top_rows.min(rows_total.saturating_sub(3));
        let rows = rows_total - top - 1;
        let cells = unsafe {
            let base = core::ptr::addr_of_mut!(CELLS);
            let all: &'static mut [u8; MAX_COLS * MAX_ROWS] = &mut *base;
            &mut all[..cols * rows]
        };
        Console {
            fb,
            atlas,
            cols,
            rows,
            top,
            cx: 0,
            cy: 0,
            cells,
            fg: FG_COLOR,
            bg: BG_COLOR,
        }
    }

    fn cell_x(&self, col: usize) -> u64 {
        col as u64 * CELL_W
    }

    fn row_y(&self, row: usize) -> u64 {
        (self.top + row) as u64 * CELL_H
    }

    fn draw_cell(&self, col: usize, row: usize) {
        let b = self.cells[row * self.cols + col];
        self.fb
            .fill_rect(self.cell_x(col), self.row_y(row), CELL_W, CELL_H, &self.bg);
        if b != 0 {
            self.fb.draw_glyph(
                b as char,
                self.cell_x(col),
                self.row_y(row),
                &self.fg,
                self.atlas,
            );
        }
    }

    fn set_cell(&mut self, col: usize, row: usize, b: u8) {
        self.cells[row * self.cols + col] = b;
        self.draw_cell(col, row);
    }

    fn erase_cell(&mut self, col: usize, row: usize) {
        self.set_cell(col, row, 0);
    }

    /// Scroll the region up one row: move the grid, zero the last row,
    /// repaint. `cy` stays on the (new) last row.
    fn scroll(&mut self) {
        self.cells.copy_within(self.cols..self.rows * self.cols, 0);
        let last = (self.rows - 1) * self.cols;
        self.cells[last..last + self.cols].fill(0);
        for r in 0..self.rows {
            for c in 0..self.cols {
                self.draw_cell(c, r);
            }
        }
        self.cy = self.rows - 1;
    }

    fn newline(&mut self) {
        self.cx = 0;
        if self.cy + 1 >= self.rows {
            self.scroll();
        } else {
            self.cy += 1;
        }
    }

    /// Write one byte to the console and mirror it to serial.
    pub fn put(&mut self, b: u8) {
        match b {
            b'\n' => self.newline(),
            b'\r' => self.cx = 0,
            0x08 => {
                if self.cx > 0 {
                    self.cx -= 1;
                    self.erase_cell(self.cx, self.cy);
                }
            }
            b'\t' => {
                let next = ((self.cx + 4) / 4) * 4;
                while self.cx < next && self.cx < self.cols - 1 {
                    self.put(b' ');
                }
            }
            0x20..=0x7E => {
                self.set_cell(self.cx, self.cy, b);
                self.cx += 1;
                if self.cx >= self.cols {
                    self.newline();
                }
            }
            _ => self.put(b'?'),
        }
        mirror(b);
    }

    pub fn puts(&mut self, s: &str) {
        for b in s.bytes() {
            self.put(b);
        }
    }

    /// Erase the whole console region (the boot art above stays).
    pub fn clear(&mut self) {
        self.cells.fill(0);
        for r in 0..self.rows {
            for c in 0..self.cols {
                self.draw_cell(c, r);
            }
        }
        self.cx = 0;
        self.cy = 0;
    }

    /// Status line along the very bottom of the screen.
    pub fn draw_status(&mut self, text: &str) {
        let row = (self.top + self.rows) as u64;
        let y = row * CELL_H;
        self.fb
            .fill_rect(0, y, self.fb.width, CELL_H + 1, &DIM_COLOR);
        self.fb.draw_text(text, 8, y, &BG_COLOR, self.atlas);
    }

    pub fn glyph_height(&self) -> u64 {
        GLYPH_H as u64
    }
}

/// Send one byte to serial with CRLF mapping for `\n`.
fn mirror(b: u8) {
    if b == b'\n' {
        crate::serial::write_str("\r\n");
    } else if (0x20..=0x7E).contains(&b) {
        crate::serial::write_str(core::str::from_utf8(&[b]).unwrap_or("?"));
    }
}
