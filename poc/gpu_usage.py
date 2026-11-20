"""Sample worker-tree GPU memory on the single-GPU Windows phase-0 host."""
import ctypes as c
import re
from ctypes import wintypes as w

import psutil


class NvmlMemory(c.Structure):
    _fields_ = [(name, c.c_ulonglong) for name in ('total', 'free', 'used')]


class NvmlProcess(c.Structure):
    _fields_ = [('pid', c.c_uint), ('used', c.c_ulonglong),
                ('gpu_instance', c.c_uint), ('compute_instance', c.c_uint)]


class CounterValue(c.Structure):
    # PDH_FMT_COUNTERVALUE with the 64-bit integer union member selected.
    _fields_ = [('status', w.DWORD), ('value', c.c_longlong)]


class CounterItem(c.Structure):
    _fields_ = [('name', w.LPWSTR), ('data', CounterValue)]


class GpuSampler:
    def __init__(self):
        self.devices = []
        self.query = w.HANDLE()
        self.counter = w.HANDLE()
        self.errors = set()
        self.samples = 0
        self.dedicated = None
        self.compute = None
        self.compute_seen = False
        self.board = None
        self.nvml_open = False
        try:
            self.nvml = c.WinDLL('nvml.dll', winmode=0x800)
            self.require(self.nvml.nvmlInit_v2(), 'nvmlInit')
            self.nvml_open = True
            count = c.c_uint()
            self.require(self.nvml.nvmlDeviceGetCount_v2(c.byref(count)), 'nvmlDeviceGetCount')
            for index in range(count.value):
                handle = c.c_void_p()
                self.require(self.nvml.nvmlDeviceGetHandleByIndex_v2(index, c.byref(handle)), 'nvmlDeviceGetHandle')
                self.devices.append(handle)
        except (OSError, AttributeError) as exc:
            self.errors.add(str(exc))
        try:
            self.pdh = c.WinDLL('pdh.dll', winmode=0x800)
            self.require(self.pdh.PdhOpenQueryW(None, 0, c.byref(self.query)), 'PdhOpenQuery')
            self.require(self.pdh.PdhAddEnglishCounterW(self.query,
                r'\GPU Process Memory(*)\Dedicated Usage', 0, c.byref(self.counter)), 'PdhAddEnglishCounter')
        except (OSError, AttributeError) as exc:
            self.errors.add(str(exc))

    @staticmethod
    def require(status, name):
        if status != 0:
            raise OSError(f'{name} returned 0x{status & 0xffffffff:08x}')

    def wddm_usage(self, pids):
        self.require(self.pdh.PdhCollectQueryData(self.query), 'PdhCollectQueryData')
        size, count = w.DWORD(), w.DWORD()
        args = self.counter, 0x400, c.byref(size), c.byref(count)  # PDH_FMT_LARGE
        status = self.pdh.PdhGetFormattedCounterArrayW(*args, None) & 0xffffffff
        if status != 0x800007D2:  # PDH_MORE_DATA
            self.require(status, 'PdhGetFormattedCounterArray(size)')
        if not size.value:
            return
        buf = c.create_string_buffer(size.value)
        self.require(self.pdh.PdhGetFormattedCounterArrayW(*args, buf), 'PdhGetFormattedCounterArray')
        usage = 0
        for item in c.cast(buf, c.POINTER(CounterItem))[:count.value]:
            match = re.match(r'pid_(\d+)_', item.name or '')
            if match and int(match[1]) in pids and item.data.status in (0, 1):
                usage += item.data.value
        self.dedicated = max(self.dedicated or 0, usage)

    def sample(self, process):
        try:
            pids = {process.pid, *(p.pid for p in process.children(recursive=True))}
        except psutil.NoSuchProcess:
            return
        except psutil.AccessDenied as exc:
            self.errors.add(str(exc))
            return
        self.samples += 1
        board, compute = 0, 0
        board_valid = bool(self.devices)
        compute_valid = bool(self.devices)
        for device in self.devices:
            memory = NvmlMemory()
            try:
                self.require(self.nvml.nvmlDeviceGetMemoryInfo(device, c.byref(memory)), 'nvmlDeviceGetMemoryInfo')
                board += memory.used
            except (OSError, AttributeError) as exc:
                board_valid = False
                self.errors.add(str(exc))
            try:
                count = c.c_uint()
                query = self.nvml.nvmlDeviceGetComputeRunningProcesses_v3
                status = query(device, c.byref(count), None)
                if status == 7:  # NVML_ERROR_INSUFFICIENT_SIZE
                    capacity = c.c_uint(count.value + 8)
                    entries = (NvmlProcess * capacity.value)()
                    self.require(query(device, c.byref(capacity), entries), 'nvmlDeviceGetComputeRunningProcesses')
                    for row in entries[:capacity.value]:
                        if row.pid in pids:
                            self.compute_seen = True
                            if row.used == 2**64 - 1:
                                compute_valid = False
                                self.errors.add('NVML per-process bytes unavailable (WDDM)')
                            else:
                                compute += row.used
                else:
                    self.require(status, 'nvmlDeviceGetComputeRunningProcesses(size)')
            except (OSError, AttributeError) as exc:
                compute_valid = False
                self.errors.add(str(exc))
        if compute_valid and compute > 0:
            self.compute = max(self.compute or 0, compute)
        if board_valid:
            self.board = max(self.board or 0, board)
        if self.counter:
            try:
                self.wddm_usage(pids)
            except OSError as exc:
                self.errors.add(str(exc))

    def finish(self):
        if self.query:
            self.pdh.PdhCloseQuery(self.query)
        if self.nvml_open:
            self.nvml.nvmlShutdown()
        return dict(samples=self.samples, nvml_worker_seen=self.compute_seen,
                    nvml_worker_peak_bytes=self.compute, wddm_worker_dedicated_peak_bytes=self.dedicated,
                    nvml_device_used_peak_bytes=self.board, warnings=sorted(self.errors))
