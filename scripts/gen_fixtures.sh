#!/usr/bin/env bash
# gen_fixtures.sh — deterministically generate the demo cgroup v2 fixtures.
#
# Layout: <root>/<container>/<UTC RFC3339 sample dir>/<cgroup file>
#
# The numbers in here are chosen so rates come out as clean hand-computable
# values (0.5 cores, 8 periods/s, ...). See README "手算样例" for the table.
set -euo pipefail

ROOT="${1:-$(dirname "$0")/../fixtures}"
rm -rf "$ROOT"
mkdir -p "$ROOT"

# sample <container> <timestampdir> — emits a sample dir path via stdout.
sdir() {
  local d="$ROOT/$1/$2"
  mkdir -p "$d"
  printf '%s\n' "$d"
}

put() { # put <file> <<'EOF' ...
  cat > "$1"
}

MEM_LIMIT=$((100 * 1024 * 1024))   # 100 MiB
M1=$((1 * 1024 * 1024));  M4=$((4 * 1024 * 1024));  M8=$((8 * 1024 * 1024)); M9=$((9 * 1024 * 1024))
M10=$((10 * 1024 * 1024)); M12=$((12 * 1024 * 1024)); M20=$((20 * 1024 * 1024))
M30=$((30 * 1024 * 1024)); M35=$((35 * 1024 * 1024)); M40=$((40 * 1024 * 1024)); M50=$((50 * 1024 * 1024))
M98=$((98 * 1024 * 1024))

##############################################################################
# container "worker" — demonstrates: normal rate, limit reached, OOM kill,
# missing sample (gap), counter reset on the SAME instance, instance rebuild
# (same container name, new instance.id), and clean continuation afterwards.
##############################################################################
W=worker

# --- 10:00:00 baseline ---
d=$(sdir $W 2026-09-24T10:00:00Z)
put "$d/cpu.stat" <<EOF
usage_usec 10000000
user_usec 6000000
system_usec 4000000
nr_periods 100
nr_throttled 0
throttled_usec 0
EOF
put "$d/cpu.pressure" <<EOF
some avg10=1.00 avg60=2.00 avg300=3.00 total=500000
EOF
put "$d/memory.current" <<EOF
$M40
EOF
put "$d/memory.events" <<EOF
low 0
high 0
max 0
oom 0
oom_kill 0
EOF
put "$d/memory.max" <<EOF
$MEM_LIMIT
EOF
printf '1\n' > "$d/memory.oom.group"
put "$d/memory.pressure" <<EOF
some avg10=2.00 avg60=3.00 avg300=4.00 total=800000
full avg10=0.50 avg60=1.00 avg300=1.50 total=200000
EOF
printf '4\n' > "$d/pids.current"
put "$d/pids.events" <<EOF
max 0
EOF
printf 'AAAAAAAA-1111\n' > "$d/instance.id"

# --- 10:00:01 (+0.5 core, healthy) ---
d=$(sdir $W 2026-09-24T10:00:01Z)
put "$d/cpu.stat" <<EOF
usage_usec 10500000
user_usec 6300000
system_usec 4200000
nr_periods 108
nr_throttled 0
throttled_usec 0
EOF
put "$d/cpu.pressure" <<EOF
some avg10=1.00 avg60=2.00 avg300=3.00 total=600000
EOF
printf '%s\n' "$M50" > "$d/memory.current"
put "$d/memory.events" <<EOF
low 0
high 0
max 0
oom 0
oom_kill 0
EOF
printf '%s\n' "$MEM_LIMIT" > "$d/memory.max"
printf '1\n' > "$d/memory.oom.group"
put "$d/memory.pressure" <<EOF
some avg10=2.50 avg60=3.00 avg300=4.00 total=900000
full avg10=0.60 avg60=1.00 avg300=1.50 total=250000
EOF
printf '4\n' > "$d/pids.current"
printf 'max 0\n' > "$d/pids.events"
printf 'AAAAAAAA-1111\n' > "$d/instance.id"

# --- 10:00:02 (memory climbs to 98 MiB, high reclaim events, throttling) ---
d=$(sdir $W 2026-09-24T10:00:02Z)
put "$d/cpu.stat" <<EOF
usage_usec 11000000
user_usec 6600000
system_usec 4400000
nr_periods 116
nr_throttled 2
throttled_usec 100000
EOF
put "$d/cpu.pressure" <<EOF
some avg10=1.20 avg60=2.00 avg300=3.00 total=700000
EOF
printf '%s\n' "$M98" > "$d/memory.current"
put "$d/memory.events" <<EOF
low 0
high 5
max 0
oom 0
oom_kill 0
EOF
printf '%s\n' "$MEM_LIMIT" > "$d/memory.max"
printf '1\n' > "$d/memory.oom.group"
put "$d/memory.pressure" <<EOF
some avg10=8.00 avg60=5.00 avg300=4.50 total=1900000
full avg10=7.00 avg60=4.00 avg300=3.00 total=1200000
EOF
printf '4\n' > "$d/pids.current"
printf 'max 0\n' > "$d/pids.events"
printf 'AAAAAAAA-1111\n' > "$d/instance.id"

