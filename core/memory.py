# -*- coding: utf-8 -*-
"""Memory of the PC and of QGIS, and the memory an analysis needs.

Read with the standard library only (no psutil, which not every QGIS has):
Linux /proc, Windows kernel32 through ctypes, macOS libc/mach through
ctypes. Each reader returns None when it cannot read, and the callers then
go on without a memory check.

The estimate of an analysis is measured: the whole analysis was run on
0.5 m DEMs of two sizes (with catchments) and the peak of each step
divided by its cells.
"""

import ctypes
import ctypes.util
import math
import sys

# Bytes a cell of the acquired DEM keeps in memory through the analysis
# (the DEM as read and as prepared), and at its peak while being prepared.
ACQUIRED_BYTES_PER_CELL = 24
# Bytes a cell at the peak of each step, of the frame with its margin
# (hillshade, slope, the CS Map) or of the acquired DEM (contours).
CS_MAP_BYTES_PER_CELL = 370
SURFACE_BYTES_PER_CELL = 40
CONTOUR_BYTES_PER_CELL = 14
# The estimate is raised by this share to cover what was not measured
# (GDAL's cache, the GeoPDF DEM, Python objects).
ESTIMATE_ALLOWANCE = 1.2
# Cells around the frame for hillshade, slope and the CS Map (as in
# analyses/terrain.py).
SURFACE_MARGIN_CELLS = 96
# Width-to-height ratio of the A-series frame.
A_SERIES_RATIO = math.sqrt(2.0)
# An analysis needing more than this share of the free memory is asked about.
SAFE_SHARE_OF_FREE = 0.9


def system_memory():
    """(total, available) bytes of the PC's memory, or None."""
    try:
        if sys.platform.startswith("linux"):
            return _linux_system()
        if sys.platform == "win32":
            return _windows_system()
        if sys.platform == "darwin":
            return _mac_system()
    except Exception:  # noqa: BLE001 - no memory check rather than a failure
        return None
    return None


def process_memory():
    """Bytes of memory this process (QGIS) uses now, or None."""
    try:
        if sys.platform.startswith("linux"):
            return _linux_process()
        if sys.platform == "win32":
            return _windows_process()
        if sys.platform == "darwin":
            return _mac_process()
    except Exception:  # noqa: BLE001 - no memory check rather than a failure
        return None
    return None


def surface_cells(frame_cells):
    """Cells of the frame with its margin, from the frame's cells."""
    if frame_cells <= 0:
        return 0
    width = math.sqrt(frame_cells * A_SERIES_RATIO)
    height = frame_cells / width
    return (width + 2 * SURFACE_MARGIN_CELLS) * (height + 2 * SURFACE_MARGIN_CELLS)


def step_need(outputs, surface, acquired):
    """Bytes the heaviest step after reading the DEM adds: the CS Map,
    hillshade or slope (``surface`` cells) or contours (``acquired``)."""
    outputs = set(outputs or ())
    steps = [0.0]
    if "cs_map" in outputs:
        steps.append(CS_MAP_BYTES_PER_CELL * surface)
    if outputs & {"hillshade", "slope"}:
        steps.append(SURFACE_BYTES_PER_CELL * surface)
    if "contours" in outputs:
        steps.append(CONTOUR_BYTES_PER_CELL * acquired)
    return ESTIMATE_ALLOWANCE * max(steps)


def analysis_need(outputs, acquired, frame):
    """Bytes an analysis adds to QGIS's memory at its peak: the acquired DEM
    (``acquired`` cells) and the heaviest step (``frame`` cells, the area
    cut out at the end)."""
    held = ESTIMATE_ALLOWANCE * ACQUIRED_BYTES_PER_CELL * acquired
    return held + step_need(outputs, surface_cells(frame), acquired)


def too_much(need):
    """(too much, available bytes): whether ``need`` bytes more do not fit in
    the free memory with some room (None for available when unknown)."""
    memory = system_memory()
    if memory is None:
        return False, None
    available = memory[1]
    return need > available * SAFE_SHARE_OF_FREE, available


# Linux (also inside Flatpak, where /proc shows the PC's memory).

def _linux_system():
    values = {}
    with open("/proc/meminfo", encoding="ascii") as handle:
        for line in handle:
            key, _, rest = line.partition(":")
            if key in ("MemTotal", "MemAvailable"):
                values[key] = int(rest.split()[0]) * 1024
    if "MemTotal" not in values or "MemAvailable" not in values:
        return None
    return values["MemTotal"], values["MemAvailable"]


