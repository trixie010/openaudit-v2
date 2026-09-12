#!/usr/bin/env bash
# Initialize a Category C fuzzing workspace for a target.
# RULE: recon-magic writes INTO the repo -> ALWAYS a throwaway clone. Never the working tree.
# Usage: ./init_fuzz_workspace.sh {repo_url} {protocol_slug} [{out_dir}]
set -euo pipefail
REPO_URL="$1"; SLUG="$2"; OUT="${3:-out}"
WS="$OUT/$SLUG/fuzzing"
WORK="$WS/target-work"
[ -d "$WORK" ] && { echo "workspace exists - rm $WORK manually if you mean it"; exit 1; }
mkdir -p "$WS"/{coverage,violations}
git clone --depth 1 "$REPO_URL" "$WORK"
rm -rf -- "$WORK/.git"  # orphan the clone: harness edits can never be pushed back upstream by mistake
mkdir -p "$WORK/magic"
cp skills/openaudit/templates/target-functions.sample.json "$WORK/target-functions.json"
cp "$OUT/$SLUG/SCOPE.md" "$WS/SCOPE.md" 2>/dev/null || echo "# scope: fill me" > "$WS/SCOPE.md"
touch "$WS/candidates-added.log"
echo "workspace ready: $WS"
echo "next: regenerate target-functions.json from kb/functions.csv, then run recon-magic"
echo "      setup/properties phases (day, connected), then the fuzz workflow (night, offline-capable)"
echo "      MANDATORY: chimera-test-linter pre-flight before the first long run."