# --- 10:00:03 (max counter advances 0->3: limit reached, no OOM kill) ---
d=$(sdir $W 2026-09-24T10:00:03Z)
put "$d/cpu.stat" <<EOF
usage_usec 11300000
user_usec 6800000
system_usec 4500000
nr_periods 124
nr_throttled 5
throttled_usec 280000
EOF
put "$d/cpu.pressure" <<EOF
some avg10=1.10 avg60=1.80 avg300=2.90 total=750000
EOF
printf '%s\n' "$MEM_LIMIT" > "$d/memory.current"
put "$d/memory.events" <<EOF
low 0
high 9
max 3
oom 0
oom_kill 0
EOF
printf '%s\n' "$MEM_LIMIT" > "$d/memory.max"
printf '1\n' > "$d/memory.oom.group"
put "$d/memory.pressure" <<EOF
some avg10=9.50 avg60=6.00 avg300=5.00 total=2900000
full avg10=9.00 avg60=5.50 avg300=4.00 total=2200000
EOF
printf '4\n' > "$d/pids.current"
printf 'max 0\n' > "$d/pids.events"
printf 'AAAAAAAA-1111\n' > "$d/instance.id"

# --- 10:00:04 (oom 0->1 AND oom_kill 0->1: OOM kill; one task gone 4->3) ---
d=$(sdir $W 2026-09-24T10:00:04Z)
put "$d/cpu.stat" <<EOF
usage_usec 11350000
user_usec 6830000
system_usec 4520000
nr_periods 132
nr_throttled 6
throttled_usec 400000
EOF
put "$d/cpu.pressure" <<EOF
some avg10=0.80 avg60=1.50 avg300=2.80 total=760000
EOF
printf '%s\n' "$M4" > "$d/memory.current"
put "$d/memory.events" <<EOF
low 0
high 10
max 4
oom 1
oom_kill 1
EOF
printf '%s\n' "$MEM_LIMIT" > "$d/memory.max"
printf '1\n' > "$d/memory.oom.group"
put "$d/memory.pressure" <<EOF
some avg10=4.00 avg60=5.50 avg300=4.80 total=3000000
full avg10=2.00 avg60=4.50 avg300=3.80 total=2250000
EOF
printf '3\n' > "$d/pids.current"
printf 'max 0\n' > "$d/pids.events"
printf 'AAAAAAAA-1111\n' > "$d/instance.id"

# --- 10:00:05? NO sample: the 10:00:05Z directory is deliberately absent ---

# --- 10:00:06 (2s gap: dt=2, one sample missing; rate halves accordingly) ---
d=$(sdir $W 2026-09-24T10:00:06Z)
put "$d/cpu.stat" <<EOF
usage_usec 11900000
user_usec 7100000
system_usec 4800000
nr_periods 148
nr_throttled 6
throttled_usec 400000
EOF
put "$d/cpu.pressure" <<EOF
some avg10=0.60 avg60=1.20 avg300=2.50 total=860000
EOF
printf '%s\n' "$M20" > "$d/memory.current"
put "$d/memory.events" <<EOF
low 0
high 10
max 4
oom 1
oom_kill 1
EOF
printf '%s\n' "$MEM_LIMIT" > "$d/memory.max"
printf '1\n' > "$d/memory.oom.group"
put "$d/memory.pressure" <<EOF
some avg10=2.00 avg60=4.00 avg300=4.20 total=3200000
full avg10=1.00 avg60=3.00 avg300=3.20 total=2350000
EOF
printf '3\n' > "$d/pids.current"
printf 'max 0\n' > "$d/pids.events"
printf 'AAAAAAAA-1111\n' > "$d/instance.id"

