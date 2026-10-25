"""Single-process durable job queue. SQLite transactions bound admission and deduplication.

One worker, at most 16 queued/running jobs. Interrupted running jobs return to queued
after restart. This is deliberately not advertised as a distributed scheduler.
"""
import hashlib,json,sqlite3,threading,time
from contextlib import contextmanager

class QueueFull(Exception):pass

class JobStore:
    def __init__(self,path,compute,max_pending=16):
        self.path=str(path);self.compute=compute;self.max_pending=max_pending;self.stop=threading.Event()
        with self.connect() as c:
            c.execute('PRAGMA journal_mode=WAL')
            c.execute('CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,payload TEXT NOT NULL,state TEXT NOT NULL,result TEXT,error TEXT,created REAL,updated REAL)')
            c.execute("UPDATE jobs SET state='queued',updated=? WHERE state='running'",(time.time(),))
        self.worker=None
    @contextmanager
    def connect(self):
        c=sqlite3.connect(self.path,timeout=10);c.row_factory=sqlite3.Row
        try:
            yield c
            c.commit()
        except BaseException:
            c.rollback();raise
        finally:c.close()
    def submit(self,payload):
        canonical=json.dumps(payload,sort_keys=True,separators=(',',':'));key=hashlib.sha256(canonical.encode()).hexdigest()
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT * FROM jobs WHERE id=?',(key,)).fetchone()
            if row:return self.decode(row),True
            pending=c.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0]
            if pending>=self.max_pending:raise QueueFull()
            now=time.time();c.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?)',(key,canonical,'queued',None,None,now,now))
        return self.get(key),False
    def decode(self,row):
        if row is None:return None
        d=dict(row)
        for k in ('payload','result'):
            if d[k] is not None:d[k]=json.loads(d[k])
        return d
    def get(self,key):
        with self.connect() as c:return self.decode(c.execute('SELECT * FROM jobs WHERE id=?',(key,)).fetchone())
    def work_once(self):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY created LIMIT 1").fetchone()
            if not row:return False
            c.execute("UPDATE jobs SET state='running',updated=? WHERE id=?",(time.time(),row['id']))
        try:
            result=self.compute(json.loads(row['payload']))
            state='succeeded';result=json.dumps(result,allow_nan=False);error=None
        except Exception as exc:
            state='failed';result=None;error=f'{type(exc).__name__}: {str(exc)[:300]}'
        with self.connect() as c:c.execute('UPDATE jobs SET state=?,result=?,error=?,updated=? WHERE id=?',(state,result,error,time.time(),row['id']))
        return True
    def start(self):
        def loop():
            while not self.stop.is_set():
                if not self.work_once():self.stop.wait(.1)
        self.worker=threading.Thread(target=loop,daemon=True,name='raster-statistics');self.worker.start()
    def close(self):
        self.stop.set()
        if self.worker:self.worker.join(timeout=5)
