//! Deterministic elementary functions.
//!
//! Training must give the same model file on every machine. `f64::exp` and
//! `f64::tanh` come from the platform's libm, whose last bits differ between
//! versions, so the trainer uses `exp` below: only +, -, *, / and exact power
//! of two scaling, which IEEE 754 fixes. `tests/features.rs` checks it
//! against libm (an independent implementation) to 1e-14 relative.

/// ln 2 split in two (fdlibm's e_exp.c constants), so that k * LN2_HI is exact.
const LN2_HI: f64 = 6.931_471_803_691_238_164_90e-01;
const LN2_LO: f64 = 1.908_214_929_270_587_700_02e-10;
const INV_LN2: f64 = 1.442_695_040_888_963_387_00e+00;

pub fn exp(x: f64) -> f64 {
    if x.is_nan() {
        return x;
    }
    if x > 709.0 {
        return f64::INFINITY;
    }
    if x < -745.0 {
        return 0.0;
    }
    // x = k ln2 + r, |r| <= ln2 / 2.
    let k = (x * INV_LN2).round();
    let r = (x - k * LN2_HI) - k * LN2_LO;
    // Taylor series to r^17 / 17!: the next term is below 1e-21 for |r| <= 0.35.
    let mut term = 1.0;
    let mut sum = 1.0;
    for n in 1..=17 {
        term = term * r / f64::from(n);
        sum += term;
    }
    scale_by_power_of_two(sum, k as i32)
}

fn scale_by_power_of_two(value: f64, k: i32) -> f64 {
    // Two steps keep every factor a normal power of two.
    let half = k / 2;
    value * two_to(half) * two_to(k - half)
}

fn two_to(k: i32) -> f64 {
    f64::from_bits(((k + 1023) as u64) << 52)
}

pub fn tanh(x: f64) -> f64 {
    if x > 20.0 {
        return 1.0;
    }
    if x < -20.0 {
        return -1.0;
    }
    let e = exp(2.0 * x);
    (e - 1.0) / (e + 1.0)
}

const GELU_C: f64 = 0.797_884_560_802_865_4; // sqrt(2 / pi)

/// GELU, tanh form (Hendrycks and Gimpel 2016, eq. in section 2).
pub fn gelu(x: f64) -> f64 {
    0.5 * x * (1.0 + tanh(GELU_C * (x + 0.044_715 * x * x * x)))
}

pub fn gelu_grad(x: f64) -> f64 {
    let t = tanh(GELU_C * (x + 0.044_715 * x * x * x));
    0.5 * (1.0 + t) + 0.5 * x * (1.0 - t * t) * GELU_C * (1.0 + 3.0 * 0.044_715 * x * x)
}

/// Softmax in place, max-subtracted.
pub fn softmax(values: &mut [f64]) {
    let max = values.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
    let mut total = 0.0;
    for v in values.iter_mut() {
        *v = exp(*v - max);
        total += *v;
    }
    for v in values.iter_mut() {
        *v /= total;
    }
}
