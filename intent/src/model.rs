//! The encoder: parameter layout, forward pass and hand-written backward pass
//! (intent/README.md, "Model"). All arithmetic is f64; the shipped file is f32.

// Index loops mirror the formulas they implement; rewriting them as iterator
// chains could change the order of floating-point operations.
#![allow(clippy::needless_range_loop)]

use crate::math::{gelu, gelu_grad, softmax};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Config {
    pub buckets: usize,
    pub d: usize,
    pub heads: usize,
    pub ff: usize,
    pub layers: usize,
    pub max_len: usize,
    pub intents: usize,
    pub tags: usize,
}

impl Config {
    /// The shipped model's shape.
    pub const NOVA: Config = Config {
        buckets: 8192,
        d: 48,
        heads: 4,
        ff: 96,
        layers: 2,
        max_len: 64,
        intents: 14,
        tags: 11,
    };

    pub fn head_dim(&self) -> usize {
        self.d / self.heads
    }

    pub fn validate(&self) -> Result<(), String> {
        let fields = [
            self.buckets,
            self.d,
            self.heads,
            self.ff,
            self.layers,
            self.max_len,
            self.intents,
            self.tags,
        ];
        if fields.iter().any(|&f| f == 0 || f > 1 << 20) {
            return Err(format!("config field out of range: {self:?}"));
        }
        if !self.d.is_multiple_of(self.heads) {
            return Err(format!(
                "d {} is not a multiple of heads {}",
                self.d, self.heads
            ));
        }
        Ok(())
    }
}

#[derive(Clone, Debug)]
pub struct LayerOffsets {
    pub ln1_g: usize,
    pub ln1_b: usize,
    pub wqkv: usize,
    pub bqkv: usize,
    pub wo: usize,
    pub bo: usize,
    pub ln2_g: usize,
    pub ln2_b: usize,
    pub w1: usize,
    pub b1: usize,
    pub w2: usize,
    pub b2: usize,
}

/// Where each tensor starts in the flat parameter vector. The order is the
/// file order (intent/README.md, "Layout").
#[derive(Clone, Debug)]
pub struct Layout {
    pub emb: usize,
    pub pos: usize,
    pub layers: Vec<LayerOffsets>,
    pub lnf_g: usize,
    pub lnf_b: usize,
    pub wi: usize,
    pub bi: usize,
    pub ws: usize,
    pub bs: usize,
    pub total: usize,
    /// (offset, length) of the weight matrices that get weight decay.
    pub decayed: Vec<(usize, usize)>,
}

struct Cursor(usize);

impl Cursor {
    fn take(&mut self, n: usize) -> usize {
        let at = self.0;
        self.0 += n;
        at
    }
}

impl Layout {
    pub fn new(c: &Config) -> Layout {
        let d = c.d;
        let mut at = Cursor(0);
        let emb = at.take(c.buckets * d);
        let pos = at.take(c.max_len * d);
        let mut layers = Vec::new();
        let mut decayed = Vec::new();
        for _ in 0..c.layers {
            let l = LayerOffsets {
                ln1_g: at.take(d),
                ln1_b: at.take(d),
                wqkv: at.take(d * 3 * d),
                bqkv: at.take(3 * d),
                wo: at.take(d * d),
                bo: at.take(d),
                ln2_g: at.take(d),
                ln2_b: at.take(d),
                w1: at.take(d * c.ff),
                b1: at.take(c.ff),
                w2: at.take(c.ff * d),
                b2: at.take(d),
            };
            decayed.push((l.wqkv, d * 3 * d));
            decayed.push((l.wo, d * d));
            decayed.push((l.w1, d * c.ff));
            decayed.push((l.w2, c.ff * d));
            layers.push(l);
        }
        let lnf_g = at.take(d);
        let lnf_b = at.take(d);
        let wi = at.take(d * c.intents);
        let bi = at.take(c.intents);
        let ws = at.take(d * c.tags);
        let bs = at.take(c.tags);
        decayed.push((wi, d * c.intents));
        decayed.push((ws, d * c.tags));
        Layout {
            emb,
            pos,
            layers,
            lnf_g,
            lnf_b,
            wi,
            bi,
            ws,
            bs,
            total: at.0,
            decayed,
        }
    }
}

