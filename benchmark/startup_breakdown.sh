#!/bin/bash
# ============================================================
# Python Startup Breakdown Benchmark
# ============================================================
# Tests WHERE the 34ms Python startup overhead actually lives:
#   1. Bare interpreter boot (python3 -c "pass")
#   2. Interpreter + import resolution (our ccrun imports)
#   3. Full container creation (python3 ccrun.py run ... /bin/true)
#
# If (1) + (2) ≈ 34ms, then Cython can't help (it only speeds
# up code execution, not interpreter boot or imports).
# ============================================================

ITERATIONS=50
ROOTFS="/root/alpine-rootfs"
CCRUN_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "============================================"
echo "  Python Startup Breakdown Benchmark"
echo "  Iterations: $ITERATIONS"
echo "============================================"
echo ""

# ---- Test 1: Bare interpreter startup ----
echo ">>> Test 1: Bare Python interpreter (python3 -c 'pass')"
t1_total=0
for i in $(seq 1 $ITERATIONS); do
    start=$(date +%s%N 2>/dev/null || python3 -c "import time; print(int(time.time_ns()))")
    python3 -c "pass"
    end=$(date +%s%N 2>/dev/null || python3 -c "import time; print(int(time.time_ns()))")
    elapsed=$(( (end - start) / 1000000 ))
    t1_total=$((t1_total + elapsed))
done
t1_mean=$((t1_total / ITERATIONS))
echo "    Mean: ${t1_mean}ms"
echo ""

# ---- Test 2: Interpreter + ccrun imports ----
echo ">>> Test 2: Python + all ccrun imports"
IMPORTS="import os, sys, subprocess, json, hashlib, tarfile, struct, ctypes, signal, socket, errno"
t2_total=0
for i in $(seq 1 $ITERATIONS); do
    start=$(date +%s%N 2>/dev/null || python3 -c "import time; print(int(time.time_ns()))")
    python3 -c "$IMPORTS"
    end=$(date +%s%N 2>/dev/null || python3 -c "import time; print(int(time.time_ns()))")
    elapsed=$(( (end - start) / 1000000 ))
    t2_total=$((t2_total + elapsed))
done
t2_mean=$((t2_total / ITERATIONS))
echo "    Mean: ${t2_mean}ms"
echo ""

# ---- Test 3: Interpreter + imports + parse ccrun.py (no run) ----
echo ">>> Test 3: Python + load ccrun.py module (no container)"
t3_total=0
for i in $(seq 1 $ITERATIONS); do
    start=$(date +%s%N 2>/dev/null || python3 -c "import time; print(int(time.time_ns()))")
    python3 -c "exec(open('${CCRUN_DIR}/python/ccrun.py').read().split('if __name__')[0])"
    end=$(date +%s%N 2>/dev/null || python3 -c "import time; print(int(time.time_ns()))")
    elapsed=$(( (end - start) / 1000000 ))
    t3_total=$((t3_total + elapsed))
done
t3_mean=$((t3_total / ITERATIONS))
echo "    Mean: ${t3_mean}ms"
echo ""

# ---- Test 4: Full container run (the actual 34ms) ----
echo ">>> Test 4: Full container run (python3 ccrun.py run /bin/true)"
t4_total=0
for i in $(seq 1 $ITERATIONS); do
    start=$(date +%s%N 2>/dev/null || python3 -c "import time; print(int(time.time_ns()))")
    python3 ${CCRUN_DIR}/python/ccrun.py run --rootfs $ROOTFS /bin/true 2>/dev/null
    end=$(date +%s%N 2>/dev/null || python3 -c "import time; print(int(time.time_ns()))")
    elapsed=$(( (end - start) / 1000000 ))
    t4_total=$((t4_total + elapsed))
done
t4_mean=$((t4_total / ITERATIONS))
echo "    Mean: ${t4_mean}ms"
echo ""

# ---- Test 5: C container run (baseline) ----
echo ">>> Test 5: C container run (baseline)"
t5_total=0
for i in $(seq 1 $ITERATIONS); do
    start=$(date +%s%N 2>/dev/null || python3 -c "import time; print(int(time.time_ns()))")
    ${CCRUN_DIR}/c/ccrun run --rootfs $ROOTFS /bin/true 2>/dev/null
    end=$(date +%s%N 2>/dev/null || python3 -c "import time; print(int(time.time_ns()))")
    elapsed=$(( (end - start) / 1000000 ))
    t5_total=$((t5_total + elapsed))
done
t5_mean=$((t5_total / ITERATIONS))
echo "    Mean: ${t5_mean}ms"
echo ""

# ---- Breakdown ----
echo "============================================"
echo "  BREAKDOWN"
echo "============================================"
echo ""
echo "  1. Bare interpreter boot:    ${t1_mean}ms"
echo "  2. + Import resolution:      ${t2_mean}ms  (+$((t2_mean - t1_mean))ms from imports)"
echo "  3. + Parse ccrun.py:         ${t3_mean}ms  (+$((t3_mean - t2_mean))ms from parsing)"
echo "  4. + Container creation:     ${t4_mean}ms  (+$((t4_mean - t3_mean))ms from syscalls)"
echo "  5. C baseline (reference):   ${t5_mean}ms"
echo ""
echo "============================================"
echo "  WHAT CYTHON COULD OPTIMIZE"
echo "============================================"
echo ""
interp_pct=$(( (t1_mean * 100) / t4_mean ))
import_pct=$(( ((t2_mean - t1_mean) * 100) / t4_mean ))
parse_pct=$(( ((t3_mean - t2_mean) * 100) / t4_mean ))
syscall_pct=$(( ((t4_mean - t3_mean) * 100) / t4_mean ))
echo "  Interpreter boot:  ${t1_mean}ms  (${interp_pct}% of total) — Cython: NO HELP"
echo "  Import resolution: +$((t2_mean - t1_mean))ms  (${import_pct}% of total) — Cython: NO HELP"
echo "  Script parsing:    +$((t3_mean - t2_mean))ms  (${parse_pct}% of total) — Cython: HELPS HERE"
echo "  Container syscalls:+$((t4_mean - t3_mean))ms  (${syscall_pct}% of total) — Cython: MINIMAL"
echo ""
cython_reachable=$((parse_pct + syscall_pct))
echo "  Cython can touch at most ${cython_reachable}% of the overhead."
echo "  The remaining $((100 - cython_reachable))% is interpreter + imports (untouchable)."
echo ""
echo "  Nuitka/PyOxidizer would eliminate the interpreter boot entirely."
echo "============================================"
