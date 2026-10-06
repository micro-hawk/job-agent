#!/bin/zsh
export PATH="/opt/homebrew/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
source ~/.zshrc >/dev/null 2>&1
cd "$(dirname "$0")/.." || exit 1
echo "=== $(date) ==="
uv run jobagent run
uv run jobagent apply
uv run jobagent instahyre || true
uv run jobagent referrals || true
