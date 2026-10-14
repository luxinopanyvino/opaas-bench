#!/usr/bin/env bash
# One command from a fresh clone to a verified certificate:
#
#   uv → datasets + classifiers (first time only) → run (blocks A–E)
#      → keygen (first time only) → sign → verify → printable certificate
#      → static site
#
# Runs on Linux, macOS, and Windows under Git Bash (or WSL). uv is installed if
# missing, and uv in turn fetches a Python if the machine has none, so the only
# prerequisite is bash plus an inference server already serving the model:
# OPAAS evaluates a deployment, it never starts one.
#
# Configuration, all optional, as environment variables:
#
#   OPAAS_MODEL     model id exactly as the backend serves it  (llama3.2:1b)
#   OPAAS_BACKEND   vllm | ollama | openai                      (ollama)
#   OPAAS_BASE_URL  server endpoint                             (adapter's own)
#   OPAAS_NUM_CTX   context window Ollama must serve               (8192 on Ollama)
#                   Without it Ollama uses its own default, about 2048 tokens,
#                   and silently drops the start of longer prompts: block C
#                   confirmed exactly that on a real run.
#   OPAAS_DATASET   datasets to evaluate                   (ifeval,xstest,squad2,bbq)
#                   squad2 adds passages with unanswerable questions, which is
#                   what measures hallucination; bbq adds 1,800 social-bias
#                   questions across 9 categories; a local path composes your own.
#   OPAAS_CLASSIFIERS  1 installs and fetches the block D classifiers
#                   (toxicity, faithfulness), 0 skips them         (1)
#                   It pulls in torch: several hundred MB to a few GB the
#                   first time, depending on the platform.
#   OPAAS_KEY_PASSPHRASE  passphrase of the signing key; asked for if unset
#
# Any argument is passed through to `opaas run`, e.g.
#
#   OPAAS_MODEL=qwen2.5:7b ./start.sh --suite-concurrency 8 --quantization "GGUF Q4_K_M"

set -euo pipefail

cd "$(dirname "$0")"

MODEL="${OPAAS_MODEL:-llama3.2:1b}"
BACKEND="${OPAAS_BACKEND:-ollama}"
BASE_URL="${OPAAS_BASE_URL:-}"
NUM_CTX="${OPAAS_NUM_CTX:-}"
[ -z "$NUM_CTX" ] && [ "$BACKEND" = "ollama" ] && NUM_CTX=8192
DATASET="${OPAAS_DATASET:-ifeval,xstest,squad2,bbq}"
CLASSIFIERS="${OPAAS_CLASSIFIERS:-1}"

KEY_DIR="keys"
PRIVATE_KEY="$KEY_DIR/opaas-root-ed25519.pem"
PUBLIC_KEY="$KEY_DIR/opaas-root-ed25519.pub.pem"
DID_DOCUMENT="$KEY_DIR/did.json"
# One ledger for every run on this machine: the hash chain is only worth
# something if issuances accumulate in it rather than starting over each time.
LEDGER="runs/ledger.jsonl"
OUT_DIR="runs/start-$(date +%Y%m%d-%H%M%S)"

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
fail() { printf '\n\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

is_windows() {
    case "$(uname -s)" in MINGW* | MSYS* | CYGWIN*) return 0 ;; *) return 1 ;; esac
}

# --- uv ---------------------------------------------------------------------

# The CLI prints Unicode (▲, ✓, block bars). A Windows console defaults to
# cp1252, where Rich crashes on them halfway through printing the summary.
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8

step "Checking uv"
if ! command -v uv >/dev/null 2>&1; then
    echo "uv not found, installing it for this user."
    if is_windows; then
        powershell.exe -NoProfile -ExecutionPolicy ByPass -Command \
            "irm https://astral.sh/uv/install.ps1 | iex"
    elif command -v curl >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- https://astral.sh/uv/install.sh | sh
    else
        fail "Neither curl nor wget is available. Install uv by hand: https://docs.astral.sh/uv/"
    fi
    # The installer edits the shell profile, which this shell has already read.
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    command -v uv >/dev/null 2>&1 \
        || fail "uv was installed but is not on PATH. Open a new terminal and run ./start.sh again."
fi
uv --version

step "Installing OPAAS exactly as uv.lock pins it"
# --locked refuses to run if uv.lock drifted from pyproject.toml: a run is only
# reproducible by a third party if it used the dependencies the lock records.
# --inexact keeps what is already installed but not requested here (the dev
# extra), instead of uninstalling a developer's tools.
sync_args=(--locked --inexact)
[ "$CLASSIFIERS" = "1" ] && sync_args+=(--extra classifiers)
uv sync "${sync_args[@]}"