/// SplitMix64 (Steele, Lea and Flood 2014): a small, fixed, portable generator.
pub struct SplitMix64(pub u64);

impl SplitMix64 {
    pub fn next_u64(&mut self) -> u64 {
        self.0 = self.0.wrapping_add(0x9e37_79b9_7f4a_7c15);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xbf58_476d_1ce4_e5b9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94d0_49bb_1331_11eb);
        z ^ (z >> 31)
    }

    /// Uniform in [0, 1), 53 bits.
    pub fn unit(&mut self) -> f64 {
        (self.next_u64() >> 11) as f64 * (1.0 / (1u64 << 53) as f64)
    }

    /// Uniform in [0, n).
    pub fn below(&mut self, n: usize) -> usize {
        (self.unit() * n as f64) as usize
    }
}

pub struct Model {
    pub cfg: Config,
    pub layout: Layout,
    pub params: Vec<f64>,
}

const LN_EPS: f64 = 1e-5;

/// What the backward pass needs from one layer's forward pass.
struct LayerCache {
    a: Vec<f64>,
    xhat1: Vec<f64>,
    rstd1: Vec<f64>,
    qkv: Vec<f64>,
    probs: Vec<f64>,
    o: Vec<f64>,
    m: Vec<f64>,
    xhat2: Vec<f64>,
    rstd2: Vec<f64>,
    h1: Vec<f64>,
    g: Vec<f64>,
}

pub struct Cache {
    t: usize,
    layers: Vec<LayerCache>,
    xf: Vec<f64>,
    xhatf: Vec<f64>,
    rstdf: Vec<f64>,
    /// Intent probabilities.
    pub intent: Vec<f64>,
    /// Tag probabilities for positions 1..t, row-major.
    pub tags: Vec<f64>,
}

impl Model {
    /// Uniform initialisation (only +, *, so the same on every machine):
    /// a uniform on [-a, a] has standard deviation a / sqrt(3).
    pub fn init(cfg: Config, seed: u64) -> Model {
        let layout = Layout::new(&cfg);
        let mut params = vec![0.0; layout.total];
        let mut rng = SplitMix64(seed);
        let mut fill = |params: &mut Vec<f64>, at: usize, n: usize, std: f64| {
            let a = std * 3f64.sqrt();
            for p in &mut params[at..at + n] {
                *p = (rng.unit() * 2.0 - 1.0) * a;
            }
        };
        let d = cfg.d;
        fill(&mut params, layout.emb, cfg.buckets * d, 0.1);
        fill(&mut params, layout.pos, cfg.max_len * d, 0.02);
        for l in &layout.layers {
            fill(&mut params, l.wqkv, d * 3 * d, 0.02);
            fill(&mut params, l.wo, d * d, 0.02);
            fill(&mut params, l.w1, d * cfg.ff, 0.02);
            fill(&mut params, l.w2, cfg.ff * d, 0.02);
            for p in &mut params[l.ln1_g..l.ln1_g + d] {
                *p = 1.0;
            }
            for p in &mut params[l.ln2_g..l.ln2_g + d] {
                *p = 1.0;
            }
        }
        for p in &mut params[layout.lnf_g..layout.lnf_g + d] {
            *p = 1.0;
        }
        fill(&mut params, layout.wi, d * cfg.intents, 0.02);
        fill(&mut params, layout.ws, d * cfg.tags, 0.02);
        Model {
            cfg,
            layout,
            params,
        }
    }

