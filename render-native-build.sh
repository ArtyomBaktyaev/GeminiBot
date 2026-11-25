#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
pip install -r requirements.txt
PLAYWRIGHT_BROWSERS_PATH=0 python -m playwright install --only-shell chromium
mkdir -p .tinytex
curl -fL --retry 3 --output /tmp/geminibot-tinytex.tar.xz https://github.com/rstudio/tinytex-releases/releases/download/v2026.11/TinyTeX-1-linux-x86_64-v2026.11.tar.xz
tar -xJf /tmp/geminibot-tinytex.tar.xz -C .tinytex --strip-components=1
"$(pwd -P)/.tinytex/bin/x86_64-linux/tlmgr" install collection-langcyrillic cm-super parskip booktabs