def _linux_process():
    with open("/proc/self/status", encoding="ascii") as handle:
        for line in handle:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    return None


# Windows (kernel32 through ctypes; no administrator rights needed).

class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _windows_system():
    status = _MemoryStatusEx()
    status.dwLength = ctypes.sizeof(_MemoryStatusEx)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return int(status.ullTotalPhys), int(status.ullAvailPhys)


def _windows_process():
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.K32GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(_ProcessMemoryCounters), wintypes.DWORD,
    ]
    counters = _ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(_ProcessMemoryCounters)
    if not kernel32.K32GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        return None
    return int(counters.WorkingSetSize)


# macOS (libc and mach through ctypes; no extra rights needed).

_HOST_VM_INFO64 = 4
_MACH_TASK_BASIC_INFO = 20


class _VmStatistics64(ctypes.Structure):
    _fields_ = [
        ("free_count", ctypes.c_uint32),
        ("active_count", ctypes.c_uint32),
        ("inactive_count", ctypes.c_uint32),
        ("wire_count", ctypes.c_uint32),
        ("zero_fill_count", ctypes.c_uint64),
        ("reactivations", ctypes.c_uint64),
        ("pageins", ctypes.c_uint64),
        ("pageouts", ctypes.c_uint64),
        ("faults", ctypes.c_uint64),
        ("cow_faults", ctypes.c_uint64),
        ("lookups", ctypes.c_uint64),
        ("hits", ctypes.c_uint64),
        ("purges", ctypes.c_uint64),
        ("purgeable_count", ctypes.c_uint32),
        ("speculative_count", ctypes.c_uint32),
        ("decompressions", ctypes.c_uint64),
        ("compressions", ctypes.c_uint64),
        ("swapins", ctypes.c_uint64),
        ("swapouts", ctypes.c_uint64),
        ("compressor_page_count", ctypes.c_uint32),
        ("throttled_count", ctypes.c_uint32),
        ("external_page_count", ctypes.c_uint32),
        ("internal_page_count", ctypes.c_uint32),
        ("total_uncompressed_pages_in_compressor", ctypes.c_uint64),
    ]


class _TimeValue(ctypes.Structure):
    _fields_ = [("seconds", ctypes.c_int), ("microseconds", ctypes.c_int)]


class _MachTaskBasicInfo(ctypes.Structure):
    _fields_ = [
        ("virtual_size", ctypes.c_uint64),
        ("resident_size", ctypes.c_uint64),
        ("resident_size_max", ctypes.c_uint64),
        ("user_time", _TimeValue),
        ("system_time", _TimeValue),
        ("policy", ctypes.c_int),
        ("suspend_count", ctypes.c_int),
    ]


_libc = None


def _mac_libc():
    global _libc
    if _libc is None:
        _libc = ctypes.CDLL(ctypes.util.find_library("c"))
    return _libc


def _mac_sysctl_uint64(name):
    value = ctypes.c_uint64(0)
    size = ctypes.c_size_t(ctypes.sizeof(value))
    if _mac_libc().sysctlbyname(
            name.encode("ascii"), ctypes.byref(value), ctypes.byref(size), None, 0) != 0:
        return None
    return int(value.value)


def _mac_system():
    libc = _mac_libc()
    total = _mac_sysctl_uint64("hw.memsize")
    if not total:
        return None
    libc.mach_host_self.restype = ctypes.c_uint
    page = ctypes.c_size_t(0)
    host = libc.mach_host_self()
    if libc.host_page_size(host, ctypes.byref(page)) != 0 or not page.value:
        return None
    stats = _VmStatistics64()
    count = ctypes.c_uint(ctypes.sizeof(_VmStatistics64) // 4)
    if libc.host_statistics64(
            host, _HOST_VM_INFO64, ctypes.byref(stats), ctypes.byref(count)) != 0:
        return None
    # Free, plus pages macOS gives up at once when asked.
    pages = (stats.free_count + stats.inactive_count
             + stats.speculative_count + stats.purgeable_count)
    return total, min(total, pages * page.value)


def _mac_process():
    libc = _mac_libc()
    task = ctypes.c_uint.in_dll(libc, "mach_task_self_").value
    info = _MachTaskBasicInfo()
    count = ctypes.c_uint(ctypes.sizeof(_MachTaskBasicInfo) // 4)
    if libc.task_info(
            task, _MACH_TASK_BASIC_INFO, ctypes.byref(info), ctypes.byref(count)) != 0:
        return None
    return int(info.resident_size)
