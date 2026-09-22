#!/usr/bin/env bash
# 离线依赖准备（在有网络的机器上执行）：
#   bash scripts/vendor_offline.sh
# 产出：
#   offline/wheels/          —— 全部 Python 包 + spaCy 模型 wheel
#   offline/requirements.lock —— 锁定版本清单
#   offline/nltk_data/       —— NLTK stopwords/punkt 数据包
# 离线机器上：
#   pip install --no-index --find-links offline/wheels -r requirements.txt
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p offline/wheels

python3 -m pip download \
  -r requirements.txt \
  -d offline/wheels

SPACY_MODEL_URL="https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.7.1/en_core_web_sm-3.7.1-py3-none-any.whl"
python3 -m pip download "$SPACY_MODEL_URL" -d offline/wheels

python3 -m pip freeze > offline/requirements.lock

python3 - <<'PY'
import nltk
nltk.download("stopwords", download_dir="offline/nltk_data", quiet=True)
nltk.download("punkt_tab", download_dir="offline/nltk_data", quiet=True)
nltk.download("punkt", download_dir="offline/nltk_data", quiet=True)
print("nltk data vendored -> offline/nltk_data")
PY

# 只保留英文资源（默认数据包含全部语言），离线目录控制在 ~1MB
python3 - <<'PY'
import pathlib, shutil
base = pathlib.Path("offline/nltk_data")
keep = {
    base / "corpora/stopwords": {"english", "README"},
    base / "tokenizers/punkt_tab": {"english", "README"},
    base / "tokenizers/punkt": {"english.pickle", "PY3", "README"},
}
for d, names in keep.items():
    if not d.exists():
        continue
    for p in d.iterdir():
        if p.name not in names:
            shutil.rmtree(p) if p.is_dir() else p.unlink()
    py3 = d / "PY3"
    if py3.exists():
        for p in py3.iterdir():
            if p.name != "english.pickle":
                p.unlink()
for z in base.rglob("*.zip"):
    z.unlink()
print("trimmed to English-only")
PY

echo "完成。离线部署时把整个 offline/ 目录随工程拷贝即可。"