    pub fn forward(&self, input: &[Vec<u32>]) -> Cache {
        let c = &self.cfg;
        let p = &self.params;
        let (d, t) = (c.d, input.len());
        assert!(
            t >= 1 && t <= c.max_len,
            "input length {t} outside 1..={}",
            c.max_len
        );

        let mut x = vec![0.0; t * d];
        for (pos, features) in input.iter().enumerate() {
            let row = &mut x[pos * d..(pos + 1) * d];
            for &f in features {
                let e = self.layout.emb + f as usize * d;
                for j in 0..d {
                    row[j] += p[e + j];
                }
            }
            let n = features.len() as f64;
            let at = self.layout.pos + pos * d;
            for j in 0..d {
                row[j] = row[j] / n + p[at + j];
            }
        }

        let mut layers = Vec::with_capacity(c.layers);
        for l in &self.layout.layers {
            let (a, xhat1, rstd1) = layernorm(p, l.ln1_g, l.ln1_b, &x, t, d);
            let qkv = linear(p, l.wqkv, l.bqkv, &a, t, d, 3 * d);
            let (probs, o) = attention(&qkv, t, d, c.heads);
            let attn = linear(p, l.wo, l.bo, &o, t, d, d);
            for i in 0..t * d {
                x[i] += attn[i];
            }
            let (m, xhat2, rstd2) = layernorm(p, l.ln2_g, l.ln2_b, &x, t, d);
            let h1 = linear(p, l.w1, l.b1, &m, t, d, c.ff);
            let g: Vec<f64> = h1.iter().map(|&v| gelu(v)).collect();
            let f = linear(p, l.w2, l.b2, &g, t, c.ff, d);
            for i in 0..t * d {
                x[i] += f[i];
            }
            layers.push(LayerCache {
                a,
                xhat1,
                rstd1,
                qkv,
                probs,
                o,
                m,
                xhat2,
                rstd2,
                h1,
                g,
            });
        }

        let (xf, xhatf, rstdf) = layernorm(p, self.layout.lnf_g, self.layout.lnf_b, &x, t, d);
        let mut intent = linear(p, self.layout.wi, self.layout.bi, &xf[..d], 1, d, c.intents);
        softmax(&mut intent);
        let mut tags = linear(
            p,
            self.layout.ws,
            self.layout.bs,
            &xf[d..],
            t - 1,
            d,
            c.tags,
        );
        for row in tags.chunks_mut(c.tags) {
            softmax(row);
        }
        Cache {
            t,
            layers,
            xf,
            xhatf,
            rstdf,
            intent,
            tags,
        }
    }

    /// Loss of one example: cross-entropy of the intent plus the mean
    /// cross-entropy of the tags at positions 1..t.
    pub fn loss(&self, cache: &Cache, intent: usize, tags: &[usize]) -> f64 {
        let mut loss = -cache.intent[intent].ln();
        if cache.t > 1 {
            let n = (cache.t - 1) as f64;
            for (row, &tag) in cache.tags.chunks(self.cfg.tags).zip(tags) {
                loss -= row[tag].ln() / n;
            }
        }
        loss
    }

    /// Add `scale` times the gradient of `loss()` to `grads`.
    pub fn backward(
        &self,
        input: &[Vec<u32>],
        cache: &Cache,
        intent: usize,
        tags: &[usize],
        grads: &mut [f64],
        scale: f64,
    ) {
        let c = &self.cfg;
        let p = &self.params;
        let lay = &self.layout;
        let (d, t) = (c.d, cache.t);
        assert_eq!(tags.len(), t - 1, "one tag per word");

        // Output heads.
        let mut dxf = vec![0.0; t * d];
        let mut dlogit: Vec<f64> = cache.intent.iter().map(|&q| q * scale).collect();
        dlogit[intent] -= scale;
        linear_backward(
            p,
            grads,
            lay.wi,
            lay.bi,
            &cache.xf[..d],
            &dlogit,
            1,
            d,
            c.intents,
            &mut dxf[..d],
        );
        if t > 1 {
            let n = (t - 1) as f64;
            let mut dtags = vec![0.0; (t - 1) * c.tags];
            for (r, &tag) in tags.iter().enumerate() {
                for k in 0..c.tags {
                    dtags[r * c.tags + k] = cache.tags[r * c.tags + k] * scale / n;
                }
                dtags[r * c.tags + tag] -= scale / n;
            }
            linear_backward(
                p,
                grads,
                lay.ws,
                lay.bs,
                &cache.xf[d..],
                &dtags,
                t - 1,
                d,
                c.tags,
                &mut dxf[d..],
            );
        }
        let mut dx = vec![0.0; t * d];
        layernorm_backward(
            p,
            grads,
            lay.lnf_g,
            lay.lnf_b,
            &cache.xhatf,
            &cache.rstdf,
            &dxf,
            t,
            d,
            &mut dx,
        );

        for (l, lc) in lay.layers.iter().zip(&cache.layers).rev() {
            // Feed-forward branch: x2 = x1 + W2 gelu(W1 LN2(x1)).
            let mut dg = vec![0.0; t * c.ff];
            linear_backward(p, grads, l.w2, l.b2, &lc.g, &dx, t, c.ff, d, &mut dg);
            let dh1: Vec<f64> = dg
                .iter()
                .zip(&lc.h1)
                .map(|(&g, &h)| g * gelu_grad(h))
                .collect();
            let mut dm = vec![0.0; t * d];
            linear_backward(p, grads, l.w1, l.b1, &lc.m, &dh1, t, d, c.ff, &mut dm);
            layernorm_backward(
                p, grads, l.ln2_g, l.ln2_b, &lc.xhat2, &lc.rstd2, &dm, t, d, &mut dx,
            );

            // Attention branch: x1 = x0 + Wo attention(Wqkv LN1(x0)).
            let mut do_ = vec![0.0; t * d];
            linear_backward(p, grads, l.wo, l.bo, &lc.o, &dx, t, d, d, &mut do_);
            let dqkv = attention_backward(&lc.qkv, &lc.probs, &do_, t, d, c.heads);
            let mut da = vec![0.0; t * d];
            linear_backward(p, grads, l.wqkv, l.bqkv, &lc.a, &dqkv, t, d, 3 * d, &mut da);
            layernorm_backward(
                p, grads, l.ln1_g, l.ln1_b, &lc.xhat1, &lc.rstd1, &da, t, d, &mut dx,
            );
        }

        // Inputs: x0[pos] = mean of feature embeddings + position embedding.
        for (pos, features) in input.iter().enumerate() {
            let row = &dx[pos * d..(pos + 1) * d];
            let n = features.len() as f64;
            for &f in features {
                let e = lay.emb + f as usize * d;
                for j in 0..d {
                    grads[e + j] += row[j] / n;
                }
            }
            let at = lay.pos + pos * d;
            for j in 0..d {
                grads[at + j] += row[j];
            }
        }
    }
}

