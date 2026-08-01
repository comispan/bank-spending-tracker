# Phase 0 spike

**Question this answers:** can we extract transactions from a statement PDF and *prove* the extraction is correct — and on which of your banks does it fail?

Nothing here is production code. Answer the question, throw it away, then build Phase 1 knowing what you're up against.

All commands below are Windows PowerShell.

## Run it on your machine, not in a cloud session

Statements are among the most sensitive documents you own. Keep them local.
`spike/statements/`, `spike/out/`, and `passwords.json` are all gitignored — check that before you copy anything in.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r spike\requirements.txt
```

If activation fails with *"running scripts is disabled on this system"*, PowerShell's execution policy is blocking it. Allow it for this window only — no permanent change to your machine:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

## Step 1 — smoke-test (no real data, no network)

```powershell
python spike\selftest.py          # checks encoding + the reconciliation gate
python spike\make_sample.py       # writes a synthetic statement
python spike\extract.py --dry-run
```

Confirms the plumbing works before you point it at anything real. `selftest.py`
needs no API key and touches no statements; run it first if anything misbehaves,
since it isolates a broken environment from a bad parse.

### A note on text encoding

All output is written as UTF-8 with LF endings on every platform, via the
`write_text`/`read_text` helpers in `extract.py`. Nothing calls Python's
`Path.read_text`/`write_text` directly — those inherit the platform's locale
encoding (cp1252 on Windows), which cannot represent characters that genuinely
appear in statements, and the failure is a mid-run crash. `selftest.py` audits
the source for that mistake, so a future edit that reintroduces it fails loudly.

Files we *don't* write — currently just `passwords.json` — are decoded
tolerantly (UTF-8, UTF-8-with-BOM, then cp1252), so it doesn't matter which
editor you used to create it. Notepad's BOM is handled.

## Step 2 — text extraction on your statements (still no network)

Drop 5–10 real PDFs into `spike\statements\`, then:

```powershell
python spike\extract.py --dry-run
```

This **sends nothing anywhere.** It writes each statement's extracted text to `spike\out\<name>.txt`. Open a few and check whether the transaction table survived — columns aligned, amounts on the right row, dates intact.

```powershell
notepad spike\out\dbs.txt
```

This step alone is worth doing carefully. If the text comes out scrambled or empty, no amount of LLM cleverness downstream will fix it, and you've learned that for free.

The `rows` column counts lines that start with a date and end with an amount — a rough count of surviving transaction rows. `TABLE-OK` means the table structure made it through; `NO-TABLE` means inspect the `.txt` before spending any API calls on that bank.

### Encrypted statements

Many issuers encrypt statements only to set permission flags (no printing, no
copying) and leave the user password *empty* — DBS does this. Those open with no
password at all, and the tool tries that automatically before asking for one.

If a statement is genuinely locked, create `spike\passwords.json`:

```json
{ "uob-jun-2026.pdf": "S1234567A", "ocbc-jun-2026.pdf": "0512" }
```

## Step 3 — full pipeline

Per page: text → model → structured JSON → merged → **reconciled against the statement's own printed totals.**

Card numbers are masked to last-4 before any text leaves the machine (`redact()` in `extract.py`). If you use a hosted provider, the rest of the page — merchants, amounts, dates — does go to that API. That's the tradeoff this step is testing. Running locally (below) avoids it entirely.

### Option A — local, free, nothing leaves your machine

The best fit for financial data, and the one to try first on a 32GB Windows box:

```powershell
ollama pull qwen3.6:35b-a3b

