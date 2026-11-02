//! N4d: `intent-train train --extra` (src/bin/intent-train.rs), run as the real
//! binary. Oracles: the counts printed, against line counts written here by
//! hand, and the refusals; nothing is read back from the trainer's own code.

use std::path::{Path, PathBuf};
use std::process::Command;

const TRAIN: &str = "chat\thow are you\tO O O\napps.open\topen notes\tO B-app\n";
const LESSONS: &str = "apps.open\tstart probe\tO B-app\nchat\tgood morning\tO O\n";

fn folder(name: &str) -> PathBuf {
    let path = std::env::temp_dir().join(format!("intent-extra-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&path);
    std::fs::create_dir_all(&path).unwrap();
    std::fs::write(path.join("train.tsv"), TRAIN).unwrap();
    std::fs::write(path.join("mine.lessons.tsv"), LESSONS).unwrap();
    path
}

fn train(dir: &Path, out: &str, more: &[&str]) -> (bool, String) {
    let train = dir.join("train.tsv");
    let mut args = vec![
        "train".to_string(),
        "--train".into(),
        train.display().to_string(),
        "--dev".into(),
        train.display().to_string(),
        "--epochs".into(),
        "1".into(),
        "--out".into(),
        dir.join(out).display().to_string(),
    ];
    args.extend(more.iter().map(|s| s.to_string()));
    let done = Command::new(env!("CARGO_BIN_EXE_intent-train"))
        .args(&args)
        .output()
        .unwrap();
    let text = format!(
        "{}{}",
        String::from_utf8_lossy(&done.stdout),
        String::from_utf8_lossy(&done.stderr)
    );
    (done.status.success(), text)
}

#[test]
fn each_lesson_line_is_added_weight_times() {
    let dir = folder("weight");
    let lessons = dir.join("mine.lessons.tsv").display().to_string();
    let (ok, text) = train(
        &dir,
        "mine.lessons.bin",
        &["--extra", &lessons, "--extra-weight", "3"],
    );
    assert!(ok, "{text}");
    // 2 lines in TRAIN; 2 lines in LESSONS, 3 times each.
    let expected = format!(
        "train 2 examples (0 too long, skipped) + 2 lesson lines x 3 = 6 (0 too long, skipped) from {lessons}, dev 2"
    );
    assert!(text.contains(&expected), "{text}");
    assert!(dir.join("mine.lessons.bin").exists());
}

#[test]
fn a_model_trained_on_lessons_must_be_named_lessons_bin() {
    let dir = folder("name");
    let lessons = dir.join("mine.lessons.tsv").display().to_string();
    let (ok, text) = train(&dir, "nova-intent.bin", &["--extra", &lessons]);
    assert!(!ok);
    assert!(text.contains("--out must end in .lessons.bin"), "{text}");
    assert!(!dir.join("nova-intent.bin").exists());
}

#[test]
fn a_weight_without_lessons_or_out_of_range_is_refused() {
    let dir = folder("refused");
    let lessons = dir.join("mine.lessons.tsv").display().to_string();
    let (ok, text) = train(&dir, "a.lessons.bin", &["--extra-weight", "3"]);
    assert!(
        !ok && text.contains("--extra-weight needs --extra"),
        "{text}"
    );
    let (ok, text) = train(
        &dir,
        "a.lessons.bin",
        &["--extra", &lessons, "--extra-weight", "0"],
    );
    assert!(
        !ok && text.contains("--extra-weight must be 1 to 1000"),
        "{text}"
    );
    std::fs::write(dir.join("empty.lessons.tsv"), "").unwrap();
    let empty = dir.join("empty.lessons.tsv").display().to_string();
    let (ok, text) = train(&dir, "a.lessons.bin", &["--extra", &empty]);
    assert!(!ok && text.contains("no training lines"), "{text}");
    assert!(!dir.join("a.lessons.bin").exists());
}
