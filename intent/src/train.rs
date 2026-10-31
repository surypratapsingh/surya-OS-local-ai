//! Data loading, AdamW training and evaluation.

use crate::features::{encode, tokens};
use crate::model::{Model, SplitMix64};
use crate::{INTENTS, TAGS};

pub struct Example {
    pub input: Vec<Vec<u32>>,
    pub intent: usize,
    pub tags: Vec<usize>,
}

/// Parse `intent<TAB>text<TAB>tags` lines (intent/data/generate.py). A line
/// whose tag count differs from the token count of its text is an error: the
/// generator's tokenizer and this one disagree.
pub fn parse_tsv(text: &str, buckets: usize, max_len: usize) -> Result<(Vec<Example>, usize), String> {
    let mut out = Vec::new();
    let mut too_long = 0;
    for (number, line) in text.lines().enumerate() {
        let fields: Vec<&str> = line.split('\t').collect();
        if fields.len() != 3 {
            return Err(format!("line {}: expected 3 tab-separated fields", number + 1));
        }
        let intent = INTENTS
            .iter()
            .position(|&n| n == fields[0])
            .ok_or_else(|| format!("line {}: unknown intent {:?}", number + 1, fields[0]))?;
        let tags: Vec<usize> = fields[2]
            .split(' ')
            .filter(|s| !s.is_empty())
            .map(|s| TAGS.iter().position(|&t| t == s).ok_or_else(|| format!("line {}: unknown tag {s:?}", number + 1)))
            .collect::<Result<_, _>>()?;
        let words = tokens(fields[1]);
        if words.len() != tags.len() {
            return Err(format!(
                "line {}: {} tokens but {} tags in {:?}",
                number + 1,
                words.len(),
                tags.len(),
                fields[1]
            ));
        }
        match encode(fields[1], buckets, max_len) {
            Some(input) => out.push(Example { input, intent, tags }),
            None => too_long += 1,
        }
    }
    Ok((out, too_long))
}

pub struct TrainConfig {
    pub epochs: usize,
    pub batch: usize,
    pub lr: f64,
    pub warmup: usize,
    pub weight_decay: f64,
    pub clip: f64,
    pub seed: u64,
}

impl TrainConfig {
    pub const NOVA: TrainConfig = TrainConfig {
        epochs: 12,
        batch: 32,
        lr: 2e-3,
        warmup: 300,
        weight_decay: 0.01,
        clip: 1.0,
        seed: 20_261_031,
    };
}

#[derive(Debug, Clone, Copy, Default)]
pub struct Metrics {
    pub examples: usize,
    pub loss: f64,
    pub intent_accuracy: f64,
    pub tag_accuracy: f64,
    /// Examples whose intent is chat but whose top intent is an action.
    pub chat_as_action: usize,
    pub chat_lines: usize,
}

pub fn evaluate(model: &Model, examples: &[Example]) -> Metrics {
    let mut m = Metrics { examples: examples.len(), ..Metrics::default() };
    let (mut right, mut tags_right, mut tags_total) = (0usize, 0usize, 0usize);
    for ex in examples {
        let cache = model.forward(&ex.input);
        m.loss += model.loss(&cache, ex.intent, &ex.tags);
        let top = argmax(&cache.intent);
        right += usize::from(top == ex.intent);
        if ex.intent == 0 {
            m.chat_lines += 1;
            m.chat_as_action += usize::from(top != 0);
        }
        for (row, &tag) in cache.tags.chunks(model.cfg.tags).zip(&ex.tags) {
            tags_right += usize::from(argmax(row) == tag);
            tags_total += 1;
        }
    }
    let n = examples.len().max(1) as f64;
    m.loss /= n;
    m.intent_accuracy = right as f64 / n;
    m.tag_accuracy = tags_right as f64 / tags_total.max(1) as f64;
    m
}

pub fn argmax(values: &[f64]) -> usize {
    let mut best = 0;
    for (i, &v) in values.iter().enumerate() {
        if v > values[best] {
            best = i;
        }
    }
    best
}

/// AdamW (Loshchilov and Hutter 2019) with linear warm-up, then linear decay
/// to a tenth of the peak rate, and global gradient-norm clipping.
pub fn train(model: &mut Model, data: &[Example], dev: &[Example], tc: &TrainConfig, log: &mut dyn FnMut(String)) {
    let n = model.params.len();
    let mut grads = vec![0.0; n];
    let mut m1 = vec![0.0; n];
    let mut m2 = vec![0.0; n];
    let mut decay = vec![false; n];
    for &(at, len) in &model.layout.decayed {
        for flag in &mut decay[at..at + len] {
            *flag = true;
        }
    }
    let (beta1, beta2, eps) = (0.9, 0.999, 1e-8);
    let (mut beta1_t, mut beta2_t) = (1.0, 1.0);
    let steps_per_epoch = data.len().div_ceil(tc.batch);
    let total = steps_per_epoch * tc.epochs;
    let mut order: Vec<usize> = (0..data.len()).collect();
    let mut rng = SplitMix64(tc.seed ^ 0x5eed);
    let mut step = 0usize;
    for epoch in 1..=tc.epochs {
        // Fisher-Yates shuffle.
        for i in (1..order.len()).rev() {
            let j = rng.below(i + 1);
            order.swap(i, j);
        }
        let mut epoch_loss = 0.0;
        for batch in order.chunks(tc.batch) {
            step += 1;
            grads.iter_mut().for_each(|g| *g = 0.0);
            let scale = 1.0 / batch.len() as f64;
            for &i in batch {
                let ex = &data[i];
                let cache = model.forward(&ex.input);
                epoch_loss += model.loss(&cache, ex.intent, &ex.tags);
                model.backward(&ex.input, &cache, ex.intent, &ex.tags, &mut grads, scale);
            }
            let norm = grads.iter().map(|g| g * g).sum::<f64>().sqrt();
            if norm > tc.clip {
                let s = tc.clip / norm;
                grads.iter_mut().for_each(|g| *g *= s);
            }
            let lr = if step <= tc.warmup {
                tc.lr * step as f64 / tc.warmup as f64
            } else {
                tc.lr * (1.0 - 0.9 * (step - tc.warmup) as f64 / (total - tc.warmup).max(1) as f64)
            };
            beta1_t *= beta1;
            beta2_t *= beta2;
            for i in 0..n {
                let g = grads[i];
                m1[i] = beta1 * m1[i] + (1.0 - beta1) * g;
                m2[i] = beta2 * m2[i] + (1.0 - beta2) * g * g;
                let mhat = m1[i] / (1.0 - beta1_t);
                let vhat = m2[i] / (1.0 - beta2_t);
                let mut update = mhat / (vhat.sqrt() + eps);
                if decay[i] {
                    update += tc.weight_decay * model.params[i];
                }
                model.params[i] -= lr * update;
            }
        }
        let dev_metrics = evaluate(model, dev);
        log(format!(
            "epoch {epoch:>2}: train loss {:.4}  dev loss {:.4}  dev intent accuracy {:.4}  dev tag accuracy {:.4}  dev chat lines read as an action {} of {}",
            epoch_loss / data.len() as f64,
            dev_metrics.loss,
            dev_metrics.intent_accuracy,
            dev_metrics.tag_accuracy,
            dev_metrics.chat_as_action,
            dev_metrics.chat_lines,
        ));
    }
}
