"""Recoverable work bundles and Windows' recycle-only file operation."""
import ctypes
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import uuid


def recycle_to_bin(path):
    """Use IFileOperation's recycle-only flag; never fall back to permanent deletion."""
    path = Path(path).resolve(strict=True)
    if os.name != 'nt' or sys.getwindowsversion().major < 10:
        raise RuntimeError('系统回收箱需要 Windows 10 或更新版本')
    if ctypes.windll.kernel32.GetDriveTypeW(str(path.anchor)) != 3:
        raise RuntimeError('此磁盘不支持安全移入回收箱，作品已保留')

    class GUID(ctypes.Structure):
        _fields_ = [('data',ctypes.c_ubyte * 16)]
        def __init__(self, value):
            super().__init__(); self.data[:] = uuid.UUID(value).bytes_le

    def check(result):
        if result < 0:
            raise OSError(f'系统回收箱操作失败（0x{result & 0xffffffff:08X}）')

    def call(pointer, index, types=(), arguments=()):
        table = ctypes.cast(pointer,ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        return ctypes.WINFUNCTYPE(ctypes.c_long,ctypes.c_void_p,*types)(table[index])(pointer,*arguments)

    ole = ctypes.OleDLL('ole32'); shell = ctypes.WinDLL('shell32')
    ole.CoInitializeEx.argtypes = [ctypes.c_void_p,ctypes.c_ulong]
    ole.CoInitializeEx.restype = ctypes.c_long
    ole.CoCreateInstance.argtypes = [ctypes.POINTER(GUID),ctypes.c_void_p,ctypes.c_ulong,ctypes.POINTER(GUID),ctypes.POINTER(ctypes.c_void_p)]
    ole.CoCreateInstance.restype = ctypes.c_long
    shell.SHCreateItemFromParsingName.argtypes = [ctypes.c_wchar_p,ctypes.c_void_p,ctypes.POINTER(GUID),ctypes.POINTER(ctypes.c_void_p)]
    shell.SHCreateItemFromParsingName.restype = ctypes.c_long
    check(ole.CoInitializeEx(None,2 | 4))
    operation = ctypes.c_void_p(); item = ctypes.c_void_p()
    try:
        check(ole.CoCreateInstance(ctypes.byref(GUID('3ad05575-8857-4850-9277-11b85bdb8e09')),None,1,
            ctypes.byref(GUID('947aab5f-0a5c-4c13-b4d6-4bf7836fc9f8')),ctypes.byref(operation)))
        check(shell.SHCreateItemFromParsingName(str(path),None,ctypes.byref(GUID('43826d1e-e718-42ee-bc55-a1e261c37bfe')),ctypes.byref(item)))
        # RECYCLEONDELETE | EARLYFAILURE | ADDUNDORECORD | NOERRORUI | SILENT | NOCONFIRMATION
        check(call(operation,5,(ctypes.c_ulong,),(0x00080000 | 0x00100000 | 0x20000000 | 0x0400 | 0x0004 | 0x0010,)))
        check(call(operation,18,(ctypes.c_void_p,ctypes.c_void_p),(item,None)))
        check(call(operation,21))
        aborted = ctypes.c_int()
        check(call(operation,22,(ctypes.POINTER(ctypes.c_int),),(ctypes.byref(aborted),)))
        if aborted.value or path.exists():
            raise RuntimeError('未能移入系统回收箱，作品已保留')
    finally:
        if item: call(item,2)
        if operation: call(operation,2)
        ole.CoUninitialize()


class HistoryRecycle:
    def __init__(self, directory, videos, storage, sender=None):
        self.directory = Path(directory).resolve()
        self.videos = Path(videos).resolve()
        self.storage = storage
        self.sender = sender or recycle_to_bin

    def folder(self, name):
        folder = (self.directory / name).resolve()
        if folder.parent != self.directory or not name.startswith('H3-'):
            raise ValueError('无效的作品恢复目录')
        return folder

    def video(self, filename):
        if not re.fullmatch(r'[A-Za-z0-9_-]+\.mp4',filename):
            raise ValueError('无效的视频文件名')
        target = (self.videos / filename).resolve()
        if target.parent != self.videos:
            raise ValueError('视频路径超出保存目录')
        return target

    @staticmethod
    def digest(path):
        with path.open('rb') as handle:
            return hashlib.file_digest(handle,'sha256').hexdigest()

    def download(self, stored, target):
        if not isinstance(stored.get('size'),int) or not re.fullmatch(r'[0-9a-f]{64}',stored.get('sha256','')):
            raise RuntimeError('云端视频缺少校验记录，作品已保留')
        response = self.storage.open_download(stored)
        digest = hashlib.sha256(); size = 0
        try:
            if response.status_code != 200:
                raise RuntimeError('Drive 视频下载失败，作品已保留')
            with target.open('xb') as handle:
                for chunk in response.iter_content(1024 * 1024):
                    handle.write(chunk); digest.update(chunk); size += len(chunk)
        finally:
            response.close()
        if size != stored['size'] or digest.hexdigest() != stored['sha256']:
            raise RuntimeError('视频校验未通过，作品已保留')

    def prepare(self, job, keep_source=False):
        snapshot = json.loads(json.dumps(job))
        token = uuid.uuid4().hex
        self.directory.mkdir(parents=True,exist_ok=True)
        folder = self.folder('H3-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + token[:12])
        folder.mkdir()
        manifest = dict(version=1,token=token,job=snapshot,video_filename=None,source_moved=False)
        try:
            filename = job.get('file')
            source = self.video(filename) if filename else None
            if source and source.is_file():
                target = folder / filename
                manifest.update(video_filename=filename,source_moved=not keep_source,
                                video_size=source.stat().st_size,video_sha256=self.digest(source))
                (folder / 'work.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
                if keep_source: shutil.copy2(source,target)
                else: source.replace(target)
            elif job.get('drive'):
                filename = job['id'] + '.mp4'; self.video(filename)
                self.download(job['drive'],folder / filename)
                manifest['video_filename'] = filename
            elif job.get('file'):
                raise RuntimeError('找不到视频文件，作品记录已保留')
            if manifest['video_filename']:
                video = folder / manifest['video_filename']
                manifest.update(video_size=video.stat().st_size,video_sha256=self.digest(video))
            (folder / 'work.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
            (folder / '恢复说明.txt').write_text('在 Windows 回收箱中还原整个 H3 文件夹，再打开或刷新视频工作台。视频和配置记录会自动回到最近作品。\n视频文件也可以直接取出使用。',encoding='utf-8')
            return folder,manifest
        except Exception:
            self.rollback(folder,manifest)
            raise

    def put_video_back(self, folder, manifest):
        filename = manifest.get('video_filename')
        if not filename: return
        target = self.video(filename)
        source = folder / filename
        if source.resolve().parent != folder.resolve():
            raise RuntimeError('恢复文件夹中缺少视频，未覆盖现有记录')
        if not source.is_file():
            if target.is_file() and target.stat().st_size == manifest.get('video_size') and self.digest(target) == manifest.get('video_sha256'): return
            raise RuntimeError('恢复文件夹中缺少视频，未覆盖现有记录')
        if 'video_sha256' in manifest and (source.stat().st_size != manifest['video_size'] or self.digest(source) != manifest['video_sha256']):
            raise RuntimeError('恢复视频校验未通过，原记录已保留')
        self.videos.mkdir(parents=True,exist_ok=True)
        if target.exists():
            if target.stat().st_size != source.stat().st_size or self.digest(target) != self.digest(source):
                raise RuntimeError('原位置已有不同的视频，恢复文件夹已保留')
        else:
            source.replace(target)

    def remove_staging(self, folder):
        # Only a verified direct child of the task's recovery directory is removable.
        folder = self.folder(folder.name)
        if folder.exists(): shutil.rmtree(folder)

    def rollback(self, folder, manifest):
        if not folder.exists(): return
        if manifest.get('source_moved'): self.put_video_back(folder,manifest)
        self.remove_staging(folder)

    def send(self, folder):
        self.sender(self.folder(folder.name))

    def recover(self, jobs, busy=(), persist=None):
        changed = False
        for job in jobs.values():
            pending = job.get('history_recycle_pending')
            if not pending or job['id'] in busy: continue
            folder = self.folder(pending['folder'])
            if folder.exists():
                manifest = json.loads((folder / 'work.json').read_text(encoding='utf-8'))
                if manifest['token'] != pending['token']: continue
                if manifest.get('source_moved'): self.put_video_back(folder,manifest)
                job.pop('history_recycle_pending',None)
                if persist: persist()
                self.remove_staging(folder)
            else:
                job.update(history_hidden=True,history_recycle_token=pending['token'])
            job.pop('history_recycle_pending',None); changed = True
        if not self.directory.is_dir(): return changed
        for folder in self.directory.iterdir():
            if not folder.is_dir() or folder.is_symlink() or not folder.name.startswith('H3-'): continue
            try:
                folder = self.folder(folder.name)
                manifest = json.loads((folder / 'work.json').read_text(encoding='utf-8'))
                original = manifest['job']; job = jobs.get(original['id'])
                if not job or job['id'] in busy: continue
                if not job.get('history_hidden') and not job.get('history_recycle_pending'):
                    if manifest.get('source_moved') and job.get('file') == manifest.get('video_filename'):
                        self.put_video_back(folder,manifest)
                        self.remove_staging(folder)
                    continue
                if not job.get('history_hidden') or job.get('history_recycle_token') != manifest['token']: continue
                self.put_video_back(folder,manifest)
                restored = dict(original)
                for key in ('history_hidden','history_recycle_token','history_recycle_pending'): restored.pop(key,None)
                if manifest.get('video_filename'): restored['file'] = manifest['video_filename']
                jobs[job['id']] = restored; changed = True
                if persist: persist()
                self.remove_staging(folder)
            except (OSError,ValueError,KeyError,TypeError,RuntimeError):
                # A conflict or incomplete restored folder must never overwrite existing data.
                continue
        return changed