$env:SPIKE_PROVIDER = "ollama"
$env:SPIKE_MODEL    = "qwen3.6:35b-a3b"
python spike\extract.py
```

Uses Ollama's native `/api/chat` with its `format` parameter, which constrains
token *generation* to the schema — malformed JSON becomes mechanically
impossible rather than merely discouraged.

⚠️ Ollama does **not** accept OpenAI's `response_format: {type: "json_schema"}`
on its `/v1` endpoint, so don't try to reach it with `SPIKE_PROVIDER=openai`.
You'd get unconstrained output and conclude local models can't do this.

**Prove the pipeline on a small model first.** A 20GB model on a 32GB machine
is the worst thing to debug against — you can't tell a wrong setting from slow
inference. Get a green run on something fast, then scale up:

```powershell
ollama pull qwen3.5:9b
$env:SPIKE_MODEL = "qwen3.5:9b"
python spike\extract.py --file spike\statements\sample-statement.pdf
```

That's ~6GB and one synthetic page. If it reconciles, the plumbing is right and
any later failure is about model capability.

#### Thinking is off by default, deliberately

Ollama 0.12+ auto-enables thinking on thinking-capable models, and Qwen 3.x is
one. For schema-constrained extraction that is pure cost: the model writes a
long reasoning trace before emitting JSON it was going to be forced into
anyway. Minutes per page instead of seconds.

The harness sends `think: false`. To measure the difference yourself:

```powershell
$env:SPIKE_THINK = "true"
```

(Older Ollama builds don't know the field; the harness detects the rejection
and retries without it rather than failing the run.)

#### Timeouts and context

| Variable | Default | Raise it when |
|---|---|---|
| `SPIKE_TIMEOUT` | 1800 (30 min per page) | The model is genuinely slow rather than stuck |
| `SPIKE_NUM_CTX` | 16384 | Statement pages are long |

`SPIKE_NUM_CTX` matters more than it looks. Ollama's own default is much
smaller, and it drops overflow from the *front* of the prompt silently — which
looks identical to the model missing transactions rather than a config problem.

```powershell
$env:SPIKE_TIMEOUT = "3600"
$env:SPIKE_NUM_CTX = "32768"
```

If a page times out, check whether it's swapping before raising anything —
`ollama ps` shows resident size. If that's near your free RAM, the answer is a
smaller model, not a longer timeout.

#### Picking a local model (32GB RAM)

Qwen 3.5's GGUFs currently don't load in Ollama (they ship separate `mmproj`
vision files). **Qwen 3.6 is in the official Ollama library and is the one to
use**; 3.5 needs llama.cpp directly.

| Model | Size at Q4_K_M | Notes |
|---|---|---|
| `qwen3.6:35b-a3b` | ~20 GB | **Start here.** Mixture-of-experts, only ~3B parameters active per token, so it's far faster than its size suggests — the difference between usable and unusable on CPU. |
| `qwen3.6:27b` | ~16 GB | Dense. Fits with more headroom but every parameter runs on every token, so it's slower despite being smaller. |
| `qwen3.5:9b` | ~6 GB | Fallback if the above thrash. Expect weaker adherence on a strict schema. |

Q5_K_M or higher is generally better for structured extraction, but 35B at Q5
is ~25GB — tight on a 32GB machine with Windows and a browser running. Start at
Q4_K_M and only move up if reconciliation is marginal.

Check what's actually loaded and how much RAM it's using:

```powershell
ollama list
ollama ps
```

### Option B — Anthropic (the accuracy baseline)

```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-..."
python spike\extract.py
```

`$env:` lasts only for the current PowerShell window. To persist it for your user account:

```powershell
[Environment]::SetEnvironmentVariable("ANTHROPIC_API_KEY", "sk-ant-...", "User")
```

Reopen PowerShell afterwards for it to take effect. Don't put the key in a file inside the repo.

**Getting a key:** console.anthropic.com → Settings → API Keys → Create Key. The API is **billed separately from a Claude Pro/Max subscription** — a subscription includes no API credits, which is the most common surprise. Add a payment method and buy credits before the first run.

### Option C — hosted, free or cheap

```powershell
$env:SPIKE_PROVIDER = "openai"
$env:SPIKE_BASE_URL = "https://api.groq.com/openai/v1"
$env:SPIKE_API_KEY  = "gsk_..."
$env:SPIKE_MODEL    = "llama-3.3-70b-versatile"
python spike\extract.py
```

```powershell
$env:SPIKE_PROVIDER = "openai"
$env:SPIKE_BASE_URL = "https://openrouter.ai/api/v1"
$env:SPIKE_API_KEY  = "sk-or-..."
$env:SPIKE_MODEL    = "deepseek/deepseek-r1:free"
python spike\extract.py
```

⚠️ **Read the data terms before pointing a free tier at real statements.** Free tiers are frequently free because inputs are retained or used for training. That is a bad trade for a document listing everywhere you spend money. Local, or a paid tier with no-training terms, is the safer default here.

Cheapest paid: DeepSeek and Gemini Flash-Lite land near $0.10–0.30 per million input tokens, roughly 20–50× below Opus 5.

### Switching back, and clearing variables

Environment variables persist for the life of the window, so a leftover
`SPIKE_PROVIDER` will silently send the next run somewhere you didn't intend.
Clear them when switching:

```powershell
Remove-Item Env:SPIKE_PROVIDER, Env:SPIKE_MODEL, Env:SPIKE_BASE_URL, Env:SPIKE_API_KEY -ErrorAction SilentlyContinue
```

The run prints which model and endpoint it used on the first line — check it matches what you meant.

### Provider settings reference

| Variable | Meaning |
|---|---|
| `SPIKE_PROVIDER` | `anthropic` (default) \| `ollama` \| `openai` |
| `SPIKE_MODEL` | model id |
| `SPIKE_BASE_URL` | endpoint (required for `openai`; defaults to `http://localhost:11434` for `ollama`) |
| `SPIKE_API_KEY` | key for the `openai` backend |
| `SPIKE_NUM_CTX` | Ollama context window, default 16384 |
| `SPIKE_TIMEOUT` | Ollama per-page timeout in seconds, default 1800 |
| `SPIKE_THINK` | `true` to allow thinking on Ollama (default off) |

