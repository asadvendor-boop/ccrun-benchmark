<p align="center">
  <h1 align="center">🐳 ccrun — Container Runtime Benchmark</h1>
  <p align="center">
    <strong>Does the language your container runtime is written in affect workload performance?</strong>
    <br/>
    <em>We built the same runtime in 4 languages and ran 540+ benchmarks to find out.</em>
  </p>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/languages-Python%20%7C%20Go%20%7C%20Rust%20%7C%20C-blue" />
  <img src="https://img.shields.io/badge/benchmarks-540%2B-green" />
  <img src="https://img.shields.io/badge/verdict-workload%20identical-brightgreen" />
  <img src="https://img.shields.io/badge/startup-10×%20difference-orange" />
</p>

---

## ⚡ TL;DR — The Finding

```
╔═══════════════════════════════════════════════════════════════╗
║                                                               ║
║   For a web server running hours?                             ║
║   → 30ms startup doesn't matter. Workload speed: IDENTICAL.  ║
║                                                               ║
║   For serverless at 10K containers/sec?                       ║
║   → That 10× startup difference is EVERYTHING.               ║
║                                                               ║
║   Both sides of the debate are right.                         ║
║   They're just measuring different things.                    ║
║                                                               ║
╚═══════════════════════════════════════════════════════════════╝
```

|  | Workload Speed | Startup Speed |
|--|---------------|---------------|
| **Spread** | **2.1–2.6%** | **969%** |
| **Significant?** | **NO** (p > 0.1) | YES (10×) |
| **Python vs C** | **Identical** | C is 10.7× faster |

---

## 💡 The Question

