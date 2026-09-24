#!/usr/bin/env bash
# =============================================================================
# accept.sh — end-to-end acceptance for action-cancellation-consistency.
#
# Spawns REAL server processes (real rclpy action server + SQLite), drives
# them with the real CLI client over DDS, and asserts:
#   1. normal run: SUCCEEDED, terminal state == durable completed segments
#   2. duplicate goal id (same params) is rejected, not re-executed
#   3. same id with DIFFERENT parameters is rejected; original result stands
#   4. cancel during the LAST segment -> exactly one CANCELED terminal state,
#      and completed segments == stored segment rows
#   5. feedback loss (client discards every feedback) -> result/history exact
#   6. process interruption (kill -9) + POLICY_RESUME -> RECOVERABLE then
#      resumed headlessly to SUCCEEDED; pre-crash segments flagged recovered
#   7. process interruption (kill -9) + POLICY_ABORT  -> RECOVERABLE then
#      terminal ABORTED, completed work preserved
#   8. history queries (get/list) report the above
#
# Usage:  ./accept.sh
# Env:    KEEP_DB=1 keeps the run directory; SEGMENT_DELAY overrides pace.
# =============================================================================
set -u
set -o pipefail

WS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="$(mktemp -d "${TMPDIR:-/tmp}/action_cancel_accept.XXXXXX")"
DB="${RUN_DIR}/segments.db"
SRV_LOG="${RUN_DIR}/server.log"
SEGMENT_DELAY="${SEGMENT_DELAY:-0.35}"
ACTION="segmented_task"

# shellcheck disable=SC1091
set +u
source /opt/ros/jazzy/setup.bash
# shellcheck disable=SC1091
source "${WS}/install/setup.bash"
set -u

export ROS_LOCALHOST_ONLY=1

PASS=0; FAIL=0
SERVER_PID=""

