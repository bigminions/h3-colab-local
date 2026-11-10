"""Injected into the owned Colab execution; no extra server or public port.

The Jupyter Contents API remains available while Python is busy. It carries
status snapshots and scoped cancellation requests, independently of stdout.
"""


def consumer_run(operation, config, directory='/content', poll_seconds=1, interrupt=None):
    import json as _json
    import os as _os
    import signal as _signal
    import threading as _threading
    import time as _time
    from pathlib import Path as _Path
    import requests as _requests

    status_path = _Path(directory) / 'lstudio-consumer.json'
    cancel_path = _Path(directory) / 'lstudio-cancel.json'
    interrupt = interrupt or (lambda: _os.kill(_os.getpid(), _signal.SIGINT))
    state = dict(job_id=config['job_id'], app_id=config.get('app_id', 'h3'),
                 run_id=config['_consumer_run_id'], state='busy', stage='prepare',
                 prompt_id=None, running=[], pending=[], queue_known=False)
    lock = _threading.RLock()
    finished = _threading.Event()
    cancelled = _threading.Event()
    original_event = globals()['event']

    def snapshot():
        with lock:
            state['heartbeat'] = _time.time()
            tmp = status_path.with_suffix('.tmp')
            tmp.write_text(_json.dumps(state), encoding='utf-8')
            tmp.replace(status_path)

    def report(stage, message, **data):
        if stage in ('draft', 'debug'):
            return original_event(stage, message, **data)
        with lock:
            if stage != 'heartbeat':
                state['stage'] = stage
            if data.get('prompt_id'):
                state['prompt_id'] = data['prompt_id']
            if data.get('result'):
                state['result'] = data['result']
            snapshot()
        return original_event(stage, message, **data)

    def inspect_queue():
        try:
            response = _requests.get('http://127.0.0.1:8188/queue', timeout=2)
            response.raise_for_status()
            queue = response.json()
            if not all(isinstance(queue.get(k), list) for k in ('queue_running', 'queue_pending')):
                raise ValueError('Invalid generation queue')
            with lock:
                state.update(running=[item[1] for item in queue['queue_running']],
                             pending=[item[1] for item in queue['queue_pending']], queue_known=True)
        except _requests.ConnectionError:
            # No generation service is expected during initial environment setup.
            # Once a prompt has been submitted, loss of service is not proof of idle.
            with lock:
                state.update(running=[], pending=[], queue_known=not bool(state['prompt_id']))
        except Exception:
            with lock:
                state['queue_known'] = False

    def supervise():
        interrupt_sent = False
        while True:
            inspect_queue()
            try:
                request = _json.loads(cancel_path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                request = {}
            if request.get('run_id') == state['run_id'] and request.get('job_id') == state['job_id']:
                cancelled.set()
                with lock:
                    state['state'] = 'cancelling'
                    prompt = state['prompt_id']
                try:
                    if prompt and prompt in state['pending']:
                        _requests.post('http://127.0.0.1:8188/queue', json={'delete': [prompt]}, timeout=3).raise_for_status()
                    if prompt and prompt in state['running']:
                        _requests.post('http://127.0.0.1:8188/interrupt', json={'prompt_id': prompt}, timeout=3).raise_for_status()
                    if not interrupt_sent and not finished.is_set():
                        interrupt_sent = True
                        interrupt()
                except Exception as exc:
                    with lock:
                        state['cancel_error'] = type(exc).__name__
            with lock:
                if finished.is_set():
                    if cancelled.is_set():
                        state['state'] = 'cancelled'
                    snapshot()
                    return
                snapshot()
            _time.sleep(poll_seconds)

    globals()['event'] = report
    snapshot()
    monitor = _threading.Thread(target=supervise, daemon=True)
    monitor.start()
    try:
        result = operation(config)
        with lock:
            state['state'] = 'done'
        return result
    except BaseException:
        with lock:
            state['state'] = 'cancelled' if cancelled.is_set() else 'error'
        raise
    finally:
        finished.set()
        # Finish the observer before another cell can install a new observer.
        # The host independently probes ComfyUI after the kernel becomes idle.
        monitor.join()
        globals()['event'] = original_event
