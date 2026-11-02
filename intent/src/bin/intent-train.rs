//! intent-train: train, evaluate, and write the cross-check fixture.
//!
//!   intent-train train   --train intent/data/train.tsv --dev intent/data/dev.tsv --out intent/model/nova-intent.bin
//!                        [--extra MINE.lessons.tsv --extra-weight N]   (N4d; --out must then end in .lessons.bin)
//!   intent-train eval    --model intent/model/nova-intent.bin --data intent/data/dev.tsv
//!   intent-train fixture --model intent/model/nova-intent.bin --out intent/model/fixture.json

use intent::features::{encode, tokens};
use intent::io;
use intent::model::{Config, Model};
use intent::train::{evaluate, parse_tsv, train, TrainConfig};
use std::path::PathBuf;
use std::process::ExitCode;
use std::time::Instant;

/// Sentences whose outputs the stdlib-Python runtime must reproduce
/// (novacore/tests/test_intent.py). Chosen to cover punctuation, case,
/// digits, apostrophes, non-ASCII, a typo, one word, and a long line.
const FIXTURE_SENTENCES: [&str; 14] = [
    "open spotify",
    "What is the volume of a sphere?",
    "remind me in 10 minutes to stretch",
    "remember that Mom's birthday is June 5",
    "find my class 12 physics notes",
    "café ☕ open notepad",
    "lock teh screen",
    "skip",
    "",
    "please could you turn the volume down a little bit because it is far too loud in here right now thank you nova",
    "INVOICE-42.PDF",
    "x^2 + 3x = 7",
    "play some country music",
    "make a pdf called lab results with notes from the lecture",
];

fn arg(args: &[String], name: &str) -> Option<String> {
    args.iter()
        .position(|a| a == name)
        .and_then(|i| args.get(i + 1))
        .cloned()
}

fn need(args: &[String], name: &str) -> Result<String, String> {
    arg(args, name).ok_or_else(|| format!("missing {name}"))
}

fn read(path: &str) -> Result<String, String> {
    std::fs::read_to_string(path).map_err(|e| format!("{path}: {e}"))
}

fn json_string(s: &str) -> String {
    let mut out = String::from("\"");
    for unit in s.encode_utf16() {
        match unit {
            0x22 => out.push_str("\\\""),
            0x5c => out.push_str("\\\\"),
            0x20..=0x7e => out.push(unit as u8 as char),
            _ => out.push_str(&format!("\\u{unit:04x}")),
        }
    }
    out.push('"');
    out
}

fn json_numbers(values: &[f64]) -> String {
    let parts: Vec<String> = values.iter().map(|v| format!("{v:e}")).collect();
    format!("[{}]", parts.join(","))
}

/// N4d: the owner's lessons (`novacore --export-lessons`), each line added
/// `--extra-weight` times (default 1) after the training file's lines, so the
/// run stays deterministic. A model trained on them holds what it learned from
/// the owner's commands: its file name must end in `.lessons.bin`, which
/// .gitignore lists, so it is not committed by accident.
type Extra = (Vec<intent::train::Example>, usize, usize, String);

fn extra_lessons(
    args: &[String],
    out: &std::path::Path,
    buckets: usize,
    max_len: usize,
) -> Result<Option<Extra>, String> {
    let Some(path) = arg(args, "--extra") else {
        if arg(args, "--extra-weight").is_some() {
            return Err("--extra-weight needs --extra".into());
        }
        return Ok(None);
    };
    let weight = match arg(args, "--extra-weight") {
        Some(w) => w.parse::<usize>().map_err(|_| "bad --extra-weight")?,
        None => 1,
    };
    if !(1..=1000).contains(&weight) {
        return Err("--extra-weight must be 1 to 1000".into());
    }
    if !out.to_string_lossy().ends_with(".lessons.bin") {
        return Err(format!(
            "with --extra, --out must end in .lessons.bin, not {}: the model holds what it learned from your commands, and .gitignore lists those files",
            out.display()
        ));
    }
    let text = read(&path)?;
    let mut examples = Vec::new();
    let mut too_long = 0;
    for _ in 0..weight {
        let (lines, long) = parse_tsv(&text, buckets, max_len)?;
        examples.extend(lines);
        too_long = long;
    }
    if examples.is_empty() {
        return Err(format!("{path}: no training lines"));
    }
    Ok(Some((examples, too_long, weight, path)))
}