jget() { python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(eval(sys.argv[2]))" "$1" "$2"; }
# jget file "d['terminal_state']"

ok()   { echo "PASS: $*"; PASS=$((PASS+1)); }
bad()  { echo "FAIL: $*"; FAIL=$((FAIL+1)); }

SERVER_BIN="${WS}/install/action_cancel_server/lib/action_cancel_server/segment_server"
CLIENT_BIN="${WS}/install/action_cancel_server/lib/action_cancel_server/segment_client"

start_server() {
  "${SERVER_BIN}" --db-path "${DB}" --action-name "${ACTION}" \
      --segment-delay "${SEGMENT_DELAY}" \
      >"${SRV_LOG}" 2>&1 &
  SERVER_PID=$!
  # wait for readiness (action discoverable) by polling history service
  for _ in $(seq 1 100); do
    if grep -q "segment task server ready" "${SRV_LOG}" 2>/dev/null; then
      sleep 0.5; return 0
    fi
    kill -0 "${SERVER_PID}" 2>/dev/null || { echo "server died early"; cat "${SRV_LOG}"; return 1; }
    sleep 0.1
  done
  echo "server failed to become ready"; cat "${SRV_LOG}"; return 1
}

stop_server() {
  [ -n "${SERVER_PID}" ] || return 0
  kill -INT "${SERVER_PID}" 2>/dev/null || true
  for _ in $(seq 1 50); do kill -0 "${SERVER_PID}" 2>/dev/null || { SERVER_PID=""; return 0; }; sleep 0.1; done
  kill -KILL "${SERVER_PID}" 2>/dev/null || true; SERVER_PID=""
}

kill9_server() {   # simulate a hard crash / power loss
  kill -KILL "${SERVER_PID}" 2>/dev/null || true
  wait "${SERVER_PID}" 2>/dev/null || true
  SERVER_PID=""
}

cleanup() {
  if [ "${KEEP_DB:-0}" = "1" ]; then echo "run dir kept: ${RUN_DIR}"; else
    stop_server; rm -rf "${RUN_DIR}"; fi
}
trap cleanup EXIT

CLIENT=("${CLIENT_BIN}" --action-name "${ACTION}")

run_client() {  # writes JSON to $1, returns client exit code
  local out="$1"; shift
  if "${CLIENT[@]}" "$@" >"${out}" 2>"${RUN_DIR}/client.err"; then
    return 0
  else
    local rc=$?
    # rejected goals exit 2; the JSON is still emitted
    return $rc
  fi
}

echo "== workspace: ${WS}"
echo "== run dir:   ${RUN_DIR}"
start_server || exit 1
echo

# ------------------------------------------------------------------------- 1
echo "--- [1] normal run SUCCEEDS and matches durable history"
run_client "${RUN_DIR}/r1.json" send g_normal \
  --inputs-file "${WS}/examples/task_normal.json" --iterations 2000 --resume
T=$(jget "${RUN_DIR}/r1.json" "d['terminal_state']")
[ "${T}" = "3" ] && ok "normal terminal_state=SUCCEEDED(3)" || bad "normal terminal_state=${T}"
C=$(jget "${RUN_DIR}/r1.json" "d['completed_segments']")
[ "${C}" = "4" ] && ok "normal completed_segments=4" || bad "normal completed=${C}"

run_client "${RUN_DIR}/h1.json" get g_normal
HC=$(jget "${RUN_DIR}/h1.json" "d['completed_segments']")
HS=$(jget "${RUN_DIR}/h1.json" "d['state']")
NSEG=$(jget "${RUN_DIR}/h1.json" "len(d['segments'])")
[ "${HC}" = "4" ] && [ "${HS}" = "3" ] && [ "${NSEG}" = "4" ] \
  && ok "history state=SUCCEEDED, stored segments=4" \
  || bad "history state=${HS} completed=${HC} segments=${NSEG}"
# terminal hash equals last chained hash
RHASH=$(jget "${RUN_DIR}/h1.json" "d['result_hash']")
LHASH=$(jget "${RUN_DIR}/h1.json" "d['segments'][-1]['chained_hash']")
[ "${RHASH}" = "${LHASH}" ] && [ -n "${RHASH}" ] \
  && ok "result_hash == final chained hash" || bad "result hash mismatch"
echo

# ------------------------------------------------------------------------- 2
echo "--- [2] duplicate goal id (identical params) rejected, no re-run"
run_client "${RUN_DIR}/r2.json" send g_normal \
  --inputs-file "${WS}/examples/task_normal.json" --iterations 2000 --resume || RC=$?
RC=${RC:-0}
[ "${RC}" = "2" ] && ok "duplicate resend rejected (exit 2)" || bad "duplicate exit=${RC}"
run_client "${RUN_DIR}/h2.json" get g_normal
ERR=$(jget "${RUN_DIR}/h2.json" "d['error_code']")
[ "${ERR}" = "DUPLICATE_FINISHED" ] && ok "history records DUPLICATE_FINISHED" \
  || bad "history error_code=${ERR}"
echo

# ------------------------------------------------------------------------- 3
echo "--- [3] same id with changed parameters rejected"
run_client "${RUN_DIR}/r3.json" send g_normal --input changed --input params \
  --iterations 2000 --resume || RC3=$?
RC3=${RC3:-0}
[ "${RC3}" = "2" ] && ok "changed-params goal rejected" || bad "changed-params exit=${RC3}"
run_client "${RUN_DIR}/h3.json" get g_normal
ERR3=$(jget "${RUN_DIR}/h3.json" "d['error_code']")
SEG3=$(jget "${RUN_DIR}/h3.json" "len(d['segments'])")
[ "${ERR3}" = "DUPLICATE_ID_CHANGED_PARAMETERS" ] && [ "${SEG3}" = "4" ] \
  && ok "rejection reason recorded; original 4 segments intact" \
  || bad "err=${ERR3} segments=${SEG3}"
echo

# ------------------------------------------------------------------------- 4
echo "--- [4] cancel during the LAST segment -> single CANCELED state"
run_client "${RUN_DIR}/r4.json" send g_cancel \
  --inputs-file "${WS}/examples/task_cancel_last.json" --iterations 2000 --resume \
  --cancel-after-index 3 || RC4=$?
RC4=${RC4:-0}
T4=$(jget "${RUN_DIR}/r4.json" "d['terminal_state']")
C4=$(jget "${RUN_DIR}/r4.json" "d['completed_segments']")
ADM=$(jget "${RUN_DIR}/r4.json" "d.get('cancel_admitted')")
[ "${T4}" = "4" ] && ok "terminal_state=CANCELED(4)" || bad "cancel terminal=${T4}"
[ "${C4}" = "4" ] && ok "exactly 4/5 segments durable (last never committed)" \
  || bad "cancel completed=${C4}"
[ "${ADM}" = "True" ] && ok "cancel admitted (ERROR_NONE + goals_canceling)" \
  || bad "cancel not admitted: ${ADM}"

run_client "${RUN_DIR}/h4.json" get g_cancel
HS4=$(jget "${RUN_DIR}/h4.json" "d['state']")
HC4=$(jget "${RUN_DIR}/h4.json" "d['completed_segments']")
NS4=$(jget "${RUN_DIR}/h4.json" "len(d['segments'])")
RH4=$(jget "${RUN_DIR}/h4.json" "d['result_hash']")
[ "${HS4}" = "4" ] && [ "${HC4}" = "4" ] && [ "${NS4}" = "4" ] \
  && ok "history: single terminal CANCELED, segments==completed==4" \
  || bad "history state=${HS4} completed=${HC4} segments=${NS4}"
[ "${RH4}" = "" ] && ok "canceled goal has no result hash" || bad "unexpected hash=${RH4}"
echo

# ------------------------------------------------------------------------- 5
echo "--- [5] feedback loss: client ignores all feedback, truth still exact"
run_client "${RUN_DIR}/r5.json" send g_nofb \
  --input a --input b --input c --input d --iterations 2000 --resume \
  --ignore-feedback
FBC=$(jget "${RUN_DIR}/r5.json" "d['feedback_count']")
T5=$(jget "${RUN_DIR}/r5.json" "d['terminal_state']")
C5=$(jget "${RUN_DIR}/r5.json" "d['completed_segments']")
[ "${FBC}" = "0" ] && ok "client observed 0 feedbacks" || bad "feedback_count=${FBC}"
[ "${T5}" = "3" ] && [ "${C5}" = "4" ] \
  && ok "SUCCEEDED with 4 segments despite total feedback loss" \
  || bad "nofb state=${T5} completed=${C5}"
echo

# ------------------------------------------------------------- 6 & 7: crash
for POLICY in resume abort; do
  ID="g_crash_${POLICY}"
  echo "--- [crash:${POLICY}] start, kill -9 mid-run, restart"
  if [ "${POLICY}" = "resume" ]; then
    IF="${WS}/examples/task_crash_resume.json"; POLFLAG="--resume"; ITERS=2000000
  else
    IF="${WS}/examples/task_crash_abort.json";  POLFLAG="--abort-on-recovery"; ITERS=2000000
  fi

  # launch client in background, then hard-kill the server mid-run.
  # Wait deterministically (poll the DB) until >=1 segment is committed but
  # the goal is still RUNNING, so the crash genuinely lands "in progress".
  ( "${CLIENT[@]}" send "${ID}" --inputs-file "${IF}" --iterations "${ITERS}" ${POLFLAG} \
      >"${RUN_DIR}/crash_${POLICY}_client.json" 2>&1 ) &
  CPID=$!
  READY=0
  for _ in $(seq 1 120); do
    ROW=$(python3 - "$DB" "$ID" 2>/dev/null <<'PY'
import sqlite3, sys
try:
    c = sqlite3.connect(sys.argv[1])
    r = c.execute("SELECT state, completed_segments, total_segments FROM goals WHERE goal_id=?", (sys.argv[2],)).fetchone()
except Exception:
    r = None
if not r:
    print("none 0 0")
else:
    print(f"{r[0]} {r[1]} {r[2]}")
PY
)
    read -r ST CM TT <<<"${ROW}"
    if [ "${ST}" = "1" ] && [ "${CM}" -ge 1 ] && [ "${CM}" -lt "${TT}" ]; then
      READY=1
      # small extra delay to be clearly mid-run (between segments), not on
      # the boundary of the very first commit
      sleep "$(python3 -c "print(0.15)")"
      break
    fi
    if [ "${ST}" = "3" ] || [ "${ST}" = "4" ] || [ "${ST}" = "5" ]; then
      echo "    goal reached terminal ${ST} before crash window; aborting case"
      break
    fi
    sleep 0.25
  done
  kill9_server
  wait "${CPID}" 2>/dev/null || true
  [ "${READY}" = "1" ] || echo "    (warning: crash fired without confirmed partial progress)"

  # Before restart, the DB should still hold a RUNNING goal + partial work.
  python3 - "$DB" "$ID" >"${RUN_DIR}/pre_${POLICY}.txt" <<'PY'
import sqlite3, sys
db, gid = sys.argv[1], sys.argv[2]
c = sqlite3.connect(db)
st, comp, total = c.execute(
    "SELECT state, completed_segments, total_segments FROM goals WHERE goal_id=?",
    (gid,)).fetchone()
nseg = c.execute("SELECT COUNT(*) FROM segments WHERE goal_id=?", (gid,)).fetchone()[0]
print(st, comp, nseg, total)
PY
  read -r PST PCOMP PNSEG PTOT <"${RUN_DIR}/pre_${POLICY}.txt"
  echo "    pre-restart durable: state=${PST} completed=${PCOMP} segments=${PNSEG}/${PTOT}"
  [ "${PST}" = "1" ] && [ "${PCOMP}" -ge 1 ] && [ "${PCOMP}" -lt "${PTOT}" ] \
    && ok "[${POLICY}] crash left a partial RUNNING goal" \
    || bad "[${POLICY}] unexpected pre-restart state=${PST} comp=${PCOMP}/${PTOT}"

  # restart the SAME database -> recovery sweep + policy action
  start_server || exit 1

  # poll history until terminal
  FINAL=""
  for _ in $(seq 1 150); do
    run_client "${RUN_DIR}/post_${POLICY}.json" get "${ID}" || true
    FINAL=$(jget "${RUN_DIR}/post_${POLICY}.json" "d['state']" 2>/dev/null || echo "")
    case "${FINAL}" in 3|4|5|6) break;; esac
    sleep 0.2
  done
  echo "    post-restart state=${FINAL}"

  if [ "${POLICY}" = "resume" ]; then
    [ "${FINAL}" = "3" ] && ok "[resume] resumed headlessly to SUCCEEDED" \
      || bad "[resume] final state=${FINAL}"
    POSTC=$(jget "${RUN_DIR}/post_${POLICY}.json" "d['completed_segments']")
    POSTN=$(jget "${RUN_DIR}/post_${POLICY}.json" "len(d['segments'])")
    POSTT=$(jget "${RUN_DIR}/post_${POLICY}.json" "d['total_segments']")
    [ "${POSTC}" = "${POSTT}" ] && [ "${POSTN}" = "${POSTT}" ] \
      && ok "[resume] all ${POSTT} segments present, completed==segments" \
      || bad "[resume] completed=${POSTC} segments=${POSTN} total=${POSTT}"
    PREV_REC=$(jget "${RUN_DIR}/post_${POLICY}.json" "d['segments'][0]['recovered']")
    [ "${PREV_REC}" = "True" ] && ok "[resume] pre-crash segments flagged recovered" \
      || bad "[resume] recovered flag=${PREV_REC}"
    grep -q "marked RECOVERABLE" "${SRV_LOG}" && grep -q "resuming goal ${ID}" "${SRV_LOG}" \
      && ok "[resume] server logged RECOVERABLE sweep and resume" \
      || bad "[resume] missing recovery log lines"
  else
    [ "${FINAL}" = "5" ] && ok "[abort] terminated ABORTED per POLICY_ABORT" \
      || bad "[abort] final state=${FINAL}"
    AERR=$(jget "${RUN_DIR}/post_${POLICY}.json" "d['error_code']")
    AC=$(jget "${RUN_DIR}/post_${POLICY}.json" "d['completed_segments']")
    AN=$(jget "${RUN_DIR}/post_${POLICY}.json" "len(d['segments'])")
    [ "${AERR}" = "ABORTED_BY_RECOVERY_POLICY" ] \
      && ok "[abort] error_code=ABORTED_BY_RECOVERY_POLICY" \
      || bad "[abort] error_code=${AERR}"
    [ "${AC}" -ge 1 ] && [ "${AC}" = "${AN}" ] \
      && ok "[abort] completed work preserved (${AC} segments)" \
      || bad "[abort] completed=${AC} segments=${AN}"
    grep -q "aborted on recovery by POLICY_ABORT" "${SRV_LOG}" \
      && ok "[abort] server logged policy abort" || bad "[abort] missing abort log"
  fi
  echo
done

# ------------------------------------------------------------------------- 8
echo "--- [8] list history"
run_client "${RUN_DIR}/list.json" list --state 255
NGOALS=$(jget "${RUN_DIR}/list.json" "len(d['goals'])")
[ "${NGOALS}" -ge 5 ] && ok "list reports ${NGOALS} durable goals" \
  || bad "list goals=${NGOALS}"
run_client "${RUN_DIR}/list_succ.json" list --state 3
NSUCC=$(jget "${RUN_DIR}/list_succ.json" "len(d['goals'])")
[ "${NSUCC}" -ge 3 ] && ok "state filter SUCCEEDED -> ${NSUCC} goals" \
  || bad "succeeded filter=${NSUCC}"

echo
echo "=============================================="
echo " RESULT: ${PASS} passed, ${FAIL} failed"
echo "=============================================="
stop_server
[ "${FAIL}" = "0" ]
