# Local models for NOVA on a 16 GB laptop (work order N3)

> **Status, 2026-11-02 (lead, claude).** Written from pages found by web search
> on 2026-11-02. **Nothing here was downloaded or run** where this was written:
> Hugging Face is not reachable from that environment, and the primary model
> cards on huggingface.co and ai.google.dev failed to load. Of the pages listed
> at the end, only the llama.cpp pull request was read directly. Every file
> size below is a size a named page lists. Every speed or memory figure is an
> **estimate** and says so. On the owner's laptop, the N3 eval
> (`novacore/eval/run_eval.py`) decides. This page only says what to try first.

## The two jobs

NovaCore calls a local model for two different jobs (`novacore/SCOPE.md`):

- **`[model]`, chat and teacher.** It answers questions, explains, talks, and
  can never act. Quality matters more than speed.
- **`[router]`, picking at most one action.** It gets a short prompt and must
  answer one JSON object, which `{schema_file}` (llama.cpp `-jf`) and the
  strict parser limit. Speed matters, because voice waits for it. Safety matters
  most: the N3 gate is no wrong and no false action on an action that runs
  without asking.

NOVA's own command model (`intent/`, 0.4 million parameters, no download) is
the router today. On the N3 set it answered 105 of 120 lines right in 0.07 s
per line on the machine that wrote this, but 9 of its answers were actions
that would run without asking (`docs/logs/n3-evidence.log`). It does not meet
the gate, so keep `confirm_all_actions = true` until a router does. Lessons
(N4d) fix it for the exact words you teach.

## What fits in 16 GB (estimates)

- Windows 11 with a browser and your usual apps open uses several GB on its
  own. Check what is left in Task Manager → Performance → Memory with
  everything you normally run open. This guide assumes **about 8 to 10 GB for
  models**, as an estimate.
- A GGUF model needs about its file size in memory, plus the context cache
  (estimate: a few hundred MB at the short contexts NOVA uses).
- NovaCore starts the runner once per message. Windows keeps the model file in
  its cache between calls when memory allows; the first call after a restart
  reads it from disk.
- If the chat model and the router are the **same file**, it is cached once.
  On 16 GB, that is the simplest choice.

## Candidates

The licences and sizes below come from the pages in "Sources". They are not
checked against the model cards; check each card before you download.

| Model | Try it for | Q4_K_M file, as listed | Licence, as reported | Notes |
|---|---|---|---|---|
| Qwen3.5 4B | router; chat if speed matters | 3.01 GB (bartowski), 2.8 GB (SoAIHQ), 2.7 GB (AtomicChat) | Apache 2.0 | Released in March 2026 with 0.8B, 2B and 9B. CPU-only round-ups name it the best place to start. |
| Qwen3.5 9B | chat and teacher; router if fast enough | 6.17 GB (bartowski), 5.8 GB (SoAIHQ), 5.6 GB (AtomicChat) | Apache 2.0 | The 16 GB pick in several round-ups. |
| Gemma 4 E4B | either | 4.98 to 5.41 GB, depending on the uploader | Apache 2.0 | Released 2026-04-02. Its card reportedly includes audio input. |
| Phi-4-mini-instruct (3.8B) | router | about 2.5 GB | MIT | A smaller alternative from another family. |
| gpt-oss-20b | **not on this laptop** | 13.8 GB weights (MXFP4) | Apache 2.0 | Listed as running "within 16GB of memory", which leaves nothing for Windows. |
| Gemma 4 26B A4B, Gemma 4 31B | **not on this laptop** | not looked up; about 15 GB and 17 GB at 4.5 bits per weight (estimate from the parameter counts) | Apache 2.0 | Too big for 16 GB with Windows running. |

Speed (estimate only): one guide puts an 8 to 9B model at Q4 at about 8 to 15
tokens a second on a 16 GB laptop's CPU, and smaller models faster. The router
also reads a prompt of several hundred tokens on every message. The eval
prints the real seconds per line on your machine.

