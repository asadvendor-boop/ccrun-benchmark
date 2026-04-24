#!/bin/bash
# ccrun Benchmark Suite - Master Runner
# Runs benchmarks across Python, Go, Rust, C implementations
# Usage: sudo ./run_benchmarks.sh [--iterations N]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
RESULTS_DIR="${SCRIPT_DIR}/results"
ROOTFS="/root/alpine-rootfs"
STARTUP_ITERS=100
WORKLOAD_ITERS=10

CCRUN_PY="python3 ${PROJECT_DIR}/python/ccrun.py"
CCRUN_GO="${PROJECT_DIR}/go/ccrun"
CCRUN_RS="${PROJECT_DIR}/rust/target/release/ccrun"
CCRUN_C="${PROJECT_DIR}/c/ccrun"

mkdir -p "$RESULTS_DIR"

now_ns() { date +%s%N; }

log() { echo -e "\033[0;36m  $1\033[0m"; }

# Build all
build_all() {
    log "Building Go..."
    cd "${PROJECT_DIR}/go" && go build -o ccrun . && cd -
    log "Building Rust..."
    cd "${PROJECT_DIR}/rust" && cargo build --release 2>&1 && cd -
    log "Building C..."
    cd "${PROJECT_DIR}/c" && make clean && make && cd -
    log "All builds complete"
}

# Prepare benchmark scripts in Alpine rootfs
prepare_workloads() {
    cat > "${ROOTFS}/bench_cpu.sh" << 'EOF'
#!/bin/sh
i=0; a=0; b=1; n=500000
while [ $i -lt $n ]; do t=$((a+b)); a=$b; b=$t; i=$((i+1)); done
echo "done"
EOF
    cat > "${ROOTFS}/bench_io.sh" << 'EOF'
#!/bin/sh
dd if=/dev/zero of=/tmp/bf bs=1M count=50 2>/dev/null
dd if=/tmp/bf of=/dev/null bs=1M 2>/dev/null
rm -f /tmp/bf; echo "done"
EOF
    cat > "${ROOTFS}/bench_mem.sh" << 'EOF'
#!/bin/sh
dd if=/dev/urandom of=/tmp/mb bs=1M count=20 2>/dev/null
cat /tmp/mb >/dev/null 2>/dev/null
rm -f /tmp/mb; echo "done"
EOF

    # Pure CPU benchmark — times itself INSIDE the container using mstime
    cat > "${ROOTFS}/bench_pure_cpu.sh" << 'EOF'
#!/bin/sh
S=$(/bin/mstime)
i=0; a=0; b=1; n=500000
while [ $i -lt $n ]; do t=$((a+b)); a=$b; b=$t; i=$((i+1)); done
E=$(/bin/mstime)
echo "ELAPSED_MS:$((E - S))"
EOF

    # Multicore benchmark — 4 parallel Fibonacci workers
    cat > "${ROOTFS}/bench_multicore.sh" << 'EOF'
#!/bin/sh
S=$(/bin/mstime)
# Spawn 4 parallel CPU-bound workers
for w in 1 2 3 4; do
    (
        i=0; a=0; b=1; n=250000
        while [ $i -lt $n ]; do t=$((a+b)); a=$b; b=$t; i=$((i+1)); done
    ) &
done
wait
E=$(/bin/mstime)
echo "ELAPSED_MS:$((E - S))"
EOF

    chmod +x "${ROOTFS}/bench_cpu.sh" "${ROOTFS}/bench_io.sh" "${ROOTFS}/bench_mem.sh" \
             "${ROOTFS}/bench_pure_cpu.sh" "${ROOTFS}/bench_multicore.sh"
    log "Workloads prepared"
}

