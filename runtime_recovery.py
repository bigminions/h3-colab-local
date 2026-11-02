"""Observe an owned in-flight Jupyter execution without submitting another one."""
import queue
import time
import requests


class TaskCancelled(RuntimeError):
    pass


class ReconnectExhausted(RuntimeError):
    pass


class CancellableWait:
    """Keep the client's event semantics while checking cancellation when quiet."""
    def __init__(self, event, check_cancelled):
        self.event, self.check_cancelled = event, check_cancelled

    def set(self):
        return self.event.set()

    def clear(self):
        return self.event.clear()

    def is_set(self):
        return self.event.is_set()

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + max(0, timeout)
        while True:
            self.check_cancelled()
            remaining = None if deadline is None else max(0, deadline - time.monotonic())
            ready = self.event.wait(.25 if remaining is None else min(.25, remaining))
            self.check_cancelled()
            if ready or deadline is not None and time.monotonic() >= deadline:
                return ready


def cancellable_receive(receive, check_cancelled):
    """Cover shell replies and recovery reads as well as interactive output."""
    def get_msg(timeout=None):
        deadline = None if timeout is None else time.monotonic() + max(0, timeout)
        while True:
            check_cancelled()
            remaining = None if deadline is None else max(0, deadline - time.monotonic())
            try:
                message = receive(timeout=.25 if remaining is None else min(.25, remaining))
            except (queue.Empty, TimeoutError):
                check_cancelled()
                if deadline is not None and time.monotonic() >= deadline:
                    raise
                continue
            check_cancelled()
            return message
    return get_msg


def enable_cancellation(runtime, check_cancelled):
    # The installed jupyter-kernel-client waits on _message_received for the
    # full execution timeout. stop_channels() does not wake that event. Adapt
    # only this runtime's waits; never modify the installed dependency globally.
    check_cancelled()
    client = runtime.kernel_client._manager.client
    check_cancelled()
    client._message_received = CancellableWait(client._message_received, check_cancelled)
    for name in ('iopub_channel', 'stdin_channel', 'shell_channel', 'control_channel'):
        channel = getattr(client, name)
        channel.get_msg = cancellable_receive(channel.get_msg, check_cancelled)


def connection_failure(exc):
    if isinstance(exc, (requests.ConnectionError, requests.Timeout, ConnectionError, TimeoutError)):
        return True
    # colab_cli raises ColabRequestError instead of requests.HTTPError.
    response = getattr(exc, 'response', None)
    if response is not None and getattr(response, 'status_code', None) in (408,429,500,502,503,504):
        return True
    return isinstance(exc, RuntimeError) and str(exc).strip() in (
        'Connection was lost.', 'Connection was lost', 'Connection is closed.')


def observe_execution(runtime, session_id, output_hook, timeout=4500):
    client = runtime.kernel_client._manager.client
    deadline = time.monotonic() + timeout
    outputs = []
    last_poll = time.monotonic()
    while time.monotonic() < deadline:
        try:
            message = client.get_iopub_msg(timeout=5)
        except queue.Empty:
            now = time.monotonic()
            if now - last_poll >= 10:
                last_poll = now
                if getattr(runtime, 'url', None) and getattr(runtime, 'token', None) and getattr(runtime, 'kernel_id', None):
                    try:
                        import requests
                        r = requests.get(runtime.url.rstrip('/') + '/api/kernels',
                                         params={'authuser': '0', 'colab-runtime-proxy-token': runtime.token},
                                         headers={'X-Colab-Client-Agent': 'colab-cli', 'X-Colab-Runtime-Proxy-Token': runtime.token},
                                         timeout=10)
                        if r.status_code == 200:
                            kernels = r.json()
                            target_k = next((k for k in kernels if k.get('id') == runtime.kernel_id), None)
                            if target_k and target_k.get('execution_state') == 'idle':
                                return outputs
                    except Exception:
                        pass
            continue
        # Never consume another notebook client's outputs as this task's result.
        if message.get('parent_header', {}).get('session') != session_id:
            continue
        kind, content = message.get('msg_type'), message.get('content', {})
        if kind == 'status' and content.get('execution_state') == 'idle':
            return outputs
        if kind == 'stream':
            output = dict(output_type='stream', text=content.get('text',''))
        elif kind == 'error':
            output = dict(output_type='error', **content)
        else:
            continue
        outputs.append(output)
        output_hook(output)
    raise RuntimeError('恢复跟踪超时；原运行时保留，请稍后继续恢复')