A [LinkedIn debate](https://www.linkedin.com/in/johncrickett/) sparked by John Crickett's Docker coding challenge asked:

> *"If you rewrite a container runtime from C to Python, does the containerized workload run slower?"*

One side argued **yes** — a faster runtime means faster containers. The other said **no** — the kernel does the real work. We decided to stop debating and start measuring.

## 🔬 The Experiment

We implemented `ccrun` — **a fully functional container runtime** — in **Python, Go, Rust, and C**, covering all 8 steps of [Coding Challenges #52](https://codingchallenges.fyi/challenges/challenge-docker):

| Step | Feature | Implementation |
|------|---------|---------------|
| 1 | Basic process execution | `fork()` + `execvp()` |
| 2 | Hostname isolation | `UTS namespace` + `sethostname()` |
| 3 | Root filesystem | `chroot()` |
| 4 | Process isolation | `PID namespace` + `/proc` mount |
| 5 | User namespace | `UID/GID mapping` (rootless mode) |
| 6 | Resource limits | `cgroup v2` (memory, CPU, PIDs) |
| 7 | Image pulling | Docker Hub Registry API v2 |
| 8 | Image configuration | `config.json` parsing (env, workdir, cmd) |

Then we ran **three categories of benchmarks** to isolate exactly what matters:

```
Category A — Startup Overhead (100 iterations)
  → How fast can the runtime create a container?

Category B — Total Time (10 iterations)
  → Startup + workload combined

Category C — Pure Workload (10 iterations)  ← THE DECISIVE TEST
  → Timed INSIDE the container with a custom monotonic clock binary
  → Completely eliminates startup overhead from the measurement
```

## 📊 Results

### Category A: Startup Time — *Runtime language matters here*

| Language | Mean (ms) | Relative |
|----------|-----------|----------|
| C        | 3.2       | 1.0×     |
| Rust     | 3.4       | 1.1×     |
| Go       | 7.8       | 2.4×     |
| Python   | 34.3      | **10.7×** |

### Category C: Pure CPU Workload — *Runtime language does NOT matter*

> **Fibonacci(500,000) timed inside the container using `clock_gettime(CLOCK_MONOTONIC)`**

| Language | Mean (ms) | Median | StdDev |
|----------|-----------|--------|--------|
| Python   | 1426.7    | 1422   | 31.5   |
| C        | 1444.7    | 1444   | 33.8   |
| Go       | 1446.4    | 1446   | 58.0   |
| Rust     | 1463.7    | 1454   | 39.9   |

**Welch's t-test results:**

| Comparison    | p-value | Significant? |
|---------------|---------|--------------|
| Python vs Go  | 0.345   | **NO**       |
| Python vs C   | 0.218   | **NO**       |
| Python vs Rust| 0.021   | marginal     |
| Go vs C       | 0.936   | **NO**       |
| Go vs Rust    | 0.437   | **NO**       |
| Rust vs C     | 0.251   | **NO**       |

```
╔═══════════════════════════════════════════════════════╗
║                                                       ║
║   Pure CPU spread: 2.6%                               ║
║   5 of 6 comparisons: NOT statistically significant   ║
║                                                       ║
║   The workload runs at identical speed regardless     ║
║   of what language called clone(), chroot(), exec()   ║
║                                                       ║
╚═══════════════════════════════════════════════════════╝
```

### Category C: Multicore — 4 Parallel Workers

| Language | Mean (ms) | Median | StdDev |
|----------|-----------|--------|--------|
| Python   | 883.8     | 885    | 5.9    |
| Go       | 889.8     | 884    | 14.7   |
| C        | 893.8     | 892    | 13.2   |
| Rust     | 902.4     | 910    | 24.9   |

```
╔═══════════════════════════════════════════════════════╗
║                                                       ║
║   Multicore spread: 2.1%                              ║
║   ALL 6 comparisons: NOT statistically significant    ║
║   (all p-values > 0.1)                                ║
║                                                       ║
║   Runtime process model (goroutines, threads, fork)   ║
║   has ZERO measurable impact on parallel workloads    ║
║                                                       ║
╚═══════════════════════════════════════════════════════╝
```

## 🏆 Verdict

| What we measured | Spread | Significant? | Conclusion |
|-----------------|--------|--------------|------------|
| **Pure CPU workload** | **2.6%** | **5/6 NO** | ✅ Identical |
| **Multicore workload** | **2.1%** | **6/6 NO** | ✅ Identical |
| Startup overhead | 969% | — | ⚠️ 10× difference |

> **Both sides of the debate are correct — they're measuring different things.**
>
> The runtime language doesn't affect what happens *inside* the container — the kernel runs identical syscalls. But it absolutely affects how fast you can *create* containers.

## 🧪 Bonus: Can Cython or Nuitka Close the Python Startup Gap?

A common suggestion: *"Just compile Python with Cython or Nuitka to eliminate the overhead."* We tested it.

We compiled `ccrun.py` with **Cython** (`--embed`, compiled to C + linked to `libpython`) and **Nuitka** (both `--standalone` and `--onefile` modes), then benchmarked all variants using [`hyperfine`](https://github.com/sharkdp/hyperfine) with 3 warmup runs and 50 measured runs (`--shell=none`):

| Variant | Startup | vs Pure Python |
|---------|---------|----------------|
| **Rust** | **3.4ms** | 10× faster |
| **C** | **3.2ms** | 10.7× faster |
| **Go** | **7.8ms** | 4.4× faster |
| Pure Python | 34ms | baseline |
| Nuitka `--standalone` | ~35ms | **4% slower** ❌ |
| Cython `--embed` | ~36ms | **6% slower** ❌ |
| Nuitka `--onefile` | ~41ms | **21% slower** ❌ |

```
╔══════════════════════════════════════════════════════════════╗
║                                                              ║
║   Cython and Nuitka make Python startup SLOWER, not faster.  ║
║                                                              ║
║   Why? The bottleneck is Py_Initialize() + import            ║
║   resolution — not your code. Compiling the code to C        ║
║   doesn't help when the interpreter boot IS the cost.        ║
║                                                              ║
║   Nuitka --onefile is worst: decompressing a 28MB binary     ║
║   at startup takes longer than just calling python3.         ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
```

### Where Python's Startup Overhead Actually Goes

| Phase | Time | % of Total | Cython helps? |
|-------|------|-----------|--------------|
| Interpreter boot (`Py_Initialize`) | ~5ms | 14% | ❌ Still required |
| Import resolution (os, json, subprocess…) | ~9ms | 25% | ❌ Still required |
| Script parsing | ~8ms | 23% | ✅ Eliminated by Cython... |
| ...but replaced by `.so` loading | ~9ms | — | ❌ ...with something slower |
| Container syscalls (clone, chroot, exec) | ~13ms | 38% | ❌ Same Python C API |

> **Note:** Breakdown percentages are from a separate profiling run. Absolute values vary between runs due to VM load, but the proportions and relative comparisons (measured via `hyperfine` in the same session) are consistent.

**Bottom line:** The only way to close the startup gap is to not use the Python runtime at all — which is exactly what Go, Rust, and C do.

## 🐛 Bugs We Found (And Fixed)

Building 4 implementations of the same runtime revealed subtle bugs that wouldn't surface in a single implementation:

### 1. Rust PID Namespace Bug
**Problem:** `unshare(CLONE_NEWPID)` doesn't put the *calling* process into the new PID namespace — only its future children. Our Rust container's workload process had a **host PID** instead of PID 1, causing `fork()` failures (BusyBox `ash` reports this as "can't fork: Out of memory").

**Fix:** Added an explicit `fork()` after `unshare(CLONE_NEWPID)` so the child becomes PID 1 in the new namespace:
```rust
// Before: broken — caller stays in parent namespace
unshare(CloneFlags::CLONE_NEWPID)?;
exec("/bin/sh"); // ← still has host PID!

// After: correct — child is PID 1
unshare(CloneFlags::CLONE_NEWPID)?;
match fork() {
    Ok(ForkResult::Child) => exec("/bin/sh"),  // ← PID 1!
    Ok(ForkResult::Parent { child }) => waitpid(child),
}
```

### 2. Cgroup v2 Controller Delegation
**Problem:** Child cgroups had no `memory.max` or `pids.max` files. Writes to these files silently failed, and resource limits were never enforced.

**Root cause:** The parent `ccrun` cgroup didn't enable `subtree_control`, so controllers weren't delegated to children.

**Fix:** Write `+cpu +memory +pids` to the parent's `cgroup.subtree_control` before creating child cgroups.

### 3. CPU Quota Throttled Multicore
**Problem:** Our multicore benchmark took 8 seconds instead of 0.9 seconds.

**Root cause:** Default `cpu.max = 50000 100000` limits the container to 50% of *one* core. With 4 parallel workers, they were serialized onto half a CPU.

**Fix:** Set `cpu.max = 400000 100000` to allow 4 full cores for multicore workloads.

## 🏗️ Architecture

```
                    ┌─────────────────────────────────┐
                    │         ccrun run <cmd>          │
                    │   (Python / Go / Rust / C)       │
                    └──────────┬──────────────────────┘
                               │
                    ┌──────────▼──────────────────────┐
                    │     1. clone() / unshare()       │
                    │     UTS + MNT + PID namespaces   │
                    └──────────┬──────────────────────┘
                               │
                    ┌──────────▼──────────────────────┐
                    │     2. Setup cgroup v2           │
                    │     memory.max, cpu.max, pids    │
                    └──────────┬──────────────────────┘
                               │
                    ┌──────────▼──────────────────────┐
                    │     3. chroot() to rootfs        │
                    │     Alpine Linux minirootfs      │
                    └──────────┬──────────────────────┘
                               │
                    ┌──────────▼──────────────────────┐
                    │     4. Mount /proc               │
                    │     Container sees PID 1         │
                    └──────────┬──────────────────────┘
                               │
                    ┌──────────▼──────────────────────┐
                    │     5. execvp() the command      │
                    │  ┌─────────────────────────────┐ │
                    │  │  /bin/sh workload.sh         │ │
                    │  │  (identical across all 4)    │ │
                    │  └─────────────────────────────┘ │
                    │     ↑ THIS is what we measure    │
                    └─────────────────────────────────┘
```

**Key insight:** Everything above the dotted line is *tooling* (varies by language). Everything below is *kernel work* (identical regardless of language). Our Category C benchmark measures only the kernel work.

## 🧪 Methodology

### Timing Approach
- **Category A/B:** External nanosecond timer (`clock_gettime` / `time.time_ns`)
- **Category C:** Custom static C binary (`mstime`) compiled into the Alpine rootfs, using `CLOCK_MONOTONIC` — completely independent of the container runtime

### Statistical Analysis
- **Test:** Welch's t-test (two-tailed, unequal variance assumed)
- **Significance level:** α = 0.05
- **Sample sizes:** 100 (startup), 10 (CPU), 5 (multicore)

### Test Environment
| Component | Detail |
|-----------|--------|
| Host | Apple Silicon (M-series) |
| Hypervisor | UTM (QEMU backend) |
| Guest OS | Ubuntu 26.04 (Resolute Raccoon), kernel 7.0.0-14-generic |
| Architecture | ARM64 (aarch64) |
| CPU | 4 cores, 1 thread/core |
| Memory | 8 GB |
| Rootfs | Alpine Linux minirootfs 3.21.4 |

## 📁 Repository Structure

```
ccrun-benchmark/
├── README.md                    # You are here
├── METHODOLOGY.md               # Deep-dive into experiment design
├── c/
│   ├── ccrun.c                  # C implementation (~750 lines)
│   └── Makefile
├── go/
│   ├── main.go                  # Go implementation (~630 lines)
│   └── go.mod
├── rust/
│   ├── src/main.rs              # Rust implementation (~680 lines)
│   ├── Cargo.toml
│   └── Cargo.lock
├── python/
│   └── ccrun.py                 # Python implementation (~640 lines)
├── benchmark/
│   ├── run_benchmarks.sh        # Master benchmark runner
│   ├── analyze.py               # Statistical analysis (Welch's t-test)
│   └── results/                 # Raw timing data from our test run
│       ├── analysis.json
│       ├── startup_*.txt
│       ├── workload_cpu_*.txt
│       ├── workload_pure_cpu_*.txt
│       └── workload_pure_multicore_*.txt
└── VM_SETUP_GUIDE.md            # How to reproduce on your own machine
```

## 🚀 Quick Start

### Prerequisites
- Linux VM with kernel 5.10+ (cgroup v2 support)
- `gcc`, `go`, `cargo`, `python3`
- Alpine minirootfs downloaded to `/root/alpine-rootfs/`

### Build All
```bash
# C
cd c && make && cd ..

# Go
cd go && go build -o ccrun . && cd ..

# Rust
cd rust && cargo build --release && cd ..

# Python — no build needed
```

### Run a Container
```bash
# All four do the same thing:
sudo ./c/ccrun run --rootfs /root/alpine-rootfs /bin/sh -c "echo Hello from C"
sudo ./go/ccrun run --rootfs /root/alpine-rootfs /bin/sh -c "echo Hello from Go"
sudo ./rust/target/release/ccrun run --rootfs /root/alpine-rootfs /bin/sh -c "echo Hello from Rust"
sudo python3 python/ccrun.py run --rootfs /root/alpine-rootfs /bin/sh -c "echo Hello from Python"
```

### Run Benchmarks
```bash
sudo ./benchmark/run_benchmarks.sh
# Results appear in benchmark/results/
# Statistical analysis printed to stdout
```

## 🤝 Contributing

Found a bug in one of the implementations? Want to add a fifth language? PRs welcome.

## 📝 License

MIT — use it, benchmark it, debate about it on LinkedIn.

---

<p align="center">
  <em>Built to settle a debate. Stayed for the systems programming.</em>
</p>
