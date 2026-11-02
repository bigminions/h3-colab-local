"""Model cache for an explicitly authorized Colab Drive mount."""
import hashlib
import json
from pathlib import Path
import time


class MountedDriveModelCache:
    def __init__(self, emit, root='/content/drive/MyDrive/H3-Model-Cache'):
        self.emit = emit
        self.root = Path(root)
        self.metrics = []

    def paths(self, identity):
        key = identity['key']
        if len(key) != 64 or any(char not in '0123456789abcdef' for char in key):
            raise ValueError('缓存版本标识不合法')
        folder = self.root / key
        if not folder.resolve().is_relative_to(self.root.resolve()):
            raise ValueError('缓存目录不能指向 H3-Model-Cache 以外')
        return folder / Path(identity['filename']).name, folder / 'verified.json'

    def lookup(self, identity):
        path, manifest = self.paths(identity)
        try:
            record = json.loads(manifest.read_text())
            return path if record == identity and path.stat().st_size == identity['size'] else None
        except (OSError, ValueError):
            return None

    def copy_verified(self, source, target, identity, operation):
        partial = target.with_name(target.name + '.partial')
        target.parent.mkdir(parents=True, exist_ok=True)
        started = last = time.monotonic()
        digest, size = hashlib.sha256(), 0
        self.emit('prepare', operation + '：' + target.name)
        try:
            with source.open('rb') as source_file, partial.open('wb') as output:
                for chunk in iter(lambda: source_file.read(8 * 1024**2), b''):
                    if getattr(self, 'cancel_check', None) and self.cancel_check():
                        raise RuntimeError('下载已取消')
                    output.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                    if time.monotonic() - last >= 15:
                        elapsed = max(time.monotonic() - started, .001)
                        self.emit('prepare', '%s %s：%.2f / %.2f GB，%.1f MB/s，已用时 %.0f 秒' %
                                  (operation, target.name, size / 1e9, identity['size'] / 1e9,
                                   size / 1e6 / elapsed, elapsed))
                        last = time.monotonic()
            if size != identity['size'] or digest.hexdigest() != identity['sha256']:
                raise RuntimeError('模型大小或 SHA-256 不符：' + target.name)
            partial.replace(target)
            elapsed = max(time.monotonic() - started, .001)
            metric = dict(operation=operation, filename=target.name, bytes=size,
                          seconds=round(elapsed, 3), mb_per_second=round(size / 1e6 / elapsed, 2), verified=True)
            self.metrics.append(metric)
            self.emit('prepare', '%s完成 %s：%.1f 秒，%.1f MB/s' %
                      (operation, target.name, elapsed, metric['mb_per_second']), cache_metric=metric)
        finally:
            partial.unlink(missing_ok=True)

    def restore(self, target, identity):
        source = self.lookup(identity)
        if source is None:
            return False
        self.copy_verified(source, Path(target), identity, 'Drive 读取')
        return True

    def store(self, target, identity):
        if self.lookup(identity):
            self.emit('prepare', 'Drive 已有缓存：' + Path(target).name)
            return
        destination, manifest = self.paths(identity)
        self.copy_verified(Path(target), destination, identity, 'Drive 写入（含挂载缓存，待同步）')
        # A fresh runtime restore checks cloud persistence independently.
        with destination.open('rb') as source:
            if hashlib.file_digest(source, 'sha256').hexdigest() != identity['sha256']:
                raise RuntimeError('Drive 回读校验失败：' + destination.name)
        temporary = manifest.with_name('verified.json.partial')
        temporary.write_text(json.dumps(identity))
        temporary.replace(manifest)

    def flush(self):
        from google.colab import drive
        started = time.monotonic()
        self.emit('prepare', '等待 Drive 完成云端同步；写入速度包含本地缓冲，实际总耗时以同步完成为准')
        drive.flush_and_unmount()
        metric = dict(operation='Drive 同步并卸载', seconds=round(time.monotonic() - started, 3))
        self.metrics.append(metric)
        self.emit('prepare', 'Drive 已完成同步并卸载，用时 %.1f 秒' % metric['seconds'], cache_metric=metric)