/// y = x W + b for `rows` rows; W is n x m, row-major.
fn linear(p: &[f64], w: usize, b: usize, x: &[f64], rows: usize, n: usize, m: usize) -> Vec<f64> {
    let mut y = vec![0.0; rows * m];
    for r in 0..rows {
        let yr = &mut y[r * m..(r + 1) * m];
        yr.copy_from_slice(&p[b..b + m]);
        for i in 0..n {
            let xi = x[r * n + i];
            let wrow = &p[w + i * m..w + (i + 1) * m];
            for j in 0..m {
                yr[j] += xi * wrow[j];
            }
        }
    }
    y
}

/// Gradients of `linear`: adds into dW, db (in `g`) and dx.
#[allow(clippy::too_many_arguments)]
fn linear_backward(
    p: &[f64],
    g: &mut [f64],
    w: usize,
    b: usize,
    x: &[f64],
    dy: &[f64],
    rows: usize,
    n: usize,
    m: usize,
    dx: &mut [f64],
) {
    for r in 0..rows {
        let dyr = &dy[r * m..(r + 1) * m];
        for j in 0..m {
            g[b + j] += dyr[j];
        }
        for i in 0..n {
            let xi = x[r * n + i];
            let wrow = &p[w + i * m..w + (i + 1) * m];
            let grow = &mut g[w + i * m..w + (i + 1) * m];
            let mut acc = 0.0;
            for j in 0..m {
                grow[j] += xi * dyr[j];
                acc += wrow[j] * dyr[j];
            }
            dx[r * n + i] += acc;
        }
    }
}

/// Layer normalisation over each row (Ba, Kiros and Hinton 2016), biased
/// variance, epsilon 1e-5. Returns (y, xhat, 1/std).
fn layernorm(
    p: &[f64],
    gain: usize,
    bias: usize,
    x: &[f64],
    rows: usize,
    d: usize,
) -> (Vec<f64>, Vec<f64>, Vec<f64>) {
    let mut y = vec![0.0; rows * d];
    let mut xhat = vec![0.0; rows * d];
    let mut rstd = vec![0.0; rows];
    for r in 0..rows {
        let row = &x[r * d..(r + 1) * d];
        let mean = row.iter().sum::<f64>() / d as f64;
        let var = row.iter().map(|v| (v - mean) * (v - mean)).sum::<f64>() / d as f64;
        let s = 1.0 / (var + LN_EPS).sqrt();
        rstd[r] = s;
        for j in 0..d {
            let h = (row[j] - mean) * s;
            xhat[r * d + j] = h;
            y[r * d + j] = h * p[gain + j] + p[bias + j];
        }
    }
    (y, xhat, rstd)
}

