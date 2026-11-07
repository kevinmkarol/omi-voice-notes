#!/bin/zsh
# ABOUTME: One-shot setup on a Mac: installs whisper-cpp, libopus and uv, fetches the Whisper Medium and Silero VAD models,
# ABOUTME: seeds the vocabulary file and syncs the Python environment. Idempotent: every step is skipped when already done.
set -euo pipefail
here=${0:A:h}
models=~/Library/Application\ Support/omi-voice-notes
vocabulary=~/.config/omi-voice-notes/vocabulary.txt

if ! command -v brew >/dev/null; then
  echo "Homebrew is required: https://brew.sh" >&2
  exit 1
fi

command -v whisper-cli >/dev/null && echo "whisper-cli is installed" || brew install whisper-cpp
command -v uv >/dev/null && echo "uv is installed" || brew install uv
brew list --versions opus >/dev/null 2>&1 && echo "opus is installed" || brew install opus

mkdir -p $models ${vocabulary:h}

# fetch URL SHA256 FILE: downloads FILE unless it is already there and keeps it only when its checksum matches.
fetch() {
  local url=$1 sha=$2 file=$3
  if [[ -s $file ]]; then
    echo "$file is in place"
    return
  fi
  curl -fL --progress-bar -o $file.part $url
  if [[ $(shasum -a 256 $file.part | cut -d' ' -f1) != $sha ]]; then
    rm -f $file.part
    echo "$file: checksum mismatch, download discarded" >&2
    exit 1
  fi
  mv $file.part $file
  echo "downloaded $file"
}

# Whisper Medium is 1.5 GB. Set OMI_NOTES_MODELS (or OMI_NOTES_WHISPER_MODEL) to use a model kept elsewhere.
fetch https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-medium.bin \
  6c14d5adee5f86394037b4e4e8b59f1673b6cee10e3cf0b11bbdbee79c156208 $models/ggml-medium.bin
fetch https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-silero-v5.1.2.bin \
  29940d98d42b91fbd05ce489f3ecf7c72f0a42f027e4875919a28fb4c04ea2cf $models/ggml-silero-v5.1.2.bin

if [[ -e $vocabulary ]]; then
  echo "$vocabulary is in place"
else
  cat > $vocabulary <<'VOCAB'
# Names and terms whisper should know how to spell, one per line. Blank lines and # comments are skipped.
# Read before every transcription, so edits need no restart.
VOCAB
  echo "created $vocabulary (add names to it)"
fi

(cd $here && uv sync -q)
echo "ready: cd $here && uv run omi-scan"
