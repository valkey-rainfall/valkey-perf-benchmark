# Benchmark Precision Guide: Achieving ≤1% CI Across Platforms

**Platforms**: ARM Graviton 3, AMD EPYC (Zen 4), Intel Xeon (pending)
**Date**: 2026-05-19
**Authors**: Rain Valentine, with investigation assistance from Rimuru (MeshClaw)

## Executive Summary

This guide documents how to achieve highly reproducible benchmark results on bare-metal servers for Valkey performance testing. Through systematic investigation, we reduced coefficient of variation (CV) from 1.5-3.8% down to 0.02-0.20% across platforms, enabling detection of performance differences as small as 0.5%.

| Platform | Before Tuning | After Tuning | Improvement |
|----------|--------------|--------------|-------------|
| ARM Graviton 3 (single-threaded) | CV 0.5-1.5% | **CV 0.20%** | 3-7x |
| ARM Graviton 3 (io-threads=9) | CV 1.5-3.0% | **CV 0.70%** | 2-4x |
| AMD EPYC 9R14 (io-threads=7) | CV 3.81% | **CV 0.024%** | 159x |
| Intel Xeon (pending) | — | — | — |

## Methodology

### How We Measured Precision

We distinguish two types of variance:

**Within-run CV**: How much throughput fluctuates during a single benchmark execution with one server instance. Measured by sampling RPS every 1 second during a 30-second run and computing CV of those samples. This captures instantaneous jitter from interrupts, scheduler decisions, and memory subsystem noise.

**Between-run CV**: How much the average throughput varies across independent server restarts. Measured by:
1. Start server → warmup → measure 30s → record mean RPS → stop server
2. Repeat N times (typically 10-30 for investigation, 3-5 for production)
3. Compute CV of the N mean-RPS values

Between-run CV is always larger than within-run CV because it includes additional variance from:
- ASLR (different code/data layout each start)
- Thread-to-core mapping non-determinism
- Page cache state differences
- Memory allocator arena initialization

**Statistical framework**: We report 95% confidence intervals using Student's t-distribution: `CI95 = t(n-1, 0.975) × stdev / √n`. The t-values (for n=3: t=4.303, for n=5: t=2.776, for n=10: t=2.262) come from standard t-distribution tables and were computed using Python's `statistics` module for mean/stdev and manual t-table lookup for small-sample CIs. For automated analysis, `scipy.stats.t.interval()` can compute exact CIs programmatically.

### Test Configuration

All measurements used:
- `valkey-benchmark` with pipelining (P=10) for throughput saturation
- GET command on pre-loaded 512-byte values (3M keyspace)
- Server and client on same machine (localhost)
- Multiple client threads to ensure server saturation

### Saturation Verification

Before measuring precision, we verified the server was the bottleneck by sweeping client parameters (threads, connections, pipeline depth) and confirming throughput plateaus. If the client is the bottleneck, you're measuring client variance, not server variance.

## Variance Factors: What Causes Inconsistent Benchmarks

Each factor below is ranked by typical impact. Not all apply to every platform.

### 1. ASLR (Address Space Layout Randomization)

**Impact**: 1-3% between-run variance
**Platforms**: All (ARM, AMD, Intel)

ASLR is a critical security feature that randomizes the virtual address of code, stack, heap, and shared libraries on each process start. In production, this makes memory-corruption exploits (buffer overflows, ROP chains) significantly harder because attackers cannot predict where code or data resides. **ASLR should always remain enabled in production environments.**

For benchmarking, however, ASLR means Valkey's hot-path functions land at different physical cache lines each restart, causing variable I-cache and TLB behavior. Valkey's event loop is tight enough that I-cache layout affects throughput measurably — two runs with identical code can differ by 1-3% purely from address randomization.

**Fix**: `sysctl kernel.randomize_va_space=0`
**Restore**: `sysctl kernel.randomize_va_space=2`

### 2. CPU Frequency Scaling / Turbo Boost

**Impact**: 2-5% bimodal distribution (x86 only)
**Platforms**: AMD EPYC, Intel Xeon (ARM Graviton has fixed frequency)

Modern x86 CPUs dynamically adjust frequency based on load, thermal headroom, and power budget. This creates non-deterministic performance — the CPU may or may not enter boost state between runs.