#[allow(clippy::too_many_arguments)]
fn layernorm_backward(
    p: &[f64],
    g: &mut [f64],
    gain: usize,
    bias: usize,
    xhat: &[f64],
    rstd: &[f64],
    dy: &[f64],
    rows: usize,
    d: usize,
    dx: &mut [f64],
) {
    let mut dxhat = vec![0.0; d];
    for r in 0..rows {
        let mut mean_dxhat = 0.0;
        let mut mean_dxhat_xhat = 0.0;
        for j in 0..d {
            let k = r * d + j;
            g[gain + j] += dy[k] * xhat[k];
            g[bias + j] += dy[k];
            dxhat[j] = dy[k] * p[gain + j];
            mean_dxhat += dxhat[j];
            mean_dxhat_xhat += dxhat[j] * xhat[k];
        }
        mean_dxhat /= d as f64;
        mean_dxhat_xhat /= d as f64;
        for j in 0..d {
            let k = r * d + j;
            dx[k] += rstd[r] * (dxhat[j] - mean_dxhat - xhat[k] * mean_dxhat_xhat);
        }
    }
}

/// Scaled dot-product attention over all positions (no mask: an encoder),
/// Vaswani et al. 2017, eq. 1. `qkv` rows are [q | k | v]; head h uses
/// columns h*dh..(h+1)*dh of each. Returns (probabilities, concatenated heads).
fn attention(qkv: &[f64], t: usize, d: usize, heads: usize) -> (Vec<f64>, Vec<f64>) {
    let dh = d / heads;
    let scale = 1.0 / (dh as f64).sqrt();
    let mut probs = vec![0.0; heads * t * t];
    let mut o = vec![0.0; t * d];
    for h in 0..heads {
        for i in 0..t {
            let q = &qkv[i * 3 * d + h * dh..i * 3 * d + h * dh + dh];
            let row = &mut probs[(h * t + i) * t..(h * t + i + 1) * t];
            for (s, score) in row.iter_mut().enumerate() {
                let k = &qkv[s * 3 * d + d + h * dh..s * 3 * d + d + h * dh + dh];
                *score = q.iter().zip(k).map(|(a, b)| a * b).sum::<f64>() * scale;
            }
            softmax(row);
            for s in 0..t {
                let v = &qkv[s * 3 * d + 2 * d + h * dh..s * 3 * d + 2 * d + h * dh + dh];
                let ps = row[s];
                for j in 0..dh {
                    o[i * d + h * dh + j] += ps * v[j];
                }
            }
        }
    }
    (probs, o)
}

fn attention_backward(
    qkv: &[f64],
    probs: &[f64],
    d_o: &[f64],
    t: usize,
    d: usize,
    heads: usize,
) -> Vec<f64> {
    let dh = d / heads;
    let scale = 1.0 / (dh as f64).sqrt();
    let mut dqkv = vec![0.0; t * 3 * d];
    let mut dp = vec![0.0; t];
    for h in 0..heads {
        for i in 0..t {
            let row = &probs[(h * t + i) * t..(h * t + i + 1) * t];
            let doi = &d_o[i * d + h * dh..i * d + h * dh + dh];
            // dp[s] = do_i . v_s, and dv_s += p[s] do_i.
            for s in 0..t {
                let v_at = s * 3 * d + 2 * d + h * dh;
                let mut acc = 0.0;
                for j in 0..dh {
                    acc += doi[j] * qkv[v_at + j];
                    dqkv[v_at + j] += row[s] * doi[j];
                }
                dp[s] = acc;
            }
            // Softmax backward: ds = p (dp - sum p dp).
            let mean: f64 = row.iter().zip(&dp).map(|(p, g)| p * g).sum();
            let q_at = i * 3 * d + h * dh;
            for s in 0..t {
                let ds = row[s] * (dp[s] - mean) * scale;
                let k_at = s * 3 * d + d + h * dh;
                for j in 0..dh {
                    dqkv[q_at + j] += ds * qkv[k_at + j];
                    dqkv[k_at + j] += ds * qkv[q_at + j];
                }
            }
        }
    }
    dqkv
}
