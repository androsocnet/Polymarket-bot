import argparse
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import time
import uuid


TERMINAL = {'COMPLETED', 'BLOCKED', 'FAILED', 'WATCHDOG_KILLED', 'SUPERVISOR_INTERRUPTED'}


def inspect(path):
    records = []
    partial = False
    invalid = None
    execution = None
    started = False
    terminal = None
    with open(path, 'rb') as stream:
        for index, line in enumerate(stream):
            try:
                row = json.loads(line)
                if not isinstance(row, dict) or not isinstance(row.get('event'), str):
                    raise ValueError('record shape')
                identity = row.get('execution_uuid')
                uuid.UUID(identity)
                if any(type(row.get(k)) not in (int, float) or not math.isfinite(row[k]) for k in ('utc', 'monotonic')):
                    raise ValueError('timestamps')
                if index == 0:
                    if row['event'] != 'PREPARED' or not isinstance(row.get('command'), list) or not row['command'] or not all(isinstance(v, str) for v in row['command']):
                        raise ValueError('PREPARED command')
                    if not isinstance(row.get('db'), str) or type(row.get('boundary')) is not int or type(row.get('parent_pid')) is not int or type(row.get('supervisor_pid')) is not int:
                        raise ValueError('PREPARED identity')
                    if type(row.get('watchdog')) not in (int, float) or not math.isfinite(row['watchdog']) or row['watchdog'] <= 0:
                        raise ValueError('PREPARED watchdog')
                    execution = identity
                elif identity != execution or (terminal is not None and row['event'] != 'LATE_SIGNAL') or row['event'] == 'PREPARED':
                    raise ValueError('execution/order')
                if row['event'] == 'LATE_SIGNAL':
                    if terminal is None or row.get('phase') != 'terminal_commit' or not isinstance(row.get('signals'), list) or not row['signals'] or not all(type(sig) is int for sig in row['signals']):
                        raise ValueError('late signal observation')
                if row['event'] == 'STARTED':
                    if started or type(row.get('child_pid')) is not int or row['child_pid'] <= 0:
                        raise ValueError('STARTED PID')
                    started = True
                if row['event'] in TERMINAL:
                    if type(row.get('child_stopped')) is not bool or 'returncode' not in row or (row['returncode'] is not None and type(row['returncode']) is not int):
                        raise ValueError('terminal fields')
                    if row['child_stopped'] != (row['returncode'] is not None) or (row['child_stopped'] and not started):
                        raise ValueError('terminal observation')
                    if row['event'] in ('COMPLETED', 'BLOCKED') and row['returncode'] != 0:
                        raise ValueError('terminal exit')
                    terminal = row
                records.append(row)
            except (ValueError, UnicodeError, TypeError, AttributeError) as exc:
                partial = True
                invalid = dict(line=index + 1, reason=str(exc))
                break
    outcome = terminal['event'] if terminal and not partial else 'UNKNOWN'
    if not partial and terminal and any(row['event'] == 'LATE_SIGNAL' for row in records):
        outcome = 'SUPERVISOR_INTERRUPTED'
    return dict(outcome=outcome, records=records,
                partial=partial, invalid_record=invalid, reconciliation='observed terminal only; no process/PID inference; no retry')