Qwen3.5 can "think" before answering. Under the `-jf` grammar the router's
output is limited from its first token. For chat, check the model card for how
to turn thinking off, or replies will be slow. Not checked here.

## A plan for the owner's laptop

1. Install llama.cpp. Take the Windows CPU build from the llama.cpp releases
   page on GitHub: a zip named like `llama-bNNNN-bin-win-cpu-x64.zip`, unzipped
   to `C:/tools/llama`.
2. On a computer with internet, download two or three GGUF files from the table
   (for example Qwen3.5 4B and Qwen3.5 9B, both Q4_K_M) into `C:/models`.
   NovaCore never downloads anything.
3. For each candidate, copy `novacore/config.example.toml` and point `[router]`
   at it. The commented llama.cpp lines there are the starting point. They are
   UNVERIFIED: the flags were read from llama.cpp's source, not run.
4. Score each one:

       $env:PYTHONPATH = "novacore/src"
       py -3 novacore/eval/run_eval.py --config novacore/config-qwen4b.toml --transcript qwen4b.n3.jsonl

   Try `--limit 5` first: if the runner fails, the eval says "RUN INVALID"
   instead of scoring.
5. Choose among the routers whose result says **GATE MET**. Take the one with
   the most commands right and a speed you can live with by voice. A router that
   answers chat to everything also "meets" the gate, so read the right counts.
   Commit the transcripts: they hold the hand-written set's lines and the
   model's answers, none of your own words. Committed transcripts are what
   closes N3.
6. Only for a router that met the gate on your laptop: consider
   `confirm_all_actions = false`. Until then, keep it `true`.

## Sources

Found by web search on 2026-11-02. They are secondary pages (round-ups,
aggregators, quantisers' repositories) unless marked otherwise. They were not
fetched from the environment that wrote this, except the llama.cpp pull
request.

- Round-ups for 16 GB and CPU-only: https://modelgrep.com/best-local-llm/16gb-ram,
  https://atomic.chat/blog/guides/best-local-llm-16gb,
  https://www.popularai.org/p/best-cpu-only-local-llm-2026,
  https://www.promptquorum.com/local-llms/local-llm-on-laptop (speed estimate)
- Qwen3.5 small models (release, licence):
  https://artificialanalysis.ai/articles/qwen3-5-small-models
- Qwen3.5 GGUF listings: https://huggingface.co/bartowski/Qwen_Qwen3.5-4B-GGUF,
  https://huggingface.co/SoAIHQ/Qwen3.5-4B-GGUF, https://huggingface.co/AtomicChat/Qwen3.5-4B-GGUF,
  https://huggingface.co/bartowski/Qwen_Qwen3.5-9B-GGUF, https://huggingface.co/SoAIHQ/Qwen3.5-9B-GGUF,
  https://huggingface.co/AtomicChat/Qwen3.5-9B-GGUF
- Gemma 4 (sizes, licence, date): https://9to5google.com/2026/04/02/google-gemma-4/,
  https://the-decoder.com/googles-gemma-4-is-now-available-with-apache-2-0-licensing-for-the-first-time/;
  E4B GGUF sizes: https://huggingface.co/AtomicChat/gemma4-e4b-it-GGUF,
  https://huggingface.co/0x3/google_gemma-4-E4B-it-GGUF
- gpt-oss-20b (memory, licence): https://lmstudio.ai/models/gpt-oss,
  https://huggingface.co/openai/gpt-oss-20b/blob/main/LICENSE
- Phi-4-mini-instruct (licence, size): https://www.promptlayer.com/models/phi-4-mini-instruct-gguf
- llama.cpp `-jf` / `--json-schema-file`, **read directly**:
  https://github.com/ggml-org/llama.cpp/pull/12011. Its help text reads: "File
  containing a JSON schema to constrain generations". The pull request does not
  say which tools take the flag.
