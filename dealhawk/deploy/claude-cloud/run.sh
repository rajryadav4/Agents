#!/usr/bin/env bash
# One DealHawk pass inside a Claude Code cloud session (fired by a scheduled task).
#
# Cloud containers are fresh every run, so the SQLite state (price history,
# alerts already sent, the learned model) is restored from — and saved back to —
# a dedicated git branch. Secrets come from the cloud environment's variables.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
APP="$(cd "$HERE/../.." && pwd)"                # the dealhawk/ project dir
REPO="$(git -C "$APP" rev-parse --show-toplevel)"
STATE_BRANCH="${DEALHAWK_STATE_BRANCH:-dealhawk-state}"
CONFIG="${DEALHAWK_CONFIG:-$HERE/dealhawk.cloud.yaml}"
WORK="$(mktemp -d)"

pip install -q -e "$APP[claude]"

# 1. restore state
if git -C "$REPO" fetch -q origin "$STATE_BRANCH" 2>/dev/null; then
  git -C "$REPO" show "origin/$STATE_BRANCH:dealhawk.db" > "$WORK/dealhawk.db" 2>/dev/null || rm -f "$WORK/dealhawk.db"
fi
export DEALHAWK_DB="$WORK/dealhawk.db"

# 2. one pass over every source (alerts go out as they're found)
dealhawk once -c "$CONFIG" || echo "dealhawk pass reported errors (continuing to save state)"

# 3. save state back to the state branch (never touches your code branches)
python3 -c "import sqlite3,sys; sqlite3.connect(sys.argv[1]).execute('PRAGMA wal_checkpoint(TRUNCATE)')" "$DEALHAWK_DB" 2>/dev/null || true
STATE_TREE="$(mktemp -d)"
git -C "$REPO" worktree add -q --detach "$STATE_TREE"
(
  cd "$STATE_TREE"
  git checkout -q --orphan "state-$$" && git rm -rq --cached . >/dev/null 2>&1 || true
  find . -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
  cp "$DEALHAWK_DB" dealhawk.db
  git add dealhawk.db
  git -c user.name="DealHawk" -c user.email="dealhawk@localhost" commit -qm "DealHawk state $(date -u +%FT%TZ)"
  git push -qf origin "HEAD:refs/heads/$STATE_BRANCH"
)
git -C "$REPO" worktree remove --force "$STATE_TREE"
git -C "$REPO" branch -qD "state-$$" 2>/dev/null || true
echo "state saved to $STATE_BRANCH"
