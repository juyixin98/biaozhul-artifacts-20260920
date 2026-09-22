#!/usr/bin/env bash
# Prepare *all* runtime NLP dependencies for an air-gapped Docker build.
#
# Run this ONCE on a machine with internet access, then copy the whole
# repository (including the new vendor/ directory) to the offline host:
#
#   scripts/download_offline_deps.sh
#   # offline host:
#   OFFLINE_BUILD=1 docker compose build
#   docker compose up -d
#
# vendor/wheels    — every Python package (pip download, full transitive set)
# vendor/models    — the en_core_web_sm spaCy pipeline wheel
# vendor/nltk_data — NLTK punkt / punkt_tab / stopwords, laid out as NLTK
#                    expects (tokenizers/ and corpora/ subdirectories)
set -euo pipefail

SPACY_MODEL="en_core_web_sm-3.7.1"
PYTHON_BIN="${PYTHON_BIN:-python3}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENDOR="${ROOT_DIR}/vendor"

mkdir -p "${VENDOR}/wheels" "${VENDOR}/models" "${VENDOR}/nltk_data"

echo "==> [1/3] Downloading Python wheels (full transitive dependency set)"
"${PYTHON_BIN}" -m pip download \
  -r "${ROOT_DIR}/requirements.txt" \
  -d "${VENDOR}/wheels"

echo "==> [2/3] Downloading spaCy model wheel (${SPACY_MODEL})"
if ! compgen -G "${VENDOR}/models/${SPACY_MODEL}*.whl" > /dev/null; then
  "${PYTHON_BIN}" -m pip download --no-deps \
    "https://github.com/explosion/spacy-models/releases/download/${SPACY_MODEL}/${SPACY_MODEL}-py3-none-any.whl" \
    -d "${VENDOR}/models"
fi

echo "==> [3/3] Downloading NLTK data zipfiles and unpacking them"
"${PYTHON_BIN}" - "${VENDOR}/nltk_data" <<'PY'
import os
import sys
import urllib.request
import zipfile

target = sys.argv[1]
base = "https://raw.githubusercontent.com/nltk/nltk_data/gh-pages/packages"
packages = [
    ("punkt", "tokenizers"),
    ("punkt_tab", "tokenizers"),
    ("stopwords", "corpora"),
]
for name, subdir in packages:
    out_dir = os.path.join(target, subdir)
    final = os.path.join(out_dir, name)
    if os.path.isdir(final):
        print(f"  {name}: already present")
        continue
    os.makedirs(out_dir, exist_ok=True)
    zip_path = os.path.join(target, f"{name}.zip")
    print(f"  downloading {name}")
    urllib.request.urlretrieve(f"{base}/{subdir}/{name}.zip", zip_path)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(out_dir)
    os.remove(zip_path)
PY

echo
echo "Offline bundle ready in ${VENDOR}"
du -sh "${VENDOR}"/* 2>/dev/null || true
