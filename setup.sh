#!/usr/bin/env bash
# macOS / Linux setup.  Run:  bash setup.sh
set -euo pipefail

echo "Net of Fees - setup (macOS/Linux)"
echo

PY=""
for cand in python3.12 python3.11 python3; do
  if command -v "$cand" >/dev/null 2>&1; then
    ver=$("$cand" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')
    major=${ver%%.*}; minor=${ver##*.}
    # 3.13+ has no wheels yet for the audio stack; 3.10 is the floor.
    if [ "$major" -eq 3 ] && [ "$minor" -ge 10 ] && [ "$minor" -le 12 ]; then
      PY="$cand"; break
    fi
  fi
done

if [ -z "$PY" ]; then
  echo "ERROR: need Python 3.10, 3.11 or 3.12."
  echo "  macOS:  brew install python@3.11"
  echo "  Linux:  sudo apt install python3.11 python3.11-venv"
  exit 1
fi
echo "  python     $($PY --version)"

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo
  echo "ERROR: FFmpeg not found."
  echo "  macOS:  brew install ffmpeg"
  echo "  Linux:  sudo apt install ffmpeg"
  exit 1
fi
echo "  ffmpeg     $(ffmpeg -version | head -1 | cut -d' ' -f3)"

echo
echo "  creating .venv ..."
"$PY" -m venv .venv
.venv/bin/python -m pip install --quiet --upgrade pip
echo "  installing requirements ..."
.venv/bin/python -m pip install --quiet -r requirements.txt

[ -f .env ] || { cp .env.example .env; echo "  created .env from .env.example"; }

echo
.venv/bin/python run.py doctor
echo "Next:"
echo "  .venv/bin/python run.py new --recipe fee_drag"
echo "  .venv/bin/python run.py 001"