def run(args):
    journal = Path(args.journal).resolve()
    db = Path(args.db).resolve()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command or not math.isfinite(args.watchdog) or args.watchdog <= 0:
        raise ValueError('explicit command and positive finite watchdog required')
    paths = [journal, Path(str(journal) + '.stdout'), Path(str(journal) + '.stderr')]
    db_paths = [Path(str(db) + suffix) for suffix in ('', '-wal', '-shm', '-journal', '.writer.lock', '.partial')]
    if any(p.exists() for p in paths + db_paths):
        raise FileExistsError('existing evidence or DB')
    execution = str(uuid.uuid4())
    guard = Path(str(db) + '.supervision.lock')
    with guard.open('x', encoding='utf-8') as lock:
        lock.write(json.dumps(dict(execution_uuid=execution, journal=str(journal))))
        lock.flush()
        os.fsync(lock.fileno())
    if any(p.exists() for p in paths + db_paths):
        raise FileExistsError('existing evidence or DB after reservation')
    events = queue.Queue(maxsize=256)
    process = None
    readers = []
    outputs = []
    interrupted = []
    old_handlers = {}
    with journal.open('x', encoding='utf-8') as log:
        def record(event, **fields):
            log.write(json.dumps(dict(event=event, execution_uuid=execution,
                                     utc=time.time(), monotonic=time.monotonic(), **fields)) + '\n')
            log.flush()
            os.fsync(log.fileno())

        def receive(name, pipe):
            try:
                while True:
                    chunk = os.read(pipe.fileno(), 4096)
                    if not chunk:
                        break
                    events.put((name, chunk, time.time()))
            except Exception as error:
                events.put(('STREAM_ERROR', type(error).__name__, time.time()))
            finally:
                pipe.close()
                events.put(('EOF', name, time.time()))

        def consume(item):
            name, data, received = item
            if name in ('STDOUT', 'STDERR'):
                target = outputs[0 if name == 'STDOUT' else 1]
                target.write(data)
                target.flush()
                os.fsync(target.fileno())
                if name == 'STDOUT':
                    stdout_hash.update(data)
                    classify(data)
                else:
                    stderr_hash.update(data)
                record(name, received_utc=received, data=data.decode('utf-8', errors='replace'),
                       base64=base64.b64encode(data).decode('ascii'))
            elif name == 'STREAM_ERROR':
                record(name, error=data)
                raise OSError('child stream read failed')

        def stop_child(reason):
            if process is not None and process.poll() is None:
                try:
                    record('KILL_REQUESTED', reason=reason, child_pid=process.pid)
                finally:
                    process.kill()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    record('KILL_WAIT_EXPIRED', child_pid=process.pid)

        def handle(signum, frame):
            interrupted.append(signum)

        def check_interrupt():
            if interrupted or Path(str(journal) + '.interrupt').exists():
                raise KeyboardInterrupt

        classification = 'NO_RESULT'
        semantic_reason = None
        result_line = None
        line_number = 0
        pending = bytearray()
        oversized = False
        stdout_hash = hashlib.sha256()
        stderr_hash = hashlib.sha256()

        def classify(chunk):
            nonlocal classification, semantic_reason, result_line, line_number, oversized
            for piece in chunk.splitlines(keepends=True):
                if not oversized:
                    if len(pending) + len(piece) > 65536:
                        oversized = True
                        pending.clear()
                        classification = 'UNAVAILABLE_OVERSIZE'
                        semantic_reason = None
                        result_line = None
                    else:
                        pending.extend(piece)
                if piece.endswith(b'\n'):
                    line_number += 1
                    if not oversized:
                        try:
                            value = json.loads(pending)
                        except RecursionError:
                            value = None
                            classification = 'UNAVAILABLE_DECODER_RECURSION'
                            semantic_reason = None
                            result_line = None
                        except (ValueError, UnicodeError):
                            value = None
                        if isinstance(value, dict) and isinstance(value.get('result'), dict):
                            classification = 'LAST_TOP_LEVEL_RESULT'
                            semantic_reason = value['result'].get('reason')
                            result_line = line_number
                    pending.clear()
                    oversized = False

        record('PREPARED', command=command, planned_child=dict(command=command, role='bounded child'),
               db=str(db), boundary=args.boundary, parent_pid=os.getppid(), supervisor_pid=os.getpid(),
               watchdog=args.watchdog, kill_wait_seconds=15, cwd=os.getcwd(),
               evidence_paths=[str(p) for p in paths], guard=str(guard))
        state = 'FAILED'
        error = None
        try:
            for sig in (signal.SIGINT, signal.SIGTERM):
                old_handlers[sig] = signal.signal(sig, handle)
            for path in paths[1:]:
                outputs.append(path.open('xb'))
            started = time.monotonic()
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, close_fds=True)
            record('STARTED', child_pid=process.pid)
            deadline = started + args.watchdog
            for name, pipe in (('STDOUT', process.stdout), ('STDERR', process.stderr)):
                thread = threading.Thread(target=receive, args=(name, pipe), daemon=True)
                readers.append(thread)
                thread.start()
            state = 'COMPLETED'
            while process.poll() is None:
                if interrupted or Path(str(journal) + '.interrupt').exists():
                    record('SIGNAL', signals=interrupted, interrupt_file=bool(Path(str(journal) + '.interrupt').exists()))
                    state = 'SUPERVISOR_INTERRUPTED'
                    stop_child(state)
                    break
                if time.monotonic() >= deadline:
                    record('WATCHDOG_EXPIRED', child_pid=process.pid)
                    state = 'WATCHDOG_KILLED'
                    stop_child(state)
                    break
                try:
                    consume(events.get(timeout=min(.02, max(0, deadline - time.monotonic()))))
                except queue.Empty:
                    pass
            drain_deadline = time.monotonic() + 15
            while any(t.is_alive() for t in readers) or not events.empty():
                check_interrupt()
                if time.monotonic() >= drain_deadline:
                    raise TimeoutError('stream collection exceeded 15 seconds')
                try:
                    consume(events.get(timeout=.02))
                except queue.Empty:
                    pass
            check_interrupt()
            if pending:
                classify(b'\n')
            hashes = {str(paths[1]): stdout_hash.hexdigest(), str(paths[2]): stderr_hash.hexdigest()}
            check_interrupt()
            if state == 'COMPLETED' and process.returncode != 0:
                state = 'FAILED'
            if state == 'COMPLETED' and classification == 'LAST_TOP_LEVEL_RESULT' and semantic_reason == 'opening_blocked':
                state = 'BLOCKED'
        except BaseException as exc:
            error = type(exc).__name__
            state = 'SUPERVISOR_INTERRUPTED' if isinstance(exc, (KeyboardInterrupt, SystemExit)) else 'FAILED'
            if state == 'SUPERVISOR_INTERRUPTED':
                record('SIGNAL', signals=interrupted, error=error)
            stop_child(state)
        finally:
            try:
                for output in outputs:
                    try:
                        output.flush()
                        os.fsync(output.fileno())
                    finally:
                        output.close()
                check_interrupt()
            except BaseException as exc:
                error = type(exc).__name__
                state = 'SUPERVISOR_INTERRUPTED' if isinstance(exc, (KeyboardInterrupt, SystemExit)) else 'FAILED'
                record('SIGNAL' if state == 'SUPERVISOR_INTERRUPTED' else 'FINALIZATION_ERROR', error=error)
                stop_child(state)
            signals_before_commit = len(interrupted)
            try:
                record(state, returncode=process.returncode if process is not None else None,
                       error=error, child_stopped=process is not None and process.returncode is not None,
                       evidence_hashes={str(paths[1]): stdout_hash.hexdigest(), str(paths[2]): stderr_hash.hexdigest()},
                       classification=classification, semantic_reason=semantic_reason, result_line=result_line,
                       classification_record_limit=65536, process_only=state == 'COMPLETED')
            finally:
                for sig, previous in old_handlers.items():
                    signal.signal(sig, previous)
                if len(interrupted) > signals_before_commit:
                    record('LATE_SIGNAL', phase='terminal_commit', signals=interrupted[signals_before_commit:])
    return 0


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='action', required=True)
    for action in ('run', 'launch'):
        p = sub.add_parser(action)
        p.add_argument('--journal', required=True)
        p.add_argument('--db', required=True)
        p.add_argument('--boundary', type=int, required=True)
        p.add_argument('--watchdog', type=float, required=True)
        p.add_argument('command', nargs=argparse.REMAINDER)
    p = sub.add_parser('inspect')
    p.add_argument('journal')
    args = parser.parse_args()
    if args.action == 'inspect':
        print(json.dumps(inspect(args.journal)))
        return 0
    if args.action == 'launch':
        flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_BREAKAWAY_FROM_JOB) if os.name == 'nt' else 0
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'run', *sys.argv[2:]],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True, creationflags=flags, start_new_session=os.name != 'nt')
        return 0
    return run(args)


if __name__ == '__main__':
    raise SystemExit(main())