The reconciliation gate is identical across providers, so "is the cheap model good enough?" stops being a guess. Run the same statements through each and compare PASS counts.

### What Anthropic costs, if you use it

Runs on `claude-opus-5` by default. For 5 statements at ~4 pages each, expect **well under $2** for the whole spike — roughly 3K input / 1.5K output tokens per page at Opus 5's $5/$25 per million:

| Model | Per page | 20 pages |
|---|---|---|
| `claude-opus-5` (default) | ~$0.05 | ~$1.05 |
| `claude-sonnet-5` | ~$0.02 | ~$0.42 |

Run the default first. The spike is asking *whether extraction is possible at all*, so a failure should mean the approach is hard — not that you economized on the model. Once it passes:

```powershell
$env:SPIKE_MODEL = "claude-sonnet-5"
python spike\extract.py
```

## Reading the output

```
statement              pages  rows  verdict     detail
dbs-jun-2026.pdf           4    47  PASS        matches printed totals
ocbc-jun-2026.pdf          3    31  PASS        balance rolls forward (card convention)
citi-jun-2026.pdf          6    52  FAIL        debits off by 412.00
amex-jun-2026.pdf          2     0  ERROR       encrypted (supplied password rejected)
uob-scan.pdf               5     0  UNVERIFIED  statement prints no totals to check against
                                 !  5/5 page(s) have no text layer — needs OCR
```

In `--dry-run` the verdict is `TABLE-OK` or `NO-TABLE` instead, since nothing
has been parsed yet.

| Verdict | Meaning |
|---|---|
| `PASS` | Numbers reconcile. Trustworthy. |
| `FAIL` | Extraction is wrong, or the statement has a quirk the parser missed. **Investigate every one** — the `detail` delta often names the missing transaction outright. |
| `UNVERIFIED` | Parsed, but the statement prints no totals to check against. Not the same as correct. |
| `ERROR` | Couldn't read the file at all. |

## What the result means

- **Most statements PASS** → the design in `../DESIGN.md` holds. Go to Phase 1.
- **Systematic FAILs at one bank** → look at that bank's `out\*.txt`. Usually a layout the text extractor mangles, or a section (fees, FX sublines, instalments) not being counted. Cheap to fix.
- **FAILs scattered everywhere** → the LLM-first approach is too loose. Fall back to per-bank template parsers, and expect real work per bank.
- **Mostly `UNVERIFIED`** → the trust gate can't function. This is the worst outcome, because it means you can never distinguish a good parse from a bad one automatically. Revisit before building further.

Record the verdict table in the PR or an issue. It's the input to every Phase 1 decision.

## Comparing providers

Because the reconciliation gate is the same everywhere, provider choice becomes
a measurement rather than a guess. Run the same statements through each and
compare:

```
statement      opus-5    qwen3.6:35b-a3b
dbs.pdf        PASS      PASS      <- local is good enough, use it
uob.pdf        PASS      FAIL      <- this bank needs the better model
```

If local passes everywhere, you're done and it costs nothing from here on.