# Benchmark one implementation
bench_startup() {
    local name="$1" cmd="$2" iters="$3"
    local file="${RESULTS_DIR}/startup_${name}.txt"
    > "$file"
    log "Startup: $name ($iters iterations)"
    for i in $(seq 1 "$iters"); do
        s=$(now_ns)
        $cmd run --rootfs "$ROOTFS" /bin/true 2>/dev/null || true
        e=$(now_ns)
        echo "scale=3; ($e - $s)/1000000" | bc >> "$file"
        (( i % 20 == 0 )) && printf "    [%d/%d]\r" "$i" "$iters"
    done
    echo ""
}

bench_workload() {
    local name="$1" cmd="$2" wl="$3" wl_name="$4" iters="$5"
    local file="${RESULTS_DIR}/workload_${wl_name}_${name}.txt"
    > "$file"
    log "Workload $wl_name: $name ($iters iterations)"
    for i in $(seq 1 "$iters"); do
        s=$(now_ns)
        $cmd run --rootfs "$ROOTFS" /bin/sh "$wl" 2>/dev/null || true
        e=$(now_ns)
        echo "scale=3; ($e - $s)/1000000" | bc >> "$file"
    done
}

# Benchmark that extracts timing from INSIDE the container (no startup overhead)
bench_pure_workload() {
    local name="$1" cmd="$2" wl="$3" wl_name="$4" iters="$5"
    local file="${RESULTS_DIR}/workload_${wl_name}_${name}.txt"
    > "$file"
    log "Pure workload $wl_name: $name ($iters iterations)"
    for i in $(seq 1 "$iters"); do
        output=$($cmd run --rootfs "$ROOTFS" /bin/sh "$wl" 2>/dev/null || true)
        elapsed=$(echo "$output" | grep 'ELAPSED_MS:' | sed 's/ELAPSED_MS://')
        if [ -n "$elapsed" ]; then
            echo "$elapsed" >> "$file"
        fi
    done
}

# Main
main() {
    echo "=== ccrun Benchmark Suite ==="
    [[ $EUID -ne 0 ]] && { echo "Error: run as root"; exit 1; }
    [[ ! -d "$ROOTFS" ]] && { echo "Error: no rootfs at $ROOTFS"; exit 1; }

    build_all
    prepare_workloads

    echo ""; echo "=== CATEGORY A: Startup Overhead ==="
    for impl in "python:$CCRUN_PY" "go:$CCRUN_GO" "rust:$CCRUN_RS" "c:$CCRUN_C"; do
        IFS=: read -r name cmd <<< "$impl"
        bench_startup "$name" "$cmd" "$STARTUP_ITERS"
    done

    echo ""; echo "=== CATEGORY B: In-Container Workloads ==="
    for wl in "cpu:/bench_cpu.sh" "io:/bench_io.sh" "mem:/bench_mem.sh"; do
        IFS=: read -r wl_name wl_path <<< "$wl"
        for impl in "python:$CCRUN_PY" "go:$CCRUN_GO" "rust:$CCRUN_RS" "c:$CCRUN_C"; do
            IFS=: read -r name cmd <<< "$impl"
            bench_workload "$name" "$cmd" "$wl_path" "$wl_name" "$WORKLOAD_ITERS"
        done
    done

    echo ""; echo "=== CATEGORY C: Pure In-Container Workloads (no startup overhead) ==="
    for wl in "pure_cpu:/bench_pure_cpu.sh" "pure_multicore:/bench_multicore.sh"; do
        IFS=: read -r wl_name wl_path <<< "$wl"
        for impl in "python:$CCRUN_PY" "go:$CCRUN_GO" "rust:$CCRUN_RS" "c:$CCRUN_C"; do
            IFS=: read -r name cmd <<< "$impl"
            bench_pure_workload "$name" "$cmd" "$wl_path" "$wl_name" "$WORKLOAD_ITERS"
        done
    done

    echo ""; echo "=== Generating Analysis ==="
    python3 "${SCRIPT_DIR}/analyze.py" "${RESULTS_DIR}"
    echo "Done! Results in ${RESULTS_DIR}/"
}

main "$@"