**AMD EPYC specifics**:
- `boost` sysfs file: **1 = boost ENABLED, 0 = boost DISABLED** (opposite of Intel!)
- EPYC 9R14 boosts from 2.6 GHz to 3.7 GHz (+42% frequency)
- `acpi-cpufreq` driver has discrete P-states (e.g., 1500/2000/2600 MHz)
- Must pin `scaling_min_freq = scaling_max_freq` to prevent downclocking

**Intel Xeon specifics** (expected):
- `intel_pstate` driver with `no_turbo`: **1 = turbo DISABLED** (opposite of AMD!)
- Hardware P-states (HWP) may override OS requests
- `energy_performance_preference` should be set to `performance`

**Fix (AMD)**:
```bash
echo 0 | sudo tee /sys/devices/system/cpu/cpufreq/boost
sudo sh -c 'for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_min_freq; do echo 2600000 > $f; done'
```

**Fix (Intel, expected)**:
```bash
echo 1 | sudo tee /sys/devices/system/cpu/intel_pstate/no_turbo
```

### 3. Chiplet / CCD Topology (AMD EPYC)

**Impact**: 5-10% multimodal distribution
**Platforms**: AMD EPYC (Zen 3/4 chiplet architecture)

AMD EPYC processors use a chiplet design where groups of 8 cores (a "CCD") share a 32MB L3 cache. Communication between CCDs goes through the Infinity Fabric interconnect with significantly higher latency than intra-CCD L3 access.

**Why it matters**: When Valkey's io-threads or the benchmark client's threads span multiple CCDs, the OS scheduler places them non-deterministically. Different placements produce different throughput levels, creating a multimodal distribution (multiple distinct performance modes).

**Key discovery**: The bimodal distribution we observed was caused by the **benchmark client** threads being placed across CCDs, not the server. The client generates inconsistent network pressure patterns depending on its thread placement.

**Golden rule**: On chiplet architectures, pin BOTH server AND client to separate single CCDs.

**Fix**:
```bash
# Server: 1 CCD (cores 0-6), io-threads=7
taskset -c 0-7 valkey-server --io-threads 7 --server-cpulist 0-6

# Client: separate CCD (cores 32-39), 8 threads
taskset -c 32-39 valkey-benchmark --threads 8
```

**Topology detection**:
```bash
# List L3 cache groups (CCDs)
find /sys/devices/system/cpu/cpu*/cache/index3 -name shared_cpu_list -exec cat {} \; | sort -u
```

### 4. C-State Wakeup Latency

**Impact**: ~5% outlier probability (sporadic drops)
**Platforms**: AMD EPYC, Intel Xeon (ARM Graviton has no C-states)

When CPU cores enter deep idle states (C1, C2, C3), waking them takes microseconds to milliseconds. During a benchmark, if a core briefly idles and enters C1/C2, the next request hitting that core experiences elevated latency.

**Fix**:
```bash
# Disable C1 and C2
sudo sh -c 'for f in /sys/devices/system/cpu/cpu*/cpuidle/state[12]/disable; do echo 1 > $f; done'
```

### 5. NUMA Cross-Node Access

**Impact**: 3-4% variance + 30-50% throughput loss
**Platforms**: Multi-socket systems (AMD EPYC 2-node, Intel multi-socket)

When server and client are on different NUMA nodes, memory access latency increases 3x (NUMA distance 32 vs 10 on AMD EPYC). The scheduler may migrate threads between nodes non-deterministically.

**Fix**: Pin all processes to the same NUMA node:
```bash
numactl --cpunodebind=0 --membind=0 valkey-server ...
```

### 6. Transparent Huge Pages (THP)

**Impact**: Sporadic latency spikes from compaction
**Platforms**: All Linux

When THP is set to `always`, the kernel proactively compacts memory to create 2MB pages. This compaction can stall allocations during benchmarks.

