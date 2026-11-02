"""Bound cache websocket waits when a Colab runtime disappears."""
import queue
import threading
import time
import requests


def execute_monitored(runtime, client, endpoint, code, **kwargs):
    finished = queue.Queue()
    def execute():
        try:
            finished.put((True, runtime.execute_code(code, **kwargs)))
        except Exception as exc:
            finished.put((False, exc))
    threading.Thread(target=execute, daemon=True).start()
    deadline, check_after, misses = time.time() + 7200, time.time() + 60, 0
    while True:
        try:
            ok, result = finished.get(timeout=15)
            if not ok:
                raise result
            return result
        except queue.Empty:
            if time.time() >= deadline:
                raise RuntimeError('缓存操作超过两小时，停止等待并释放所属运行时')
            if time.time() >= check_after:
                try:
                    present = any(a.endpoint == endpoint for a in client.list_assignments())
                    misses = 0 if present else misses + 1
                    if misses >= 3:
                        raise RuntimeError('缓存 CPU 已被回收，已保存的 Drive 模型可直接复用')
                except requests.RequestException:
                    pass
                check_after = time.time() + 60