# --- 10:00:07 (cpu counters roll back on SAME instance: counter_reset) ---
d=$(sdir $W 2026-09-24T10:00:07Z)
put "$d/cpu.stat" <<EOF
usage_usec 10000000
user_usec 6000000
system_usec 4000000
nr_periods 100
nr_throttled 0
throttled_usec 0
EOF
put "$d/cpu.pressure" <<EOF
some avg10=0.40 avg60=1.00 avg300=2.20 total=100000
EOF
printf '%s\n' "$M30" > "$d/memory.current"
put "$d/memory.events" <<EOF
low 0
high 10
max 4
oom 1
oom_kill 1
EOF
printf '%s\n' "$MEM_LIMIT" > "$d/memory.max"
printf '1\n' > "$d/memory.oom.group"
put "$d/memory.pressure" <<EOF
some avg10=1.50 avg60=3.00 avg300=3.80 total=3300000
full avg10=0.80 avg60=2.20 avg300=2.80 total=2450000
EOF
printf '3\n' > "$d/pids.current"
printf 'max 0\n' > "$d/pids.events"
printf 'AAAAAAAA-1111\n' > "$d/instance.id"

# --- 10:00:08 (normal resume after reset) ---
d=$(sdir $W 2026-09-24T10:00:08Z)
put "$d/cpu.stat" <<EOF
usage_usec 10400000
user_usec 6200000
system_usec 4200000
nr_periods 104
nr_throttled 0
throttled_usec 0
EOF
put "$d/cpu.pressure" <<EOF
some avg10=0.40 avg60=0.90 avg300=2.10 total=400000
EOF
printf '%s\n' "$M35" > "$d/memory.current"
put "$d/memory.events" <<EOF
low 0
high 10
max 4
oom 1
oom_kill 1
EOF
printf '%s\n' "$MEM_LIMIT" > "$d/memory.max"
printf '1\n' > "$d/memory.oom.group"
put "$d/memory.pressure" <<EOF
some avg10=1.20 avg60=2.60 avg300=3.50 total=3400000
full avg10=0.70 avg60=2.00 avg300=2.60 total=2550000
EOF
printf '3\n' > "$d/pids.current"
printf 'max 0\n' > "$d/pids.events"
printf 'AAAAAAAA-1111\n' > "$d/instance.id"

# --- 10:00:10? absent; 10:00:10 -> 10:00:11 boundary changes instance.id ---
# --- 10:00:11 (NEW instance reuses the name "worker": rebuild; counters at 0) ---
d=$(sdir $W 2026-09-24T10:00:11Z)
put "$d/cpu.stat" <<EOF
usage_usec 200000
user_usec 100000
system_usec 100000
nr_periods 2
nr_throttled 0
throttled_usec 0
EOF
put "$d/cpu.pressure" <<EOF
some avg10=0.10 avg60=0.20 avg300=0.30 total=10000
EOF
printf '%s\n' "$M10" > "$d/memory.current"
put "$d/memory.events" <<EOF
low 0
high 0
max 0
oom 0
oom_kill 0
EOF
printf '%s\n' "$MEM_LIMIT" > "$d/memory.max"
printf '1\n' > "$d/memory.oom.group"
put "$d/memory.pressure" <<EOF
some avg10=0.20 avg60=0.30 avg300=0.40 total=20000
full avg10=0.05 avg60=0.10 avg300=0.15 total=5000
EOF
printf '1\n' > "$d/pids.current"
printf 'max 0\n' > "$d/pids.events"
printf 'BBBBBBBB-2222\n' > "$d/instance.id"

# --- 10:00:12 (new instance running normally) ---
d=$(sdir $W 2026-09-24T10:00:12Z)
put "$d/cpu.stat" <<EOF
usage_usec 700000
user_usec 400000
system_usec 300000
nr_periods 10
nr_throttled 0
throttled_usec 0
EOF
put "$d/cpu.pressure" <<EOF
some avg10=0.10 avg60=0.20 avg300=0.30 total=110000
EOF
printf '%s\n' "$M12" > "$d/memory.current"
put "$d/memory.events" <<EOF
low 0
high 0
max 0
oom 0
oom_kill 0
EOF
printf '%s\n' "$MEM_LIMIT" > "$d/memory.max"
printf '1\n' > "$d/memory.oom.group"
put "$d/memory.pressure" <<EOF
some avg10=0.30 avg60=0.35 avg300=0.42 total=120000
full avg10=0.10 avg60=0.15 avg300=0.20 total=25000
EOF
printf '1\n' > "$d/pids.current"
printf 'max 0\n' > "$d/pids.events"
printf 'BBBBBBBB-2222\n' > "$d/instance.id"

##############################################################################
# container "batch-job" — unlimited cgroup, healthy run, then a CLEAN exit:
# pids 1 -> 0 with every counter monotonic and no OOM evidence.
##############################################################################
B=batch-job