opaas() { uv run --locked --no-sync opaas "$@"; }

# --- Datasets and classifiers -------------------------------------------------

# Everything is fetched up front, verified against its pinned hash, and the run
# then goes --offline. Finding out a download fails, or a classifier is
# missing, must happen here, not an hour into the evaluation. `fetch` skips
# whatever is already cached, so on later runs this step is instant.
step "Fetching datasets$( [ "$CLASSIFIERS" = "1" ] && echo " and classifiers" ) (first time only)"
opaas fetch

# --- Evaluation ---------------------------------------------------------------

mkdir -p "$OUT_DIR"
REPORT="$OUT_DIR/report.json"
CREDENTIAL="$OUT_DIR/credential.json"

run_args=(--model "$MODEL" --backend "$BACKEND" --suite all --dataset "$DATASET" --offline
          --output "$REPORT")
[ -n "$BASE_URL" ] && run_args+=(--base-url "$BASE_URL")
[ -n "$NUM_CTX" ] && run_args+=(--num-ctx "$NUM_CTX")

step "Evaluating $MODEL on $BACKEND, blocks A–E"
run_status=0
opaas run "${run_args[@]}" "$@" || run_status=$?
[ -f "$REPORT" ] \
    || fail "The run did not produce a report. Check that the server is serving '$MODEL'."

# The exit code alone does not say whether the report is whole: a crash while
# printing the summary also exits non-zero, after the report is written. The
# report's own status is the authority.
report_status="$(uv run --locked --no-sync python -c \
    'import json, sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["status"])' \
    "$REPORT")"
# A partial report (a block failed) is not signed here: certifying it should be
# a decision someone takes after reading why the block failed, not a side
# effect of a convenience script.
if [ "$report_status" = "partial" ]; then
    fail "The run finished as partial: a block failed. Read it with
       uv run opaas show $REPORT
       and sign it by hand if it is still worth certifying."
fi
if [ "$run_status" -ne 0 ]; then
    echo "opaas run exited with code $run_status, but the report is complete (status: $report_status)."
fi

# --- Signing key --------------------------------------------------------------

# Read the passphrase here rather than letting Python prompt for it: Python's
# hidden prompt misbehaves in Git Bash's terminal on Windows.
if [ -z "${OPAAS_KEY_PASSPHRASE:-}" ]; then
    [ -t 0 ] || fail "No terminal to ask for the key passphrase. Set OPAAS_KEY_PASSPHRASE."
    read -r -s -p "Signing key passphrase: " OPAAS_KEY_PASSPHRASE
    echo
    [ -n "$OPAAS_KEY_PASSPHRASE" ] || fail "The passphrase cannot be empty."
    if [ ! -f "$PRIVATE_KEY" ]; then
        read -r -s -p "Repeat it: " confirm
        echo
        [ "$OPAAS_KEY_PASSPHRASE" = "$confirm" ] || fail "The passphrases do not match."
    fi
fi
export OPAAS_KEY_PASSPHRASE

if [ ! -f "$PRIVATE_KEY" ]; then
    step "Generating the root signing key in $KEY_DIR/ (first time only)"
    opaas keygen --out "$KEY_DIR"
    echo "This key is your root of trust. Back it up and keep it out of git;"
    echo "see docs/key-custody.md."
fi

# --- Certificate --------------------------------------------------------------

step "Signing the report"
opaas sign --input "$REPORT" --key "$PRIVATE_KEY" --out "$CREDENTIAL" --log "$LEDGER"

step "Verifying the credential, offline"
# The public key is passed explicitly: verify never trusts the key a
# credential carries about itself.
opaas verify --credential "$CREDENTIAL" --report "$REPORT" --key "$PUBLIC_KEY" --log "$LEDGER"

step "Rendering the printable certificate"
opaas certificate --report "$REPORT" --credential "$CREDENTIAL" --key "$PUBLIC_KEY" \
    --out "$OUT_DIR/certificate.html"

step "Generating the static certificate site"
opaas publish --report "$REPORT" --credential "$CREDENTIAL" \
    --did-document "$DID_DOCUMENT" --out "$OUT_DIR/site"

step "Done"
cat <<EOF
  Report       $REPORT
  Credential   $CREDENTIAL
  Certificate  $OUT_DIR/certificate.html   (open it and print to PDF)
  Site         $OUT_DIR/site/               (cd there and: uv run python -m http.server 8000)
  Ledger       $LEDGER
EOF
