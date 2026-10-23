"""Minimal Windows kill-on-close Job Object; no breakaway permission."""
import ctypes
from ctypes import wintypes as w
import os


class _Limits(ctypes.Structure):
    _fields_ = [('user', ctypes.c_int64), ('job_user', ctypes.c_int64),
                ('flags', w.DWORD), ('min_working', ctypes.c_size_t),
                ('max_working', ctypes.c_size_t), ('active_limit', w.DWORD),
                ('affinity', ctypes.c_size_t), ('priority', w.DWORD), ('scheduling', w.DWORD)]


class _IO(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in
                ('read_ops', 'write_ops', 'other_ops', 'read_bytes', 'write_bytes', 'other_bytes')]


class _Extended(ctypes.Structure):
    _fields_ = [('basic', _Limits), ('io', _IO), ('process_memory', ctypes.c_size_t),
                ('job_memory', ctypes.c_size_t), ('peak_process', ctypes.c_size_t),
                ('peak_job', ctypes.c_size_t)]


class _Accounting(ctypes.Structure):
    _fields_ = [('user', ctypes.c_int64), ('kernel', ctypes.c_int64),
                ('period_user', ctypes.c_int64), ('period_kernel', ctypes.c_int64),
                ('faults', w.DWORD), ('total', w.DWORD), ('active', w.DWORD), ('terminated', w.DWORD)]


class WindowsJob:
    def __init__(self, name=None):
        if os.name != 'nt':
            raise OSError('windows_job_required')
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        signatures = {
            'CreateJobObjectW': ([ctypes.c_void_p, w.LPCWSTR], w.HANDLE),
            'SetInformationJobObject': ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
            'QueryInformationJobObject': ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p], w.BOOL),
            'AssignProcessToJobObject': ([w.HANDLE, w.HANDLE], w.BOOL),
            'TerminateJobObject': ([w.HANDLE, w.UINT], w.BOOL),
            'CloseHandle': ([w.HANDLE], w.BOOL),
        }
        for function_name, (args, result) in signatures.items():
            function = getattr(self.api, function_name)
            function.argtypes, function.restype = args, result
        self.handle = self.api.CreateJobObjectW(None, name)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = _Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        try:
            self._check(self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _check(ok):
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())

    def assign(self, process):
        self._check(self.api.AssignProcessToJobObject(self.handle, int(process._handle)))

    def terminate(self):
        self._check(self.api.TerminateJobObject(self.handle, 124))

    def active(self):
        accounting = _Accounting()
        self._check(self.api.QueryInformationJobObject(self.handle, 1, ctypes.byref(accounting),
                                                      ctypes.sizeof(accounting), None))
        return accounting.active

    def close(self):
        if self.handle:
            self._check(self.api.CloseHandle(self.handle))
            self.handle = None


def in_supervisor_job(name):
    """Child proves named parent job membership, then closes its query handle."""
    if os.name != 'nt' or not name:
        return False
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.OpenJobObjectW.argtypes, api.OpenJobObjectW.restype = [w.DWORD, w.BOOL, w.LPCWSTR], w.HANDLE
    api.IsProcessInJob.argtypes, api.IsProcessInJob.restype = [w.HANDLE, w.HANDLE, ctypes.POINTER(w.BOOL)], w.BOOL
    api.GetCurrentProcess.restype = w.HANDLE
    api.CloseHandle.argtypes, api.CloseHandle.restype = [w.HANDLE], w.BOOL
    handle = api.OpenJobObjectW(4, False, name)  # JOB_OBJECT_QUERY only
    if not handle:
        return False
    try:
        member = w.BOOL()
        return bool(api.IsProcessInJob(api.GetCurrentProcess(), handle, ctypes.byref(member)) and member.value)
    finally:
        api.CloseHandle(handle)