d=$(sdir $B 2026-09-24T11:00:00Z)
put "$d/cpu.stat" <<EOF
usage_usec 2000000
user_usec 2000000
system_usec 0
nr_periods 20
nr_throttled 0
throttled_usec 0
EOF
put "$d/cpu.pressure" <<EOF
some avg10=1.00 avg60=1.00 avg300=1.00 total=100000
EOF
printf '%s\n' "$M8" > "$d/memory.current"
put "$d/memory.events" <<EOF
low 0
high 0
max 0
oom 0
oom_kill 0
EOF
printf 'max\n' > "$d/memory.max"
put "$d/memory.pressure" <<EOF
some avg10=0.50 avg60=0.50 avg300=0.50 total=100000
full avg10=0.00 avg60=0.00 avg300=0.00 total=0
EOF
printf '1\n' > "$d/pids.current"
printf 'max 0\n' > "$d/pids.events"
printf 'CCCCCCCC-9999\n' > "$d/instance.id"

d=$(sdir $B 2026-09-24T11:00:01Z)
put "$d/cpu.stat" <<EOF
usage_usec 2800000
user_usec 2800000
system_usec 0
nr_periods 28
nr_throttled 0
throttled_usec 0
EOF
put "$d/cpu.pressure" <<EOF
some avg10=2.00 avg60=1.50 avg300=1.20 total=300000
EOF
printf '%s\n' "$M9" > "$d/memory.current"
put "$d/memory.events" <<EOF
low 0
high 0
max 0
oom 0
oom_kill 0
EOF
printf 'max\n' > "$d/memory.max"
put "$d/memory.pressure" <<EOF
some avg10=0.60 avg60=0.55 avg300=0.52 total=150000
full avg10=0.00 avg60=0.00 avg300=0.00 total=0
EOF
printf '1\n' > "$d/pids.current"
printf 'max 0\n' > "$d/pids.events"
printf 'CCCCCCCC-9999\n' > "$d/instance.id"

d=$(sdir $B 2026-09-24T11:00:02Z)
put "$d/cpu.stat" <<EOF
usage_usec 3000000
user_usec 3000000
system_usec 0
nr_periods 36
nr_throttled 0
throttled_usec 0
EOF
put "$d/cpu.pressure" <<EOF
some avg10=1.00 avg60=1.20 avg300=1.10 total=350000
EOF
printf '%s\n' "$M1" > "$d/memory.current"
put "$d/memory.events" <<EOF
low 0
high 0
max 0
oom 0
oom_kill 0
EOF
printf 'max\n' > "$d/memory.max"
put "$d/memory.pressure" <<EOF
some avg10=0.40 avg60=0.50 avg300=0.50 total=150000
full avg10=0.00 avg60=0.00 avg300=0.00 total=0
EOF
printf '0\n' > "$d/pids.current"
printf 'max 0\n' > "$d/pids.events"
printf 'CCCCCCCC-9999\n' > "$d/instance.id"

##############################################################################
# Deliberately broken inputs, kept in the shipped fixture tree so the ingest
# response demonstrates missing-file and bad-timestamp reporting.
##############################################################################

# required file memory.events missing -> sample rejected, run still succeeds
d=$(sdir broken-missing-file 2026-09-24T12:00:00Z)
put "$d/cpu.stat" <<EOF
usage_usec 1000
nr_periods 1
EOF
put "$d/cpu.pressure" <<EOF
some avg10=0.00 avg60=0.00 avg300=0.00 total=0
EOF
printf '1024\n' > "$d/memory.current"
printf '%s\n' "$MEM_LIMIT" > "$d/memory.max"
put "$d/memory.pressure" <<EOF
some avg10=0.00 avg60=0.00 avg300=0.00 total=0
full avg10=0.00 avg60=0.00 avg300=0.00 total=0
EOF
# (memory.events intentionally not created)

# sample directory whose name is not UTC RFC3339 -> reported, not fatal
d=$(sdir weird-timestamps not-a-timestamp)
put "$d/cpu.stat" <<EOF
usage_usec 1000
nr_periods 1
EOF
put "$d/cpu.pressure" <<EOF
some avg10=0.00 avg60=0.00 avg300=0.00 total=0
EOF
printf '1024\n' > "$d/memory.current"
put "$d/memory.events" <<EOF
low 0
high 0
max 0
oom 0
oom_kill 0
EOF
printf '%s\n' "$MEM_LIMIT" > "$d/memory.max"
put "$d/memory.pressure" <<EOF
some avg10=0.00 avg60=0.00 avg300=0.00 total=0
full avg10=0.00 avg60=0.00 avg300=0.00 total=0
EOF

echo "fixtures generated under $ROOT"
find "$ROOT" -maxdepth 2 -type d | sort
