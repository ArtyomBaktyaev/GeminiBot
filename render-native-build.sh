#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
pip install -r requirements.txt
PLAYWRIGHT_BROWSERS_PATH=0 python -m playwright install --only-shell chromium
mkdir -p .tinytex
curl -fsSL --retry 3 https://yihui.org/tinytex/TinyTeX-1.tar.gz | tar -xz -C .tinytex --strip-components=1
.tinytex/bin/x86_64-linux/tlmgr install collection-langcyrillic cm-super parskip booktabs
