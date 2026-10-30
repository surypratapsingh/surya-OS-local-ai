// B6.1 (2026-10-30): the WASI preview1 runner used for the local Windows
// transcripts in b6-evidence.log. It was previously only at mathd/target/
// (git-ignored), which made every local wasm32 result unreproducible from a
// clone. Committed here so the command can be re-run. Usage:
//   cargo build --release --target wasm32-wasip1 --test corpus
//   node mathd/corpus/wasi-runner.mjs \
//       mathd/target/wasm32-wasip1/release/deps/corpus-<hash>.wasm

import { WASI } from 'node:wasi';
import { argv, cwd, env } from 'node:process';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
const wasmPath = argv[2];
const wasmArgs = argv.slice(2);
const crateDir = resolve(cwd());
const preopens = { '.': crateDir };
if (env.FB_PREOPEN) for (const p of env.FB_PREOPEN.split(';')) if (p) preopens[p] = crateDir;
const wasi = new WASI({ version: 'preview1', args: wasmArgs, env: {}, preopens, returnOnExit: true });
const mod = await WebAssembly.compile(readFileSync(wasmPath));
const instance = await WebAssembly.instantiate(mod, wasi.getImportObject());
const code = wasi.start(instance);
process.exit(code ?? 0);
