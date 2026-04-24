#!/bin/bash
# ============================================================
# Compiler Benchmark: Python vs Cython vs Nuitka vs Go vs Rust vs C
# ============================================================
ROOTFS="/root/alpine-rootfs"
ITERATIONS=50
CCRUN="/home/ccrun/ccrun-benchmark"

echo "============================================"
echo "  COMPILER STARTUP BENCHMARK (n=$ITERATIONS)"
echo "============================================"
echo ""

run_bench() {
    local label="$1"
    local cmd="$2"
    local total=0
    echo ">>> $label"
    for i in $(seq 1 $ITERATIONS); do
        start=$(date +%s%N)
        eval "$cmd" 2>/dev/null
        end=$(date +%s%N)
        elapsed=$(( (end - start) / 1000000 ))
        total=$((total + elapsed))
    done
    mean=$((total / ITERATIONS))
    echo "    Mean: ${mean}ms"
    echo ""
    eval "${3}=$mean"
}

run_bench "Test 1: Pure Python (python3 ccrun.py)" \
    "python3 $CCRUN/python/ccrun.py run --rootfs $ROOTFS /bin/true" t_python

run_bench "Test 2: Cython (compiled to C, linked to libpython)" \
    "$CCRUN/ccrun-cython run --rootfs $ROOTFS /bin/true" t_cython

run_bench "Test 3: Nuitka (standalone binary, bundled Python)" \
    "$CCRUN/ccrun-nuitka run --rootfs $ROOTFS /bin/true" t_nuitka

run_bench "Test 4: Go" \
    "$CCRUN/go/ccrun run --rootfs $ROOTFS /bin/true" t_go

run_bench "Test 5: Rust" \
    "$CCRUN/rust/target/release/ccrun run --rootfs $ROOTFS /bin/true" t_rust

run_bench "Test 6: C (baseline)" \
    "$CCRUN/c/ccrun run --rootfs $ROOTFS /bin/true" t_c

echo "============================================"
echo "  RESULTS — STARTUP TIME (SORTED)"
echo "============================================"
echo ""
echo "  C (baseline):        ${t_c}ms"
echo "  Rust:                ${t_rust}ms"
echo "  Go:                  ${t_go}ms"
echo "  Cython:              ${t_cython}ms"
echo "  Nuitka (standalone): ${t_nuitka}ms"
echo "  Pure Python:         ${t_python}ms"
echo ""

if [ $t_python -gt 0 ]; then
    cython_pct=$(( (t_python - t_cython) * 100 / t_python ))
    nuitka_pct=$(( (t_python - t_nuitka) * 100 / t_python ))
    echo "  Cython speedup vs Pure Python: ${cython_pct}%"
    echo "  Nuitka speedup vs Pure Python: ${nuitka_pct}%"
    echo ""
    echo "  Cython still needs libpython (interpreter boot remains)."
    echo "  Nuitka bundles interpreter (eliminates import but not runtime)."
fi
echo "============================================"
