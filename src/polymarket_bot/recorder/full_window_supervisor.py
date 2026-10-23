"""CP6.3 Windows lifecycle backend. Public entry remains blocked by resource gates.

Offline workers only until all-artifact/socket/strict-total-CPU contracts verified.
"""
import ctypes
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from uuid import uuid4

from .full_window_guard import (ArtifactBudget, BoundedLog, DurableJournal,
                                FullWindowJob, MIB, TerminalReserve)
from .windows_job import WindowsJob, _Accounting, _Extended


def _api():
    if os.name != 'nt':
        raise OSError('windows_job_required')
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    signatures = {
        'OpenProcess': ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
        'GetCurrentProcess': ([], w.HANDLE),
        'WaitForSingleObject': ([w.HANDLE, w.DWORD], w.DWORD),
        'WaitForMultipleObjects': ([w.DWORD, ctypes.POINTER(w.HANDLE), w.BOOL, w.DWORD], w.DWORD),
        'CloseHandle': ([w.HANDLE], w.BOOL),
        'CreateEventW': ([ctypes.c_void_p, w.BOOL, w.BOOL, w.LPCWSTR], w.HANDLE),
        'OpenEventW': ([w.DWORD, w.BOOL, w.LPCWSTR], w.HANDLE),
        'SetEvent': ([w.HANDLE], w.BOOL),
        'OpenJobObjectW': ([w.DWORD, w.BOOL, w.LPCWSTR], w.HANDLE),
        'TerminateJobObject': ([w.HANDLE, w.UINT], w.BOOL),
        'TerminateProcess': ([w.HANDLE, w.UINT], w.BOOL),
        'QueryInformationJobObject': ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p], w.BOOL),
        'CreateFileW': ([w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE], w.HANDLE),
    }
    for name, (args, result) in signatures.items():
        getattr(api, name).argtypes, getattr(api, name).restype = args, result
    return api


def wait_process_exit(pid, milliseconds):
    api = _api()
    handle = api.OpenProcess(0x100000, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # No longer exists. Access denied is NOT proof of exit.
            return True
        raise ctypes.WinError(error)
    try:
        result = api.WaitForSingleObject(handle, milliseconds)
        if result == 0xffffffff:
            raise ctypes.WinError(ctypes.get_last_error())
        return result == 0
    finally:
        api.CloseHandle(handle)


def process_alive(pid):
    return not wait_process_exit(pid, 0)


def exclusive_reopen(path):
    api = _api()
    handle = api.CreateFileW(str(Path(path).resolve()), 0x80000000, 0, None, 3, 0, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    WindowsJob._check(api.CloseHandle(handle))


def locking_processes(path):
    """Restart Manager diagnostic. Empty result does not prove no kernel/filter handle."""
    class UniqueProcess(ctypes.Structure):
        _fields_ = [('pid', w.DWORD), ('started', w.FILETIME)]

    class ProcessInfo(ctypes.Structure):
        _fields_ = [('process', UniqueProcess), ('name', w.WCHAR*256),
                    ('service', w.WCHAR*64), ('kind', ctypes.c_int),
                    ('status', w.ULONG), ('session', w.DWORD), ('restartable', w.BOOL)]

    api = ctypes.WinDLL('rstrtmgr', use_last_error=True)
    api.RmStartSession.argtypes = [ctypes.POINTER(w.DWORD), w.DWORD, w.LPWSTR]
    api.RmRegisterResources.argtypes = [w.DWORD, w.UINT, ctypes.POINTER(w.LPCWSTR),
                                      w.UINT, ctypes.c_void_p, w.UINT, ctypes.c_void_p]
    api.RmGetList.argtypes = [w.DWORD, ctypes.POINTER(w.UINT), ctypes.POINTER(w.UINT),
                             ctypes.POINTER(ProcessInfo), ctypes.POINTER(w.DWORD)]
    api.RmEndSession.argtypes = [w.DWORD]
    session = w.DWORD()
    key = ctypes.create_unicode_buffer(33)
    def check(code):
        if code:
            raise ctypes.WinError(code)
    check(api.RmStartSession(ctypes.byref(session), 0, key))
    try:
        paths = (w.LPCWSTR*1)(str(Path(path).resolve()))
        check(api.RmRegisterResources(session, 1, paths, 0, None, 0, None))
        needed, count, reasons = w.UINT(), w.UINT(), w.DWORD()
        result = api.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), None, ctypes.byref(reasons))
        if result == 0:
            return []
        if result != 234:
            check(result)
        if needed.value > 1024:
            raise OSError('too_many_lock_owners')
        rows = (ProcessInfo*needed.value)()
        count.value = needed.value
        # One bounded snapshot; concurrent changes fail visibly, never retry cleanup.
        check(api.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), rows, ctypes.byref(reasons)))
        return [rows[i].process.pid for i in range(count.value)]
    finally:
        check(api.RmEndSession(session))


