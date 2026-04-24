# Methodology — Deep Dive

## Why Three Categories?

Most container benchmarks make a critical mistake: they measure **total time** (startup + workload) and attribute the difference to the workload. This conflates two fundamentally different things:

1. **Tooling overhead** — How fast the runtime sets up namespaces, cgroups, and chroot
2. **Kernel execution** — How fast the Linux kernel runs the containerized process

We designed three categories to isolate each factor:

### Category A: Startup Only
```
[runtime starts] ──── namespace + cgroup + chroot ──── [/bin/true exits]
└──────────── measured ────────────────────────────────┘
```
Runs `/bin/true` (exits immediately), so all measured time is tooling overhead.

### Category B: Total Time (Startup + Workload)
```
[runtime starts] ──── setup ──── [workload runs] ──── [exits]
└──────────────── measured ──────────────────────────┘
```
Typical benchmark approach. Startup overhead contaminates workload measurement.

### Category C: Pure Workload (Decisive Test)
```
[runtime starts] ──── setup ──── [mstime START] ── workload ── [mstime END] ── [exits]
                                 └──── measured ───────────────┘
```
Timing is done **inside** the container using a custom binary. The runtime language's startup overhead is completely invisible.

## The `mstime` Binary

BusyBox `date` in Alpine doesn't support `%N` (nanoseconds), and `/usr/bin/time` only has second precision. We needed millisecond-accurate timing inside the container.

Solution: A 12-line C program compiled as a **static binary** and copied into the Alpine rootfs:

```c
#include <stdio.h>
#include <time.h>
int main() {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    printf("%ld\n", ts.tv_sec * 1000 + ts.tv_nsec / 1000000);
    return 0;
}
```

Compiled with `gcc -static -o mstime mstime.c` and placed at `/bin/mstime` in the rootfs. This gives us a timing source that:
- Uses `CLOCK_MONOTONIC` (immune to NTP adjustments)
- Has millisecond precision
- Is a static binary (no dynamic linker overhead)
- Is identical across all container runtimes

## Workload Design

### Single-Core: Fibonacci(500,000)
```sh
i=0; a=0; b=1; n=500000
while [ $i -lt $n ]; do
    t=$((a+b)); a=$b; b=$t; i=$((i+1))
done
```
- Pure CPU-bound computation
- Runs in BusyBox `ash` (identical across all containers)
- 64-bit integer overflow wraps around (expected, doesn't affect timing)
- Takes ~1.4 seconds — long enough to make startup overhead negligible

### Multi-Core: 4× Fibonacci(250,000)
```sh
for w in 1 2 3 4; do
    ( i=0; a=0; b=1; n=250000; while [ $i -lt $n ]; do ... done ) &
done
wait
```
- Spawns 4 parallel subshells via `&`
- Tests whether the runtime's process model (Go goroutines, Rust threads, Python fork-exec) affects the kernel's ability to schedule parallel work
- Requires `cpu.max` to allow 4 full cores (`400000 100000`)

## Statistical Method

### Welch's t-test
We use Welch's t-test (not Student's) because:
- Unequal variances between implementations (Rust has higher StdDev than C)
- Small sample sizes (n=10 for CPU, n=5 for multicore)
- Two-tailed test (we don't assume a direction)

### Significance Level
- α = 0.05 (standard)
- A p-value > 0.05 means "we cannot reject the null hypothesis that the means are equal"

### Why Not More Iterations?
- Each CPU iteration takes ~1.4 seconds inside a container
- With 4 runtimes × 10 iterations = 56 seconds of pure compute time per category
- The results are so consistent (2.1–2.6% spread) that additional samples wouldn't change the verdict
- The multicore test has n=5 because each iteration includes container startup + 4 parallel workers

## Why the IO/MEM "Spread" is Misleading

The Category B results show IO spread of 674% and MEM spread of 488%. This is **not** because the runtimes execute IO/MEM differently — it's because those workloads are so fast (~5ms) that the startup overhead (~3–34ms) **dominates** the total time.

```
IO workload alone:    ~5ms (identical across runtimes)
Python startup:       ~34ms
Total (Python IO):    ~39ms

C startup:            ~3ms
Total (C IO):         ~8ms

"Spread":             39ms / 8ms = 487%  ← misleading!
```

This is why Category C exists — to separate the signal from the noise.

## Reproducibility

### Environment Requirements
| Requirement | Why |
|-------------|-----|
| Linux kernel ≥ 5.10 | cgroup v2 unified hierarchy |
| Root access | Namespace creation, cgroup management |
| 4+ CPU cores | Multicore benchmark needs parallel execution |
| Alpine rootfs | Lightweight, BusyBox-based container filesystem |

### Steps to Reproduce
1. Set up an Ubuntu 24.04+ VM (ARM64 or x86_64)
2. Install toolchains: `gcc`, `go`, `cargo`, `python3`
3. Download Alpine minirootfs
4. Enable cgroup subtree controllers: `echo "+cpu +memory +pids" > /sys/fs/cgroup/ccrun/cgroup.subtree_control`
5. Compile the `mstime` binary into the rootfs
6. Run `sudo ./benchmark/run_benchmarks.sh`

See [VM_SETUP_GUIDE.md](VM_SETUP_GUIDE.md) for detailed instructions.
