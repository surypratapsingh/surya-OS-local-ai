//! Oracle: finite differences of the loss, a second way to get every
//! gradient, compared with the hand-written backward pass for every
//! parameter of a small model.
//!
//! The estimate is Richardson extrapolation of two central differences
//! (steps 1e-3 and 5e-4): (4 D(h/2) - D(h)) / 3 cancels the h^2 error term.
//! A first version used one central difference with step 1e-5 and failed on
//! parameter 589 by 2.8e-11, which is that estimate's rounding floor
//! (loss ~3 times 1.1e-16 / 1e-5); the extrapolated estimate agreed with the
//! backward pass to 1.1e-13 there (docs/logs/n4-evidence.log).

use intent::model::{Config, Model, SplitMix64};

const SMALL: Config = Config { buckets: 64, d: 8, heads: 2, ff: 12, layers: 2, max_len: 6, intents: 4, tags: 3 };

fn example() -> (Vec<Vec<u32>>, usize, Vec<usize>) {
    // Position 0 is the summary token; position 3 repeats a feature.
    let input = vec![vec![1], vec![5, 9, 13], vec![20, 21], vec![33, 33, 40, 41], vec![63]];
    (input, 2, vec![1, 0, 2, 1])
}

fn perturbed(model: &mut Model) {
    let mut rng = SplitMix64(7);
    for p in model.params.iter_mut() {
        *p += (rng.unit() - 0.5) * 0.2; // move off the initial point: LN gains not all 1
    }
}

fn central(model: &mut Model, i: usize, h: f64, input: &[Vec<u32>], intent: usize, tags: &[usize]) -> f64 {
    let saved = model.params[i];
    model.params[i] = saved + h;
    let up = model.loss(&model.forward(input), intent, tags);
    model.params[i] = saved - h;
    let down = model.loss(&model.forward(input), intent, tags);
    model.params[i] = saved;
    (up - down) / (2.0 * h)
}

#[test]
fn backward_matches_finite_differences_for_every_parameter() {
    let mut model = Model::init(SMALL, 42);
    perturbed(&mut model);
    let (input, intent, tags) = example();
    let cache = model.forward(&input);
    let mut grads = vec![0.0; model.params.len()];
    model.backward(&input, &cache, intent, &tags, &mut grads, 1.0);

    let mut worst = 0.0f64;
    let mut checked_nonzero = 0;
    for i in 0..model.params.len() {
        let coarse = central(&mut model, i, 1e-3, &input, intent, &tags);
        let fine = central(&mut model, i, 5e-4, &input, intent, &tags);
        let numeric = (4.0 * fine - coarse) / 3.0;
        let error = (grads[i] - numeric).abs();
        if numeric.abs() > 1e-9 {
            checked_nonzero += 1;
        }
        worst = worst.max(error);
        assert!(
            error <= 1e-10 + 1e-7 * numeric.abs(),
            "parameter {i}: backward {:e} numeric {numeric:e} (difference {error:e})",
            grads[i]
        );
    }
    // Non-vacuous: most parameters must have a real gradient here (unused
    // embedding rows are the only zeros).
    assert!(checked_nonzero > model.params.len() / 2, "only {checked_nonzero} non-zero gradients");
    println!("checked {} parameters, {checked_nonzero} non-zero, worst absolute difference {worst:e}", model.params.len());
}

#[test]
fn scale_multiplies_the_gradient() {
    let model = Model::init(SMALL, 3);
    let (input, intent, tags) = example();
    let cache = model.forward(&input);
    let mut one = vec![0.0; model.params.len()];
    let mut half = vec![0.0; model.params.len()];
    model.backward(&input, &cache, intent, &tags, &mut one, 1.0);
    model.backward(&input, &cache, intent, &tags, &mut half, 0.5);
    for (a, b) in one.iter().zip(&half) {
        assert!((a * 0.5 - b).abs() <= 1e-15 * (1.0 + a.abs()));
    }
}
