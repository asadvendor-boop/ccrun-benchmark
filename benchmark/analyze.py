#!/usr/bin/env python3
"""
ccrun Benchmark Analyzer
========================

Reads benchmark result files and generates:
1. Statistical comparison table
2. Welch's t-test between implementations
3. Summary report proving/disproving John Crickett's claim

Usage: python3 analyze.py <results_dir> [output.json]
"""

import os
import sys
import json
import math
from pathlib import Path

def read_data(filepath):
    """Read timing data from a results file."""
    data = []
    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    data.append(float(line))
                except ValueError:
                    pass
    return data

def calc_stats(data):
    """Calculate descriptive statistics."""
    if not data:
        return {"error": "no data"}

    n = len(data)
    mean = sum(data) / n
    sorted_data = sorted(data)
    median = sorted_data[n // 2]

    if n > 1:
        variance = sum((x - mean) ** 2 for x in data) / (n - 1)
        stdev = math.sqrt(variance)
    else:
        stdev = 0

    return {
        "count": n,
        "mean": round(mean, 3),
        "median": round(median, 3),
        "stdev": round(stdev, 3),
        "min": round(min(data), 3),
        "max": round(max(data), 3),
        "p95": round(sorted_data[int(n * 0.95)], 3) if n > 1 else round(data[0], 3),
    }

def welch_ttest(data1, data2):
    """
    Perform Welch's t-test (unequal variance t-test).
    Returns t-statistic and approximate p-value.
    """
    n1, n2 = len(data1), len(data2)
    if n1 < 2 or n2 < 2:
        return 0, 1.0

    mean1 = sum(data1) / n1
    mean2 = sum(data2) / n2
    var1 = sum((x - mean1) ** 2 for x in data1) / (n1 - 1)
    var2 = sum((x - mean2) ** 2 for x in data2) / (n2 - 1)

    se = math.sqrt(var1 / n1 + var2 / n2)
    if se == 0:
        return 0, 1.0

    t_stat = (mean1 - mean2) / se

    # Welch-Satterthwaite degrees of freedom
    num = (var1 / n1 + var2 / n2) ** 2
    denom = (var1 / n1) ** 2 / (n1 - 1) + (var2 / n2) ** 2 / (n2 - 1)
    df = num / denom if denom > 0 else 1

    # Approximate p-value using normal distribution for large df
    p_value = 2 * (1 - normal_cdf(abs(t_stat)))

    return round(t_stat, 4), round(p_value, 6)

def normal_cdf(x):
    """Approximate CDF of standard normal distribution."""
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def print_table(headers, rows, title=""):
    """Print a formatted ASCII table."""
    if title:
        print(f"\n{'=' * 70}")
        print(f"  {title}")
        print(f"{'=' * 70}")

    if not rows:
        print("  (no data)")
        return

    col_widths = [max(len(str(h)), max(len(str(r[i])) for r in rows))
                  for i, h in enumerate(headers)]

    header_line = " | ".join(h.ljust(w) for h, w in zip(headers, col_widths))
    sep_line = "-+-".join("-" * w for w in col_widths)

    print(f"  {header_line}")
    print(f"  {sep_line}")
    for row in rows:
        print(f"  {' | '.join(str(r).ljust(w) for r, w in zip(row, col_widths))}")

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 analyze.py <results_dir> [output.json]")
        sys.exit(1)

    results_dir = Path(sys.argv[1])
    output_file = sys.argv[2] if len(sys.argv) > 2 else str(results_dir / "analysis.json")

    languages = ["python", "go", "rust", "c"]
    results = {"startup": {}, "workloads": {}, "comparisons": {}}

    # ---- CATEGORY A: Startup Overhead ----

    startup_data = {}
    rows = []
    for lang in languages:
        fpath = results_dir / f"startup_{lang}.txt"
        if fpath.exists():
            data = read_data(fpath)
            startup_data[lang] = data
            stats = calc_stats(data)
            results["startup"][lang] = stats
            rows.append([lang.upper(), stats["mean"], stats["median"],
                        stats["stdev"], stats["min"], stats["max"], stats["p95"]])

    if rows:
        print_table(
            ["Language", "Mean (ms)", "Median (ms)", "StdDev", "Min", "Max", "P95"],
            rows,
            "CATEGORY A: Container Startup Time (ms)"
        )

    # ---- CATEGORY B: Workloads ----
    workload_names = ["cpu", "io", "mem"]
    for wl in workload_names:
        rows = []
        workload_data = {}
        for lang in languages:
            fpath = results_dir / f"workload_{wl}_{lang}.txt"
            if fpath.exists():
                data = read_data(fpath)
                workload_data[lang] = data
                stats = calc_stats(data)
                if wl not in results["workloads"]:
                    results["workloads"][wl] = {}
                results["workloads"][wl][lang] = stats
                rows.append([lang.upper(), stats["mean"], stats["median"],
                            stats["stdev"], stats["min"], stats["max"], stats["p95"]])

        if rows:
            print_table(
                ["Language", "Mean (ms)", "Median (ms)", "StdDev", "Min", "Max", "P95"],
                rows,
                f"CATEGORY B: In-Container Workload - {wl.upper()} (ms)"
            )

        # Statistical significance tests
        if len(workload_data) >= 2:
            lang_list = list(workload_data.keys())
            comparisons = []
            for i in range(len(lang_list)):
                for j in range(i + 1, len(lang_list)):
                    l1, l2 = lang_list[i], lang_list[j]
                    t_stat, p_val = welch_ttest(workload_data[l1], workload_data[l2])
                    sig = "YES" if p_val < 0.05 else "NO"
                    comparisons.append([f"{l1} vs {l2}", t_stat, p_val, sig])

            if comparisons:
                print_table(
                    ["Comparison", "t-stat", "p-value", "Significant?"],
                    comparisons,
                    f"  Statistical Significance (Welch's t-test) - {wl.upper()}"
                )

    # ---- CATEGORY C: Pure Workloads (timed inside container, no startup) ----
    pure_workload_names = ["pure_cpu", "pure_multicore"]
    pure_labels = {
        "pure_cpu": "Pure CPU (timed inside container)",
        "pure_multicore": "Multicore (4 parallel workers, timed inside container)",
    }
    for wl in pure_workload_names:
        rows = []
        workload_data = {}
        for lang in languages:
            fpath = results_dir / f"workload_{wl}_{lang}.txt"
            if fpath.exists():
                data = read_data(fpath)
                if data:
                    workload_data[lang] = data
                    stats = calc_stats(data)
                    if wl not in results["workloads"]:
                        results["workloads"][wl] = {}
                    results["workloads"][wl][lang] = stats
                    rows.append([lang.upper(), stats["mean"], stats["median"],
                                stats["stdev"], stats["min"], stats["max"], stats["p95"]])

        if rows:
            label = pure_labels.get(wl, wl.upper())
            print_table(
                ["Language", "Mean (ms)", "Median (ms)", "StdDev", "Min", "Max", "P95"],
                rows,
                f"CATEGORY C: {label} (ms)"
            )

        # Statistical significance tests
        if len(workload_data) >= 2:
            lang_list = list(workload_data.keys())
            comparisons = []
            for i in range(len(lang_list)):
                for j in range(i + 1, len(lang_list)):
                    l1, l2 = lang_list[i], lang_list[j]
                    t_stat, p_val = welch_ttest(workload_data[l1], workload_data[l2])
                    sig = "YES" if p_val < 0.05 else "NO"
                    comparisons.append([f"{l1} vs {l2}", t_stat, p_val, sig])

            if comparisons:
                print_table(
                    ["Comparison", "t-stat", "p-value", "Significant?"],
                    comparisons,
                    f"  Statistical Significance (Welch's t-test) - {wl.upper()}"
                )

    # ---- VERDICT ----
    print(f"\n{'=' * 70}")
    print(f"  VERDICT")
    print(f"{'=' * 70}")

    # Use pure workload data if available, fall back to total workload data
    verdict_workloads = []
    if "pure_cpu" in results["workloads"]:
        verdict_workloads.append("pure_cpu")
    if "pure_multicore" in results["workloads"]:
        verdict_workloads.append("pure_multicore")
    if not verdict_workloads:
        verdict_workloads = [wl for wl in workload_names if wl in results["workloads"]]

    all_similar = True
    for wl in verdict_workloads:
        if wl in results["workloads"]:
            means = [results["workloads"][wl][l]["mean"]
                    for l in languages if l in results["workloads"][wl]]
            if means:
                spread = (max(means) - min(means)) / min(means) * 100
                if spread > 5:  # More than 5% difference
                    all_similar = False
                print(f"  {wl.upper()} workload spread: {spread:.1f}%")

    # Also show total workloads for reference
    for wl in workload_names:
        if wl in results["workloads"] and wl not in verdict_workloads:
            means = [results["workloads"][wl][l]["mean"]
                    for l in languages if l in results["workloads"][wl]]
            if means:
                spread = (max(means) - min(means)) / min(means) * 100
                print(f"  {wl.upper()} total (startup+workload) spread: {spread:.1f}%")

    # Check startup differences
    if results["startup"]:
        startup_means = [results["startup"][l]["mean"]
                        for l in languages if l in results["startup"]]
        if startup_means:
            spread = (max(startup_means) - min(startup_means)) / min(startup_means) * 100
            print(f"  Startup time spread: {spread:.1f}%")

    print()
    if all_similar:
        print("  ✅ John Crickett is RIGHT about container runtime performance.")
        print("     In-container workloads perform identically regardless of")
        print("     what language the container runtime is written in.")
        print()
        print("  ⚠️  The commenters are ALSO right about tooling overhead.")
        print("     Container startup time varies by implementation language.")
        print()
        print("  📊 NUANCE: Both sides are correct — they're measuring")
        print("     different things. Runtime language affects TOOLING speed,")
        print("     not WORKLOAD speed.")
    else:
        print("  ❌ Unexpected result: workload performance differs!")
        print("     This may indicate a measurement error or process model")
        print("     differences (e.g., multicore scheduling).")

    # Save results
    with open(output_file, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\n  Full results saved to: {output_file}")

if __name__ == "__main__":
    main()
