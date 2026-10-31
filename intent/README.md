# intent — NOVA's own command model (work order N4)

A small transformer encoder, written and trained from scratch, that reads the
owner's words and answers: chat, or one of 13 desktop actions with its
arguments. It is the "router" of work order N1, made here instead of
downloaded. Trainer: std-only Rust (this crate). Runtime: stdlib Python
(`novacore/src/novacore/intent.py`). Both implement the specification below;
`novacore/tests/test_intent.py` checks that they agree.

It is not a chat model. Answering questions still needs an owner-supplied
language model (`docs/work-orders.md`, N3).

## Tokens

ASCII letters are lower-cased; a token is a maximal run of `a-z` and `0-9`.
Every other character, including every non-ASCII character, separates tokens.
So "Mom's birthday" is `mom`, `s`, `birthday`, and Devanagari text gives no
tokens: the model is English-only for now.

## Features

Position 0 is a summary position with the single feature `c:cls`. Each token
`w` then has the features `w:<w>` and `t:<g>` for every character trigram `g`
of `<w>` (with `<` and `>` added), in order. A feature's bucket is
FNV-1a-32 of its UTF-8 bytes, modulo 8192. A position's input vector is the
mean of its feature embeddings plus its position embedding. At most 64
positions (63 tokens); a longer line is not read (the runtime answers chat).

## Model

d = 48, 4 heads, feed-forward 96, 2 pre-norm layers, no attention mask
(an encoder). LayerNorm with biased variance and epsilon 1e-5. GELU, tanh
form. A final LayerNorm, then: intent logits from position 0 (14 classes,
order in `src/lib.rs` `INTENTS`), and tag logits for every token position
(11 BIO tags, `TAGS`). Loss: intent cross-entropy plus the mean tag
cross-entropy. 435,529 parameters.

## Layout and file format

Parameters, in this order: embeddings (8192 x 48), positions (64 x 48); for
each layer: ln1 gain, ln1 bias, Wqkv (48 x 144), bqkv, Wo (48 x 48), bo,
ln2 gain, ln2 bias, W1 (48 x 96), b1, W2 (96 x 48), b2; then final gain and
bias, Wi (48 x 14), bi, Ws (48 x 11), bs. Matrices are row-major, input
index first. The file `model/nova-intent.bin` is the 8 bytes `NOVAINT1`,
nine little-endian u32 values (buckets, d, heads, ff, layers, max_len,
intents, tags, parameter count), then the parameters as little-endian f32.

## Training

`data/generate.py` (stdlib, seeded) writes `data/train.tsv` and
`data/dev.tsv` from templates. AdamW (beta 0.9 / 0.999, weight decay 0.01 on
the weight matrices), peak rate 2e-3 after 300 warm-up steps, then linear
decay to a tenth, batch 32, 12 epochs, gradient norm clipped at 1, seed
20261031. Training uses only +, -, *, / and square root (its own `exp`, see
`src/math.rs`), so the same code and data give the same file on any machine.

    python3 intent/data/generate.py
    cargo run --release --manifest-path intent/Cargo.toml --bin intent-train -- \
        train --train intent/data/train.tsv --dev intent/data/dev.tsv --out intent/model/nova-intent.bin

## Decoding, fixed before training

The runtime takes the most probable intent. If it is an action and its
probability is below the threshold, the answer is chat. Slots: the first
B-x tag and the I-x tags that follow it (an I-x with no B-x before it also
starts a span). `apps.open` must name exactly one app the schema offers,
compared with spaces and punctuation removed and a trailing "app",
"application" or "program" ignored. `timer.set` needs a duration the runtime
can read (digits or number words, minutes or hours, "half an hour", "an
hour"), from 1 to 1440 minutes. `files.*` and `memory.save` need a non-empty
span. Anything that fails is chat.

**The threshold is chosen on the development set only, by this rule written
before any training run:** the smallest value in 0.5, 0.6, 0.7, 0.8, 0.9 at
which the full runtime reads no chat line of `data/dev.tsv` as an action; 0.9
if none qualifies. The hand-written test set (`data/test.jsonl`) is scored
once, after that choice, and never used to change anything.
