#!/usr/bin/env bash
# Daily end-of-day SELF-LEARNING sidecar.
#
# Once per calendar day, after DAILY_SELF_IMPROVE_HOUR (local), it:
#   1. writes a deterministic end-of-day ANALYSIS of the day's results
#      (journal-stats + per-category / entry-price breakdown) to a log, and
#   2. runs the FENCED Claude-CLI self-tuner (scripts/auto_improve.py), which
#      proposes a small EXIT/SIZING delta on the live grinder profile, runs the
#      full test suite, opens a PR and arms auto-merge once CI is green.
#
# Hard safety properties (do not weaken):
#   * EVERYTHING runs inside try/catch (set +e + traps) — this sidecar can NEVER
#     crash or stall the live trade loop. Every failure is logged and swallowed.
#   * The tuner's own fences stay intact: ENTRY filters are FROZEN, a stop-loss
#     can NEVER be introduced, only configs/profiles/grinder.toml is writable,
#     tests must pass before a PR, CI must be green before merge.
#   * The git branch is ALWAYS restored to where it started — the live repo is
#     never left on an auto/ branch (the running bot keeps its loaded code; the
#     tuned config only takes effect on the next manual restart).
#   * Runs at most once per day (state file data/.last_self_improve).
#
# Toggle: DAILY_SELF_IMPROVE=1 (default on). Set to 0 to disable.
# This is the offline, opt-in LLM exception — it never runs in the trade loop.

set +e  # never abort the sidecar; we handle every error ourselves.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || exit 0

ENABLED="${DAILY_SELF_IMPROVE:-1}"
HOUR="${DAILY_SELF_IMPROVE_HOUR:-23}"          # local hour to run after (0-23)
CHECK_SECONDS="${DAILY_SELF_IMPROVE_CHECK_SECONDS:-1800}"  # poll cadence
STATE="$REPO_ROOT/data/.last_self_improve"
LOG_DIR="$REPO_ROOT/data/logs"
mkdir -p "$LOG_DIR"

log() { echo "[self-improve] $(date '+%F %T') $*" | tee -a "$LOG_DIR/self_improve_$(date +%Y-%m-%d).log"; }

if [[ "$ENABLED" != "1" ]]; then
    log "disabled (DAILY_SELF_IMPROVE=$ENABLED) — sidecar idle."
    exit 0
fi

# Deterministic end-of-day analysis (read-only, never fails the run).
analyse() {
    local out="$LOG_DIR/analysis_$(date +%Y-%m-%d).log"
    log "writing end-of-day analysis -> $out"
    {
        echo "===== END-OF-DAY ANALYSIS $(date '+%F %T') ====="
        uv run pmbot journal-stats 2>&1
        echo "----- per-category / entry-price breakdown -----"
        uv run python scripts/auto_improve.py --analyze-only 2>&1
    } >>"$out" 2>&1 || log "analysis step errored (ignored)."
}

# Fenced Claude self-tuner, with guaranteed branch restoration.
self_tune() {
    local start_ref
    start_ref="$(git rev-parse --abbrev-ref HEAD 2>/dev/null)"
    log "running fenced self-tuner (start branch: ${start_ref:-unknown})"
    (
        AUTO_IMPROVE_ENABLED=1 \
        AUTO_IMPROVE_USE_LLM="${AUTO_IMPROVE_USE_LLM:-1}" \
        AUTO_IMPROVE_AUTOMERGE="${AUTO_IMPROVE_AUTOMERGE:-1}" \
        uv run python scripts/auto_improve.py
    ) >>"$LOG_DIR/self_improve_$(date +%Y-%m-%d).log" 2>&1
    local rc=$?
    # ALWAYS return to the starting branch — auto_improve leaves the repo on the
    # auto/ branch it created. Never leave the live repo off its branch.
    if [[ -n "$start_ref" && "$start_ref" != "HEAD" ]]; then
        git checkout "$start_ref" >/dev/null 2>&1 || log "branch restore to $start_ref failed (manual check advised)."
        git pull --ff-only >/dev/null 2>&1 || true
    fi
    log "self-tuner finished (rc=$rc), branch restored to ${start_ref:-unknown}."
}

run_once() {
    analyse
    self_tune
    date +%Y-%m-%d >"$STATE"
    log "daily self-improve complete for $(date +%Y-%m-%d)."
}

log "sidecar up — daily run after ${HOUR}:00 local, poll ${CHECK_SECONDS}s."
while true; do
    today="$(date +%Y-%m-%d)"
    last="$(cat "$STATE" 2>/dev/null || echo "")"
    hour_now="$(date +%-H)"
    if [[ "$last" != "$today" && "$hour_now" -ge "$HOUR" ]]; then
        log "trigger: hour ${hour_now} >= ${HOUR}, last run '${last:-never}'."
        run_once
    fi
    sleep "$CHECK_SECONDS"
done