fn run(args: &[String]) -> Result<(), String> {
    match args.first().map(String::as_str) {
        Some("train") => {
            let cfg = Config::NOVA;
            let mut tc = TrainConfig::NOVA;
            if let Some(e) = arg(args, "--epochs") {
                tc.epochs = e.parse().map_err(|_| "bad --epochs")?;
            }
            let out = PathBuf::from(need(args, "--out")?);
            let (mut data, long_train) =
                parse_tsv(&read(&need(args, "--train")?)?, cfg.buckets, cfg.max_len)?;
            let (dev, long_dev) =
                parse_tsv(&read(&need(args, "--dev")?)?, cfg.buckets, cfg.max_len)?;
            let from_files = data.len();
            let extra = extra_lessons(args, &out, cfg.buckets, cfg.max_len)?;
            let mut extra_note = String::new();
            if let Some((examples, long_extra, weight, path)) = extra {
                let lines = examples.len() / weight;
                extra_note = format!(
                    " + {lines} lesson lines x {weight} = {} ({long_extra} too long, skipped) from {path}",
                    examples.len()
                );
                data.extend(examples);
            }
            println!(
                "train {} examples ({} too long, skipped){}, dev {} ({} skipped); config {:?}; epochs {} batch {} lr {} seed {}",
                from_files, long_train, extra_note, dev.len(), long_dev, cfg, tc.epochs, tc.batch, tc.lr, tc.seed
            );
            let mut model = Model::init(cfg, tc.seed);
            println!("parameters: {}", model.params.len());
            let started = Instant::now();
            train(&mut model, &data, &dev, &tc, &mut |line| {
                println!("{line}  [{:.0} s]", started.elapsed().as_secs_f64())
            });
            io::save(&model, &out).map_err(|e| format!("{}: {e}", out.display()))?;
            // Evaluate what was shipped: the f32 file, read back.
            let shipped = io::load(&out)?;
            let m = evaluate(&shipped, &dev);
            println!(
                "shipped f32 model on dev: loss {:.4} intent accuracy {:.4} tag accuracy {:.4} chat lines read as an action {} of {}",
                m.loss, m.intent_accuracy, m.tag_accuracy, m.chat_as_action, m.chat_lines
            );
            Ok(())
        }
        Some("eval") => {
            let model = io::load(&PathBuf::from(need(args, "--model")?))?;
            let (data, long) = parse_tsv(
                &read(&need(args, "--data")?)?,
                model.cfg.buckets,
                model.cfg.max_len,
            )?;
            let m = evaluate(&model, &data);
            println!(
                "{} examples ({} too long): loss {:.4} intent accuracy {:.4} tag accuracy {:.4} chat lines read as an action {} of {}",
                m.examples, long, m.loss, m.intent_accuracy, m.tag_accuracy, m.chat_as_action, m.chat_lines
            );
            Ok(())
        }
        Some("fixture") => {
            let model = io::load(&PathBuf::from(need(args, "--model")?))?;
            let mut cases = Vec::new();
            for text in FIXTURE_SENTENCES {
                let words: Vec<String> = tokens(text).iter().map(|w| json_string(w)).collect();
                let input = encode(text, model.cfg.buckets, model.cfg.max_len)
                    .ok_or("fixture sentence too long")?;
                let cache = model.forward(&input);
                let tag_rows: Vec<String> = cache
                    .tags
                    .chunks(model.cfg.tags)
                    .map(json_numbers)
                    .collect();
                cases.push(format!(
                    "{{\"text\":{},\"tokens\":[{}],\"intent\":{},\"tags\":[{}]}}",
                    json_string(text),
                    words.join(","),
                    json_numbers(&cache.intent),
                    tag_rows.join(",")
                ));
            }
            let out = need(args, "--out")?;
            let body = format!(
                "{{\"about\":\"Probabilities computed by intent-train fixture (Rust, f64 arithmetic on the f32 model file). novacore/tests/test_intent.py checks the Python runtime against them.\",\"cases\":[\n{}\n]}}\n",
                cases.join(",\n")
            );
            std::fs::write(&out, body).map_err(|e| format!("{out}: {e}"))?;
            println!("wrote {} cases to {out}", cases.len());
            Ok(())
        }
        _ => Err(
            "usage: intent-train train|eval|fixture ... (see the top of src/bin/intent-train.rs)"
                .into(),
        ),
    }
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(message) => {
            eprintln!("intent-train: {message}");
            ExitCode::FAILURE
        }
    }
}