def _self_memory_budget():
    # Spawn recorder/watchdog BEFORE assignment: this Job admits supervisor only.
    job = WindowsJob()
    limits = _Extended()
    limits.basic.flags = 0x108  # PROCESS_MEMORY | ACTIVE_PROCESS, no kill-on-close self-kill.
    limits.basic.active_limit = 1
    limits.process_memory = 128*MIB
    try:
        job._check(job.api.SetInformationJobObject(job.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
        job._check(job.api.AssignProcessToJobObject(job.handle, _api().GetCurrentProcess()))
        readback = _Extended()
        job._check(job.api.QueryInformationJobObject(job.handle, 9, ctypes.byref(readback), ctypes.sizeof(readback), None))
        if readback.process_memory != 128*MIB or readback.basic.flags != 0x108:
            raise OSError('supervisor_memory_readback_mismatch')
        return job
    except BaseException:
        job.close()
        raise


def _watchdog(job_name, cancel_name, parent_pid, kill_ns, hard_ns):
    """Independent process: Job kill even if parent holds GIL or blocks in kernel I/O."""
    api = _api()
    parent = job = cancel = None
    own_budget = None
    try:
        parent = api.OpenProcess(0x100001, False, parent_pid)  # SYNCHRONIZE | TERMINATE
        job = api.OpenJobObjectW(0x0c, False, job_name)  # QUERY | TERMINATE only
        cancel = api.OpenEventW(0x100000, False, cancel_name)
        for handle in (parent, job, cancel):
            WindowsJob._check(handle)
        own_budget = _self_memory_budget()
        print('ARMED', flush=True)
        handles = (w.HANDLE*2)(parent, cancel)
        killed = False
        while True:
            now = time.monotonic_ns()
            if now >= hard_ns:
                WindowsJob._check(api.TerminateJobObject(job, 124))
                WindowsJob._check(api.TerminateProcess(parent, 124))
                return 124
            accounting = _Accounting()
            WindowsJob._check(api.QueryInformationJobObject(job, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None))
            # Native Job time caps user CPU; this sampled sum is NOT hard total CPU cap.
            if not killed and (now >= kill_ns or accounting.user+accounting.kernel >= 120*10_000_000):
                WindowsJob._check(api.TerminateJobObject(job, 124))
                killed = True
            remaining_ms = max(1, min(10, (hard_ns-now)//1_000_000))
            result = api.WaitForMultipleObjects(2, handles, False, remaining_ms)
            if result == 0:  # Parent died; do not keep Job alive waiting for hard deadline.
                WindowsJob._check(api.TerminateJobObject(job, 124))
                return 124
            if result == 1:
                WindowsJob._check(api.QueryInformationJobObject(job, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None))
                if accounting.active:
                    WindowsJob._check(api.TerminateJobObject(job, 124))
                    return 125
                return 0
            if result != 258:
                raise ctypes.WinError(ctypes.get_last_error())
    finally:
        for handle in (cancel, job, parent):
            if handle:
                api.CloseHandle(handle)
        if own_budget:
            own_budget.close()


def supervise(command, evidence_dir, *, deadlines):
    """Real STOP/kill/outer-hard timers; internal backend, not live launch permission.

    Caller supplies immutable launch-anchored Deadlines; tests shorten only time.
    Missing terminal commit after parent death/hang is explicitly unknown evidence.
    """
    api = _api()
    started = time.monotonic_ns()
    stop_ns, kill_ns, hard_ns = deadlines.stop_ns, deadlines.kill_ns, deadlines.hard_ns
    if not started < stop_ns < kill_ns < hard_ns <= started+950_000_000_000:
        raise ValueError('invalid_launch_deadlines')
    root = Path(evidence_dir)
    root.mkdir(mode=0o700, parents=False, exist_ok=False)
    terminal = journal = output = job = own_budget = process = watchdog = None
    cancel = None
    stop_timer = None
    reader = None
    stop_requested = threading.Event()
    log_failed = threading.Event()
    forced = False
    try:
        terminal = TerminalReserve(root/'terminal.bin')
        budget = ArtifactBudget()
        journal = DurableJournal(root/'supervisor.jsonl', budget=budget)
        output = BoundedLog(root/'child.log', budget)
        name = 'Local\\CP63-'+uuid4().hex
        job = FullWindowJob(name)
        cancel_name = name+'-cancel'
        cancel = api.CreateEventW(None, True, False, cancel_name)
        WindowsJob._check(cancel)
        environment = dict(os.environ, __PYVENV_LAUNCHER__=sys.executable, CP63_SUPERVISOR_JOB=name)
        bootstrap = ('import sys;sys.path.insert(0,'+repr(str(Path(__file__).resolve().parents[2]))+');'
                     'from polymarket_bot.recorder.full_window_supervisor import _watchdog;'
                     'raise SystemExit(_watchdog(*'+repr((name, cancel_name, os.getpid(), kill_ns, hard_ns))+'))')
        watchdog = subprocess.Popen([sys._base_executable, '-B', '-c', bootstrap], env=environment,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        # Parent-side startup hard timer covers failed watchdog bootstrap. Not independent.
        bootstrap_timer = threading.Timer(max(0, (hard_ns-time.monotonic_ns())/1e9), os._exit, args=(124,))
        bootstrap_timer.daemon = True; bootstrap_timer.start()
        try:
            if watchdog.stdout.readline().strip() != b'ARMED':
                raise OSError('watchdog_not_armed')
        finally:
            bootstrap_timer.cancel(); bootstrap_timer.join()
            watchdog.stdout.close()
        journal.write('watchdog_armed', kill_ns=kill_ns, hard_ns=hard_ns)
        if command[0] == sys.executable:
            command = [sys._base_executable, *command[1:]]
        process = subprocess.Popen(command, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
        job.assign(process)
        own_budget = _self_memory_budget()
        process.stdin.write(b'PROVE\n'); process.stdin.flush()
        if process.stdout.readline(32).strip() != b'MEMBER':
            raise OSError('child_membership_not_proven')
        journal.write('membership_before_go', pid=process.pid)

        def drain_log():
            try:
                while True:
                    data = os.read(process.stdout.fileno(), 65536)
                    if not data:
                        break
                    output.write(data)
            except (OSError, ValueError):
                log_failed.set()
                job.terminate()

        reader = threading.Thread(target=drain_log, daemon=True); reader.start()

        def stop():
            stop_requested.set()
            try:
                process.stdin.write(b'STOP\n'); process.stdin.flush()
            except (OSError, ValueError):
                # Exit/crash is accounted by wait; no successful STOP commit claimed.
                pass

        stop_timer = threading.Timer(max(0, (stop_ns-time.monotonic_ns())/1e9), stop)
        stop_timer.daemon = True; stop_timer.start()
        if time.monotonic_ns() >= stop_ns:
            raise TimeoutError('startup_stop_deadline_exhausted')
        journal.write('go_intent')
        process.stdin.write(b'GO\n'); process.stdin.flush()
        process.wait(timeout=max(.001, (hard_ns-time.monotonic_ns())/1e9))
        forced = process.returncode == 124
        # Terminate Job even after root exits; active count alone not handle proof.
        job.terminate()
        reader.join(timeout=max(.001, (hard_ns-time.monotonic_ns())/1e9))
        if reader.is_alive():
            raise TimeoutError('log_pipe_not_drained')
        process.stdout.close(); process.stdin.close()
        active = job.active()
        if active:
            raise OSError('job_cleanup_unproven')
        stop_timer.cancel(); stop_timer.join()
        job.close()
        output.close()
        journal.write('terminal_intent', active_processes=active, log_failed=log_failed.is_set())
        journal.close()
        for path in (root/'child.log', root/'supervisor.jsonl'):
            exclusive_reopen(path)
        result = dict(child_exit_code=process.returncode, forced=forced,
                      stop_requested=stop_requested.is_set(), active_processes=active,
                      elapsed_seconds=(time.monotonic_ns()-started)/1e9,
                      membership_before_go=True, exclusive_reopen=True,
                      watchdog_exit_code=None, log_failed=log_failed.is_set(),
                      supervisor_memory_limit=128*MIB,
                      cpu_policy='sampled_user_plus_kernel_native_user_limit',
                      runtime_evidence_verified=False, cleanup_proven=False)
        # Keep independent hard kill through terminal I/O and handle closure.
        terminal.commit(result)
        terminal.close()
        exclusive_reopen(root/'terminal.bin')
        WindowsJob._check(api.SetEvent(cancel))
        watchdog.wait(timeout=max(.001, (hard_ns-time.monotonic_ns())/1e9))
        if watchdog.returncode != 0:
            raise OSError('watchdog_cleanup_unproven')
        result['watchdog_exit_code'] = watchdog.returncode
        return result
    finally:
        if stop_timer:
            stop_timer.cancel()
        if process:
            if process.poll() is None:
                if job and job.handle:
                    job.terminate()
                process.kill()
            process.wait(timeout=max(.001, (hard_ns-time.monotonic_ns())/1e9))
            for stream in (process.stdin, process.stdout):
                if stream and not stream.closed:
                    stream.close()
        if reader:
            reader.join(timeout=max(.001, (hard_ns-time.monotonic_ns())/1e9))
        if job:
            job.close()
        if watchdog and watchdog.poll() is None:
            if cancel:
                api.SetEvent(cancel)
            watchdog.wait(timeout=max(.001, (hard_ns-time.monotonic_ns())/1e9))
        if cancel:
            api.CloseHandle(cancel)
        for resource in (output, journal, terminal, own_budget):
            if resource:
                resource.close()
