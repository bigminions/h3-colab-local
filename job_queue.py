"""Small, single-consumer queue and SQLite persistence for the local studio."""
import json
import sqlite3
import threading
from contextlib import contextmanager


def application_id(job):
    return job.get('config', {}).get('app_id', 'h3')


def waiting_jobs(jobs, session=None):
    """Front insertions override affinity; ties preserve the explicit queue order."""
    session = session or {}
    def order(job):
        front = job.get('queue_front', False)
        affinity = (application_id(job) == session.get('app_id') and
                    job.get('config', {}).get('gpu') == session.get('accelerator'))
        position = job.get('queue_order', job.get('created', 0))
        return (0 if front else 1, 0 if front or affinity else 1, position)
    return sorted((job for job in jobs.values() if job.get('status') == 'queued'
                   and not job.get('cancel_requested')), key=order)


def queue_plan(jobs, session=None):
    """Project the full sequence, including affinity after each environment switch."""
    remaining = {j['id']: j for j in waiting_jobs(jobs)}
    environment = session or {}
    plan = []
    while remaining:
        job = waiting_jobs(remaining, environment)[0]
        affinity = (application_id(job) == environment.get('app_id') and
                    job.get('config', {}).get('gpu') == environment.get('accelerator'))
        plan.append({'id': job['id'], 'app_id': application_id(job), 'position': len(plan) + 1,
                     'reason': 'front' if job.get('queue_front') else 'affinity' if affinity else 'fifo'})
        environment = {'app_id': application_id(job), 'accelerator': job.get('config', {}).get('gpu')}
        del remaining[job['id']]
    return plan


class JobStore:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.saved = {}
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, app_id TEXT NOT NULL, status TEXT NOT NULL,
                    queue_order REAL NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS jobs_waiting ON jobs(status, queue_order);
                CREATE TABLE IF NOT EXISTS job_events (
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    position INTEGER NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(job_id, position)
                );
                CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, payload TEXT NOT NULL);
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute('PRAGMA foreign_keys=ON')
            with db:
                yield db
        finally:
            db.close()

    def load_jobs(self):
        with self.lock, self.connect() as db:
            jobs = {key: json.loads(payload) for key, payload in db.execute('SELECT id,payload FROM jobs')}
            for key, payload in db.execute('SELECT job_id,payload FROM job_events ORDER BY job_id,position'):
                jobs[key].setdefault('events', []).append(json.loads(payload))
            for job in jobs.values():
                job.setdefault('events', [])
            return jobs

    def save_jobs(self, jobs):
        with self.lock, self.connect() as db:
            saved = {}
            for key, job in jobs.items():
                payload = json.dumps({k: v for k, v in job.items() if k != 'events'}, ensure_ascii=False)
                events = json.dumps(job.get('events', []), ensure_ascii=False)
                saved[key] = (payload, events)
                previous = self.saved.get(key)
                if previous == saved[key]:
                    continue
                db.execute('''INSERT INTO jobs VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                    app_id=excluded.app_id,status=excluded.status,queue_order=excluded.queue_order,payload=excluded.payload''',
                    (key, application_id(job), job.get('status', 'done'),
                     job.get('queue_order', job.get('created', 0)), payload))
                if not previous or previous[1] != events:
                    db.execute('DELETE FROM job_events WHERE job_id=?', (key,))
                    db.executemany('INSERT INTO job_events VALUES(?,?,?)',
                                   [(key, i, json.dumps(event, ensure_ascii=False))
                                    for i, event in enumerate(job.get('events', []))])
            for key in set(self.saved) - jobs.keys():
                db.execute('DELETE FROM jobs WHERE id=?', (key,))
            db.commit()
            self.saved = saved

    def get_state(self, key, default=None):
        with self.lock, self.connect() as db:
            row = db.execute('SELECT payload FROM state WHERE key=?', (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set_state(self, key, value):
        with self.lock, self.connect() as db:
            db.execute('INSERT INTO state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload',
                       (key, json.dumps(value, ensure_ascii=False)))
