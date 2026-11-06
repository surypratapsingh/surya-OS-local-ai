//! F1.8 plant by freebuff (review of A1.4). This file must never reach main.
//!
//! It exists only to prove that the windows-latest job in
//! .github/workflows/mathd.yml actually runs the test suite: the test below
//! fails under cfg(windows) and passes everywhere else, so gnu and musl must
//! stay green while windows turns red.

#[test]
fn plant_fails_only_on_windows() {
    if cfg!(windows) {
        panic!("F1.8 plant: this test fails only on windows");
    }
    assert_eq!(1 + 1, 2);
}
