"""Environment stabilization for consistent benchmark results.

Applies OS-level tuning to minimize run-to-run variance. Designed as opt-in
(--stabilize-environment) so it doesn't affect dashboard/historical runs.

All changes are reversed on cleanup via restore().
"""

import logging
import platform
import subprocess
from pathlib import Path
from typing import Optional


def _run(cmd: str, check: bool = False) -> Optional[str]:
    """Run a shell command, return stdout or None on failure."""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=10
        )
        if check and result.returncode != 0:
            logging.warning("Command failed: %s (rc=%d)", cmd, result.returncode)
            return None
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError) as e:
        logging.warning("Command error: %s: %s", cmd, e)
        return None


def _read_sysfs(path: str) -> Optional[str]:
    """Read a sysfs file, return content or None."""
    try:
        return Path(path).read_text().strip()
    except (OSError, PermissionError):
        return None


def _write_sysfs(path: str, value: str) -> bool:
    """Write to a sysfs file via sudo tee."""
    result = _run(f"echo {value} | sudo tee {path}")
    return result is not None


class EnvironmentStabilizer:
    """Applies and restores OS tuning for benchmark consistency."""

    def __init__(self):
        self._original_aslr: Optional[str] = None
        self._original_thp: Optional[str] = None
        self._original_compaction: Optional[str] = None
        self._original_watchdog: Optional[str] = None
        self._original_timer_migration: Optional[str] = None
        self._original_dirty_writeback: Optional[str] = None
        self._original_boost: Optional[str] = None
        self._original_min_freq: Optional[str] = None
        self._applied = False

    def apply(self) -> None:
        """Apply all stabilization settings. Safe to call multiple times."""
        if self._applied:
            return

        logging.info("Applying environment stabilization...")

        # --- Universal (all platforms) ---
        self._stabilize_aslr()
        self._stabilize_thp()
        self._stabilize_kernel_noise()
        self._drop_caches()

        # --- x86-specific ---
        if platform.machine() in ("x86_64", "amd64"):
            self._stabilize_frequency()
            self._stabilize_cstates()

        self._applied = True
        logging.info("Environment stabilization complete.")

    def restore(self) -> None:
        """Restore all original settings."""
        if not self._applied:
            return

        logging.info("Restoring environment settings...")

        # ASLR
        if self._original_aslr is not None:
            _run(f"sudo sysctl -w kernel.randomize_va_space={self._original_aslr}")

        # THP
        if self._original_thp is not None:
            _run(f"echo {self._original_thp} | sudo tee /sys/kernel/mm/transparent_hugepage/enabled")

        # Kernel noise
        if self._original_compaction is not None:
            _run(f"sudo sysctl -w vm.compaction_proactiveness={self._original_compaction}")
        if self._original_watchdog is not None:
            _run(f"sudo sysctl -w kernel.watchdog={self._original_watchdog}")
        if self._original_timer_migration is not None:
            _run(f"sudo sysctl -w kernel.timer_migration={self._original_timer_migration}")
        if self._original_dirty_writeback is not None:
            _run(f"sudo sysctl -w vm.dirty_writeback_centisecs={self._original_dirty_writeback}")

        # Frequency (x86)
        if self._original_boost is not None:
            boost_path = "/sys/devices/system/cpu/cpufreq/boost"
            if Path(boost_path).exists():
                _write_sysfs(boost_path, self._original_boost)
        if self._original_min_freq is not None:
            _run(
                f"sudo sh -c 'for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_min_freq;"
                f" do echo {self._original_min_freq} > $f; done'"
            )

        # C-states (re-enable)
        if platform.machine() in ("x86_64", "amd64"):
            _run(
                "sudo sh -c 'for f in /sys/devices/system/cpu/cpu*/cpuidle/state[123]/disable;"
                " do echo 0 > $f; done'"
            )

        self._applied = False
        logging.info("Environment restored.")

    def drop_caches(self) -> None:
        """Drop page caches. Call between server restarts."""
        self._drop_caches()

    # --- Private methods ---

    def _stabilize_aslr(self) -> None:
        self._original_aslr = _run("sysctl -n kernel.randomize_va_space")
        _run("sudo sysctl -w kernel.randomize_va_space=0")
        logging.info("Disabled ASLR")

    def _stabilize_thp(self) -> None:
        thp_path = "/sys/kernel/mm/transparent_hugepage/enabled"
        content = _read_sysfs(thp_path)
        if content:
            # Parse current setting from "[always] madvise never" format
            for word in content.replace("[", "").replace("]", "").split():
                if "[" + word + "]" in _read_sysfs(thp_path) or content.startswith("["):
                    pass
            # Just store raw and set madvise
            self._original_thp = "always"  # safe default
            if "[madvise]" in (content or ""):
                self._original_thp = "madvise"
            elif "[never]" in (content or ""):
                self._original_thp = "never"
            _run(f"echo madvise | sudo tee {thp_path}")
            logging.info("Set THP to madvise")

        _run("sudo sysctl -w vm.compaction_proactiveness=0")
        self._original_compaction = "20"  # kernel default

    def _stabilize_kernel_noise(self) -> None:
        self._original_watchdog = _run("sysctl -n kernel.watchdog") or "1"
        self._original_timer_migration = _run("sysctl -n kernel.timer_migration") or "1"
        self._original_dirty_writeback = _run("sysctl -n vm.dirty_writeback_centisecs") or "500"

        _run("sudo sysctl -w kernel.watchdog=0")
        _run("sudo sysctl -w kernel.timer_migration=0")
        _run("sudo sysctl -w vm.dirty_writeback_centisecs=0")
        logging.info("Disabled watchdog, timer_migration, dirty_writeback")

    def _stabilize_frequency(self) -> None:
        """x86: disable boost and pin frequency."""
        boost_path = "/sys/devices/system/cpu/cpufreq/boost"
        if Path(boost_path).exists():
            self._original_boost = _read_sysfs(boost_path)
            _write_sysfs(boost_path, "0")
            logging.info("Disabled CPU boost")

        # Pin min freq = max freq
        max_freq = _read_sysfs("/sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq")
        self._original_min_freq = _read_sysfs(
            "/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_min_freq"
        )
        if max_freq:
            _run(
                f"sudo sh -c 'for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_min_freq;"
                f" do echo {max_freq} > $f; done'"
            )
            logging.info("Pinned CPU frequency to %s kHz", max_freq)

    def _stabilize_cstates(self) -> None:
        """x86: disable C1/C2/C3 idle states."""
        _run(
            "sudo sh -c 'for f in /sys/devices/system/cpu/cpu*/cpuidle/state[123]/disable;"
            " do echo 1 > $f; done'"
        )
        logging.info("Disabled C-states")

    def _drop_caches(self) -> None:
        _run("sudo sh -c 'echo 3 > /proc/sys/vm/drop_caches'")
