from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


def pi_power_blocker(throttled_output: str) -> str:
    """Return a reason only for a *current* Pi power or throttle condition."""

    match = re.search(r"0x([0-9a-fA-F]+)", throttled_output)
    if match is None:
        return ""
    flags = int(match.group(1), 16)
    current = flags & 0xF
    if current & 0x1:
        return "background preparation paused: Raspberry Pi is undervolted"
    if current & 0x4:
        return "background preparation paused: Raspberry Pi is currently throttled"
    return ""


def memory_pressure_blocker(
    meminfo: str, *, min_available_mib: float = 1536.0
) -> str:
    """Pause optional generation before it competes with the UI or SSH."""

    match = re.search(r"^MemAvailable:\s+(\d+)\s+kB$", meminfo, re.MULTILINE)
    if match is None:
        return ""
    available_mib = int(match.group(1)) / 1024.0
    if available_mib < min_available_mib:
        return (
            "background preparation paused: only "
            f"{available_mib:.0f} MiB memory is available"
        )
    return ""


def background_preparation_blocker(
    max_temperature_c: float = 78.0,
    min_available_mib: float = 1536.0,
) -> str:
    """Protect interactive use while a small device is power- or heat-limited.

    Missing Pi-specific telemetry is treated as healthy so the same code works
    on development machines. Historical throttle flags do not block work after
    the electrical or thermal condition has cleared.
    """

    command = shutil.which("vcgencmd")
    if command:
        try:
            result = subprocess.run(
                [command, "get_throttled"],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            result = None
        if result is not None:
            blocker = pi_power_blocker(result.stdout)
            if blocker:
                return blocker

    thermal = Path("/sys/class/thermal/thermal_zone0/temp")
    try:
        temperature_c = float(thermal.read_text(encoding="ascii").strip()) / 1000.0
    except (OSError, ValueError):
        return ""
    if temperature_c >= max_temperature_c:
        return (
            "background preparation paused: device temperature is "
            f"{temperature_c:.1f} C"
        )
    try:
        meminfo = Path("/proc/meminfo").read_text(encoding="ascii")
    except OSError:
        return ""
    blocker = memory_pressure_blocker(
        meminfo, min_available_mib=min_available_mib
    )
    if blocker:
        return blocker
    return ""
