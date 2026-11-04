"""Local media organized by application, purpose and media kind."""
import json
import mimetypes
from pathlib import Path
import re
import threading
import uuid

EXTENSIONS = {'images': {'png', 'jpg', 'webp'}, 'videos': {'mp4'},
              'audios': {'wav', 'mp3', 'm4a', 'ogg', 'flac', 'aac'}}
ASSET_ID = re.compile(r'^[0-9a-f]{32}\.(png|jpg|webp|mp4|wav|mp3|m4a|ogg|flac|aac)$')
SEGMENT = re.compile(r'^[a-z][a-z0-9_-]{0,39}$')


class AssetLibrary:
    def __init__(self, root):
        self.root = Path(root)
        self.lock = threading.RLock()

    @staticmethod
    def validate_segment(value):
        if not isinstance(value, str) or not SEGMENT.fullmatch(value):
            raise ValueError('素材应用或用途不合法')
        return value

    def folder(self, kind, app_id, purpose):
        if kind not in EXTENSIONS:
            raise ValueError('素材类型不存在')
        target = self.root / self.validate_segment(app_id) / self.validate_segment(purpose) / kind
        if not target.resolve().is_relative_to(self.root.resolve()):
            raise ValueError('素材路径不合法')
        return target

    def kind(self, asset_id):
        if not ASSET_ID.fullmatch(asset_id):
            raise ValueError('素材编号不合法')
        extension = asset_id.rsplit('.', 1)[1]
        return next(kind for kind, extensions in EXTENSIONS.items() if extension in extensions)

    def target(self, asset_id, app_id, purpose):
        return self.folder(self.kind(asset_id), app_id, purpose) / asset_id

    def _folders(self, kind=None, app_id=None, purpose=None):
        if kind is not None and kind not in EXTENSIONS:
            raise ValueError('素材类型不存在')
        if app_id is not None: self.validate_segment(app_id)
        if purpose is not None: self.validate_segment(purpose)
        if not self.root.is_dir(): return
        for app in self.root.iterdir():
            if not app.is_dir() or app.is_symlink() or not SEGMENT.fullmatch(app.name) or app_id is not None and app.name != app_id:
                continue
            for usage in app.iterdir():
                if not usage.is_dir() or usage.is_symlink() or not SEGMENT.fullmatch(usage.name) or purpose is not None and usage.name != purpose:
                    continue
                for media_kind in EXTENSIONS if kind is None else (kind,):
                    folder = self.folder(media_kind, app.name, usage.name)
                    if folder.is_dir() and not folder.is_symlink():
                        yield folder, app.name, usage.name, media_kind

    def resolve(self, asset_id):
        kind = self.kind(asset_id)
        found = [folder / asset_id for folder, *_ in self._folders(kind)
                 if (folder / asset_id).is_file() and not (folder / asset_id).is_symlink()]
        if not found:
            raise FileNotFoundError('素材不存在：' + asset_id)
        if len(found) != 1:
            raise ValueError('素材编号重复，请检查素材目录：' + asset_id)
        return found[0]

    def exists(self, asset_id):
        try:
            self.resolve(asset_id)
            return True
        except FileNotFoundError:
            return False

    def store(self, asset_id, data, name=None, metadata=None, *, app_id='shared', purpose='imports'):
        with self.lock:
            if self.exists(asset_id):
                raise ValueError('素材编号已存在')
            target = self.target(asset_id, app_id, purpose)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix('.' + uuid.uuid4().hex + '.partial')
            try:
                temporary.write_bytes(data)
                temporary.replace(target)
                if name or metadata:
                    self._write_metadata(target, {'name': name or asset_id, **(metadata or {})})
            finally:
                temporary.unlink(missing_ok=True)
            return target

    def _write_metadata(self, path, metadata):
        stat = path.stat()
        info = {**metadata, 'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}
        target = path.with_suffix(path.suffix + '.json')
        temporary = target.with_suffix('.' + uuid.uuid4().hex + '.tmp')
        try:
            temporary.write_text(json.dumps(info, ensure_ascii=False), encoding='utf-8')
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)

    def list(self, kind=None, jobs=(), *, app_id=None, purpose=None):
        by_file = {job.get('file') or (job.get('id', '') + '.mp4'): job for job in jobs
                   if (job.get('file') or job.get('drive')) and not job.get('history_hidden')}
        items, seen = [], set()
        for folder, application, usage, media_kind in self._folders(kind, app_id, purpose):
            for path in folder.iterdir():
                if not path.is_file() or path.is_symlink() or not ASSET_ID.fullmatch(path.name) or self.kind(path.name) != media_kind:
                    continue
                if path.name in seen:
                    continue
                seen.add(path.name)
                stat = path.stat()
                try:
                    metadata = json.loads(path.with_suffix(path.suffix + '.json').read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    metadata = {}
                job = by_file.get(path.name, {})
                job_config = job.get('config') or {}
                items.append({'id': path.name, 'kind': media_kind,
                              'folder': folder.relative_to(self.root).as_posix(),
                              'app_id': application, 'purpose': usage,
                              'name': job_config.get('title') or metadata.get('name') or path.name,
                              'size': stat.st_size, 'created': job.get('created', stat.st_mtime),
                              'job_id': job.get('id'), 'local': True,
                              'url': '/api/assets/' + path.name + '/file'})
        for name, job in by_file.items():
            job_config = job.get('config') or {}
            application = job_config.get('app_id', 'h3')
            if name in seen or kind not in (None, 'videos') or app_id is not None and application != app_id or purpose not in (None, 'outputs'):
                continue
            items.append({'id': name, 'kind': 'videos', 'folder': application + '/outputs/videos',
                          'app_id': application, 'purpose': 'outputs',
                          'name': job_config.get('title') or name,
                          'size': (job.get('drive') or {}).get('size', 0), 'created': job.get('created', 0),
                          'job_id': job.get('id'), 'local': False, 'recoverable': bool(job.get('drive')),
                          'url': '/video/' + name})
        return sorted(items, key=lambda item: item['created'], reverse=True)

    def media_type(self, asset_id):
        self.kind(asset_id)
        return mimetypes.guess_type(asset_id)[0] or 'application/octet-stream'
