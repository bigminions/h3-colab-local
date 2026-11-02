"""Drive is a transfer source; playback always serves a verified local file."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from starlette.requests import Request
from starlette.responses import FileResponse
import app


class VideoStreamTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=app.PRIVATE)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.id = 'b'*32
        self.data = b'local-video-data'
        self.control = app.Controller(self.path, self.path)
        self.job = {'id': self.id, 'status': 'done', 'drive': {'file_id': 'video', 'job_id': self.id,
            'size': len(self.data), 'sha256': hashlib.sha256(self.data).hexdigest()}}
        self.control.jobs[self.id] = self.job
        self.upstream = MagicMock(status_code=200)
        self.upstream.iter_content.return_value = [self.data]
        for name, value in [('controller', self.control), ('VIDEOS', self.path)]:
            context = patch.object(app, name, value); context.start(); self.addCleanup(context.stop)
        context = patch.object(self.control.drive_storage, 'open_download', return_value=self.upstream)
        self.download = context.start(); self.addCleanup(context.stop)

    def request(self, value='bytes=0-3'):
        return Request({'type': 'http', 'headers': [(b'range', value.encode())]})

    def test_first_play_fetches_complete_file_then_reuses_local_for_ranges(self):
        response = app.video(self.id + '.mp4', self.request())
        self.assertIsInstance(response, FileResponse)
        self.assertEqual(Path(response.path).read_bytes(), self.data)
        self.assertEqual(response.headers['accept-ranges'], 'bytes')
        self.download.assert_called_once_with(self.job['drive'])
        self.assertNotIn('location', response.headers)
        self.upstream.close.assert_called_once()
        app.video(self.id + '.mp4', self.request('bytes=4-7'))
        self.download.assert_called_once()

    def test_download_export_uses_the_same_local_file(self):
        response = app.video(self.id + '.mp4', self.request(), download=True)
        self.assertIn('attachment', response.headers['content-disposition'])
        self.assertEqual(Path(response.path).read_bytes(), self.data)
        app.video(self.id + '.mp4', self.request(), download=True)
        self.download.assert_called_once()

    def test_unsupported_multirange_never_requests_drive(self):
        with self.assertRaises(app.HTTPException) as error:
            app.video(self.id + '.mp4', self.request('bytes=0-1,4-5'))
        self.assertEqual(error.exception.status_code, 416)
        self.download.assert_not_called()

    def test_failed_download_never_returns_partial_video(self):
        self.upstream.status_code = 403
        with self.assertRaises(app.HTTPException): app.video(self.id + '.mp4', self.request())
        self.assertNotIn('file', self.job)
        self.assertFalse((self.path / (self.id + '.mp4')).exists())
        self.assertEqual(list(self.path.glob('*.partial')), [])
        self.upstream.close.assert_called_once()

    def test_corruption_and_missing_file_are_repaired_before_playback(self):
        app.video(self.id + '.mp4', self.request())
        target = self.path / self.job['file']
        target.write_bytes(b'x' * len(self.data))
        response = app.video(self.id + '.mp4', self.request())
        self.assertEqual(Path(response.path).read_bytes(), self.data)
        target.unlink()
        response = app.video(self.id + '.mp4', self.request())
        self.assertEqual(Path(response.path).read_bytes(), self.data)
        self.assertEqual(self.download.call_count, 3)


if __name__ == '__main__': unittest.main()
