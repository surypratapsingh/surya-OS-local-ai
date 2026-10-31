//! The model file (intent/README.md, "File format"): magic, eight u32 shape
//! fields, a u32 parameter count, then that many little-endian f32 values.

use crate::model::{Config, Layout, Model};
use std::path::Path;

pub const MAGIC: &[u8; 8] = b"NOVAINT1";

fn header(cfg: &Config, count: usize) -> Vec<u8> {
    let mut out = MAGIC.to_vec();
    for field in [cfg.buckets, cfg.d, cfg.heads, cfg.ff, cfg.layers, cfg.max_len, cfg.intents, cfg.tags, count] {
        out.extend_from_slice(&(field as u32).to_le_bytes());
    }
    out
}

/// The bytes of the file: f64 parameters rounded to the nearest f32.
pub fn to_bytes(model: &Model) -> Vec<u8> {
    let mut out = header(&model.cfg, model.params.len());
    for &p in &model.params {
        out.extend_from_slice(&(p as f32).to_le_bytes());
    }
    out
}

pub fn save(model: &Model, path: &Path) -> std::io::Result<()> {
    std::fs::write(path, to_bytes(model))
}

pub fn from_bytes(bytes: &[u8]) -> Result<Model, String> {
    if bytes.len() < 8 + 9 * 4 || &bytes[..8] != MAGIC {
        return Err("not a NOVAINT1 model file".into());
    }
    let field = |i: usize| u32::from_le_bytes(bytes[8 + 4 * i..12 + 4 * i].try_into().unwrap()) as usize;
    let cfg = Config {
        buckets: field(0),
        d: field(1),
        heads: field(2),
        ff: field(3),
        layers: field(4),
        max_len: field(5),
        intents: field(6),
        tags: field(7),
    };
    cfg.validate()?;
    let layout = Layout::new(&cfg);
    let count = field(8);
    if count != layout.total {
        return Err(format!("file says {count} parameters; this shape needs {}", layout.total));
    }
    let body = &bytes[8 + 9 * 4..];
    if body.len() != count * 4 {
        return Err(format!("expected {} bytes of parameters, found {}", count * 4, body.len()));
    }
    let params: Vec<f64> = body
        .chunks_exact(4)
        .map(|b| f64::from(f32::from_le_bytes(b.try_into().unwrap())))
        .collect();
    if let Some(i) = params.iter().position(|v| !v.is_finite()) {
        return Err(format!("parameter {i} is not finite"));
    }
    Ok(Model { cfg, layout, params })
}

pub fn load(path: &Path) -> Result<Model, String> {
    let bytes = std::fs::read(path).map_err(|e| format!("{}: {e}", path.display()))?;
    from_bytes(&bytes)
}