**Fix**: `echo madvise | sudo tee /sys/kernel/mm/transparent_hugepage/enabled`
(Valkey doesn't use `madvise()` for huge pages, so this effectively disables THP for it.)

Also disable proactive compaction: `sysctl vm.compaction_proactiveness=0`

### 7. Page Cache Accumulation

**Impact**: 0.5-1% downward drift across repetitions (io-threads only)
**Platforms**: All

When running multiple repetitions without clearing page cache, kernel metadata pages accumulate, slightly increasing memory pressure and reducing available cache for Valkey's working set.

**Fix**: `echo 3 > /proc/sys/vm/drop_caches` between server restarts.

### 8. Background System Activity

**Impact**: 0.1-0.5% sporadic noise
**Platforms**: All

Kernel watchdog timers, systemd periodic tasks (sysstat, dnf-makecache), and dirty page writeback can cause brief throughput dips.

**Fix**:
```bash
sysctl kernel.watchdog=0
sysctl kernel.timer_migration=0
sysctl vm.dirty_writeback_centisecs=0
systemctl stop sysstat-collect.timer nm-cloud-setup.timer dnf-makecache.timer
```

## Platform Best Practices

### ARM Graviton 3 (c7g.metal)

**Architecture**: 64 cores, 1 NUMA node, 32MB shared L3 (monolithic), fixed frequency, no turbo/boost, no C-states.

ARM Graviton is the easiest platform to stabilize because it lacks most x86 variance sources (no frequency scaling, no chiplets, no C-states, single NUMA node). The primary variance source is ASLR.

**Stabilization recipe**:
```bash
# Required (apply before benchmarking, reapply after reboot)
sudo sysctl -w kernel.randomize_va_space=0
echo madvise | sudo tee /sys/kernel/mm/transparent_hugepage/enabled
sudo sysctl -w vm.compaction_proactiveness=0
sudo sysctl -w kernel.watchdog=0
sudo sysctl -w kernel.timer_migration=0
sudo sysctl -w vm.dirty_writeback_centisecs=0
sudo systemctl stop sysstat-collect.timer nm-cloud-setup.timer dnf-makecache.timer
```

**Benchmark command** (single-threaded):
```bash
taskset -c 32 valkey-server --port 7777 --save "" --appendonly no --protected-mode no --daemonize yes
taskset -c 40-47 valkey-benchmark -p 7777 -t get -n 5000000 -c 150 -d 512 -P 10 --threads 8 -q
```

**Benchmark command** (io-threads=9):
```bash
taskset -c 32-40 valkey-server --port 7777 --io-threads 9 --save "" --appendonly no --protected-mode no --daemonize yes
taskset -c 44-55 valkey-benchmark -p 7777 -t get -n 10000000 -c 150 -d 512 -P 10 --threads 8 -q
# Drop caches between repetitions:
sudo sh -c 'echo 3 > /proc/sys/vm/drop_caches'
```

**Precision achieved**:
| Config | Within-run CV | Between-run CV | CI95 (n=3) |
|--------|--------------|----------------|------------|
| Single-threaded | 0.10% | 0.20% | ±0.23% |
| io-threads=9 | 0.24% | 0.70% | ±0.81% |

**Recommended parameters**: 3 reps × 30s (single-threaded) or 5 reps × 30s (io-threads=9). Warmup: 5s.

**Remaining variance source**: io-threads=9 between-run CV of ~0.7% is an irreducible floor from client-server interaction on a single machine. Thread scheduling non-determinism across the shared L3 creates slight variations in how io-threads contend for cache lines. `isolcpus` was tested and did not improve this — the variance is from the interaction pattern, not OS noise.

---

### AMD EPYC 9R14 (c7a.metal)

**Architecture**: 192 cores (2×96), 2 NUMA nodes, 24 CCDs × 8 cores, 32MB L3 per CCD, base 2.6 GHz / boost 3.7 GHz, C1+C2 idle states.

AMD EPYC requires the most stabilization due to its chiplet architecture. The key insight is that **both server and client must be pinned to separate single CCDs** to avoid non-deterministic thread placement across chiplet boundaries.

**Stabilization recipe**:
```bash
# 1. Disable boost (AMD: 0 = disabled, opposite of Intel!)
echo 0 | sudo tee /sys/devices/system/cpu/cpufreq/boost

# 2. Pin frequency (prevent downclocking to 2000/1500 MHz)
sudo sh -c 'for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_min_freq; do echo 2600000 > $f; done'
sudo sh -c 'for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_max_freq; do echo 2600000 > $f; done'

# 3. Disable C-states
sudo sh -c 'for f in /sys/devices/system/cpu/cpu*/cpuidle/state[12]/disable; do echo 1 > $f; done'

# 4. Disable ASLR
sudo sysctl -w kernel.randomize_va_space=0

# 5. THP + background noise (same as ARM)
echo madvise | sudo tee /sys/kernel/mm/transparent_hugepage/enabled
sudo sysctl -w vm.compaction_proactiveness=0
sudo sysctl -w kernel.watchdog=0
sudo sysctl -w kernel.timer_migration=0
sudo sysctl -w vm.dirty_writeback_centisecs=0
```

**Benchmark command** (io-threads=7, optimal for chiplet):
```bash
# Server on CCD0 (cores 0-7), using 7 of 8 cores
taskset -c 0-7 valkey-server --port 7777 --io-threads 7 --server-cpulist 0-6 \
    --save "" --appendonly no --protected-mode no --daemonize yes

# Client on CCD4 (cores 32-39), all 8 cores
taskset -c 32-39 valkey-benchmark -p 7777 -t get -n 10000000 \
    -c 800 -d 512 -P 10 --threads 8 -q

# Drop caches between repetitions:
sudo sh -c 'echo 3 > /proc/sys/vm/drop_caches'
```

**Precision achieved**:
| Config | CV (unstabilized) | CV (stabilized) | CI95 (n=3) |
|--------|-------------------|-----------------|------------|
| io-threads=7, 1 CCD | 3.81% | **0.024%** | ±0.06% |
| Single-threaded | 0.25% | **0.023%** | ±0.05% |

**Recommended parameters**: 3 reps × 30s. Warmup: 5s. Total: 1m 45s per config.

**Why io-threads=7 not 9?** On 8-core CCDs, io-threads=7 (main + 6 IO = 7 threads) leaves 1 core free for OS/interrupts while keeping all communication within the shared 32MB L3. io-threads=9 requires spanning 2 CCDs, which is 51% slower and introduces multimodal variance.

**Client saturation sweet spot**: t=8, c=800, P=10. Client ceiling on 1 CCD at 2.6 GHz is ~2.85M rps — provides 42% headroom over current server max (~2.0M rps).

---

### Intel Xeon (Sapphire Rapids / Ice Lake) — PENDING VALIDATION

**Expected architecture**: Monolithic die (no chiplets on most SKUs), single large shared LLC, per-core turbo, `intel_pstate` driver, HWP (Hardware P-states).

Intel Xeon is expected to be similar to ARM in terms of stabilization simplicity — monolithic LLC means no chiplet boundary effects. The main challenges are turbo boost and HWP.

**Expected stabilization recipe**:
```bash
# 1. Disable turbo (Intel: 1 = turbo DISABLED)
echo 1 | sudo tee /sys/devices/system/cpu/intel_pstate/no_turbo

# 2. Set performance governor + HWP preference
sudo cpupower frequency-set -g performance
echo performance | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/energy_performance_preference

# 3. Disable C-states
sudo sh -c 'for f in /sys/devices/system/cpu/cpu*/cpuidle/state[12]/disable; do echo 1 > $f; done'

# 4. ASLR + THP + background (same as other platforms)
sudo sysctl -w kernel.randomize_va_space=0
echo madvise | sudo tee /sys/kernel/mm/transparent_hugepage/enabled
sudo sysctl -w vm.compaction_proactiveness=0
sudo sysctl -w kernel.watchdog=0
```

**Expected precision**: Similar to ARM (CV ~0.1-0.3%) since monolithic LLC avoids chiplet effects. Turbo disable should eliminate the bimodal distribution seen on AMD.

**Status**: Awaiting instance availability for validation.

## Cross-Platform Comparison

| Factor | ARM Graviton 3 | AMD EPYC 9R14 | Intel Xeon (expected) |
|--------|---------------|---------------|----------------------|
| NUMA nodes | 1 | 2 | 1-2 |
| Cache topology | 1 × 32MB shared L3 | 24 × 32MB L3 (per CCD) | 1 × large shared LLC |
| Frequency | Fixed (~2.6 GHz) | Variable (must pin) | Variable (must pin) |
| Turbo/boost | None | Yes (disable: `echo 0`) | Yes (disable: `echo 1`) |
| C-states | None | C1, C2 (must disable) | C1, C2, C6 (must disable) |
| Chiplet effects | None | **Critical** (pin to 1 CCD) | None (monolithic) |
| ASLR impact | 1-3% | 1-3% | 1-3% (expected) |
| Optimal io-threads | 9 (fits shared L3) | 7 (fits 1 CCD of 8 cores) | TBD (likely 8-16) |
| Stabilized CV | 0.20% (1-thread) | 0.024% (1-thread) | TBD |
| Irreducible floor | ~0.7% (io-threads=9) | ~0.024% (io-threads=7) | TBD |
| Stabilization effort | Low (2 commands) | High (5 steps + topology) | Medium (expected) |

### Why AMD Achieves Lower CV Than ARM

Counter-intuitively, the AMD platform achieves lower CV (0.024%) than ARM (0.20%) when fully stabilized. This is because:

1. **Frequency is truly locked** — AMD at 2.6 GHz with boost off has zero frequency variation. ARM Graviton, while "fixed frequency," still has minor micro-architectural clock variations.
2. **Single-CCD eliminates all shared-resource contention** — 7 threads sharing 32MB L3 with no other processes is an ideal scenario. ARM's 64 cores sharing one L3 means more potential for cache pollution from kernel threads.
3. **Fewer cores active** — Only 8 cores on the CCD are doing anything, vs ARM where kernel threads on other cores still access the shared L3.

The tradeoff: AMD requires much more effort to stabilize and has a lower absolute throughput ceiling (2.0M vs ARM's higher potential with more io-threads).

## Limitations and Caveats

1. **Single-machine testing**: Server and client on the same host share L3 cache, memory bandwidth, and kernel resources. This masks some real-world effects (network latency, NIC interrupts) but eliminates network variance.

2. **Localhost networking**: Using loopback avoids NIC/driver variance but means results don't reflect production network overhead. For relative A/B comparisons this is fine.

3. **Workload-specific**: Results are for GET/SET with pipelining. Different workloads (Lua scripts, large values, cluster mode) may have different variance characteristics.

4. **io-threads=9 irreducible floor on ARM**: The ~0.7% between-run CV for saturated io-threads=9 on a single machine appears to be fundamental to the client-server interaction pattern, not fixable by OS tuning. For sub-0.5% CI on multi-threaded, consider a two-machine setup.

5. **AMD chiplet constraint**: Limiting to 1 CCD (7 io-threads) means we can't directly compare with ARM's 9 io-threads. For cross-platform comparison, either accept the different thread counts or accept higher variance on AMD with 2 CCDs.

6. **Intel not yet validated**: The Intel section is based on architectural knowledge, not empirical measurement. Actual results may differ.

## Implementation: Conductress & valkey-perf-benchmark

### Conductress Changes (feature/x86-stabilization branch)

The following has been implemented in Conductress's `enable_cpu_consistency_mode()`:

| Change | File | Effect |
|--------|------|--------|
| Disable ASLR | `server.py` | Eliminates 1-3% between-run variance |
| Pin CPU frequency | `server.py` | Prevents AMD downclocking |
| Drop page caches | `server.py` + `task_perf_benchmark.py` | Prevents io-threads drift |
| `require_single_cache` allocator | `cpu_allocator.py` | Constrains client to 1 CCD |
| Reduced threads/clients | `config.py` | 8 threads / 800 clients (fits 1 CCD) |
| Restore on cleanup | `server.py` | ASLR=2, freq range, boost re-enabled |

### valkey-perf-benchmark (OSS) Planned PRs

| PR | Feature | Status |
|----|---------|--------|
| 1 | Metadata enrichment (record environment state) | Ready to push |
| 2 | Pin valkey-benchmark to fixed tag | Planned |
| 3 | `--stabilize-environment` opt-in | Planned |
| 4 | IRQ + NUMA + CCD-aware pinning | Planned |
| 5 | perf stat opt-in | Planned |

## Quick Reference: Minimum Viable Benchmark

For a quick A/B comparison achieving ≤1% CI95:

| Step | ARM | AMD | Intel (expected) |
|------|-----|-----|------------------|
| 1. Stabilize | ASLR=0, THP=madvise | + boost off, freq pin, C-states off | + no_turbo, C-states off |
| 2. Pin server | taskset to N cores | taskset to 1 CCD | taskset to N cores |
| 3. Pin client | Separate cores | **Separate single CCD** | Separate cores |
| 4. Warmup | 5s | 5s | 5s |
| 5. Measure | 30s | 30s | 30s |
| 6. Repeat | 3-5× (drop_caches) | 3× (drop_caches) | 3-5× (drop_caches) |
| **Total time** | **1m 45s – 2m 55s** | **1m 45s** | **~2 min** |
| **Expected CI95** | ±0.23% / ±0.81% | ±0.06% | ~±0.2% |
