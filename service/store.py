"""SQLite transactions shared by the HTTP handlers and the step dispatcher."""

import hashlib
import json
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

LICENSE_VERSION = "noncommercial-1"
GPU = {
    "track",
    "gvhmr",
    "gvhmr_batch",
    "pva",
    "refine",
    "footmr",
    "handflow",
    "exmr_batch",
    "flow_camera",
    "flow_batch",
}
CPU = {"prepare", "correct", "export", "flow_export", "rig", "save_fbx"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
 id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, token_hash TEXT UNIQUE NOT NULL,
 accepted TEXT, enabled INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS uploads (
 id TEXT PRIMARY KEY, owner TEXT NOT NULL REFERENCES users, bytes INTEGER NOT NULL,
 sha TEXT NOT NULL, offset INTEGER NOT NULL DEFAULT 0, info TEXT);
CREATE TABLE IF NOT EXISTS projects (
 id TEXT PRIMARY KEY, owner TEXT NOT NULL REFERENCES users, name TEXT NOT NULL,
 upload TEXT NOT NULL REFERENCES uploads, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, owner TEXT NOT NULL REFERENCES users, project TEXT NOT NULL REFERENCES projects,
 model TEXT NOT NULL, options TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL,
 result TEXT, frames INTEGER, fps REAL, error TEXT, artifact TEXT, bytes INTEGER);
CREATE TABLE IF NOT EXISTS steps (
 job TEXT NOT NULL REFERENCES jobs, seq INTEGER NOT NULL, stage TEXT NOT NULL,
 lane TEXT NOT NULL, args TEXT NOT NULL, state TEXT NOT NULL, started REAL, ended REAL,
 current INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(job,seq));
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY, job TEXT NOT NULL REFERENCES jobs, at REAL NOT NULL, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_job ON events(job,id);
CREATE INDEX IF NOT EXISTS jobs_project ON jobs(project,created);
CREATE INDEX IF NOT EXISTS steps_lane ON steps(lane,state);
CREATE TABLE IF NOT EXISTS gpu_usage (
 job TEXT NOT NULL REFERENCES jobs, seq INTEGER NOT NULL, started REAL NOT NULL, ended REAL NOT NULL,
 PRIMARY KEY(job,seq,started));
CREATE INDEX IF NOT EXISTS usage_window ON gpu_usage(ended);
CREATE TABLE IF NOT EXISTS license_acceptances (
 owner TEXT NOT NULL REFERENCES users, version TEXT NOT NULL, accepted_at REAL NOT NULL,
 PRIMARY KEY(owner,version));
CREATE TABLE IF NOT EXISTS requests (
 owner TEXT NOT NULL, scope TEXT NOT NULL, key TEXT NOT NULL, body TEXT NOT NULL,
 resource TEXT NOT NULL, PRIMARY KEY(owner,scope,key));
"""


class Problem(Exception):
    def __init__(self, status, code):
        super().__init__(code)
        self.status, self.code = status, code


def ident(prefix):
    return prefix + "_" + uuid.uuid4().hex


def encode(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def event(db, job, kind, **fields):
    db.execute(
        "INSERT INTO events(job,at,data) VALUES(?,?,?)", (job, time.time(), encode({"type": kind, **fields}))
    )


class Store:
    def __init__(self, directory):
        self.root = Path(directory).resolve()
        for d in ("uploads", "jobs", "artifacts"):
            (self.root / d).mkdir(parents=True, exist_ok=True)
        self.file = self.root / "service.sqlite"
        with self.db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)

    @contextmanager
    def db(self, write=False):
        db = sqlite3.connect(self.file, isolation_level=None, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def add_user(self, name):
        token = secrets.token_urlsafe(32)
        with self.db(True) as db:
            db.execute(
                "INSERT INTO users(id,name,token_hash) VALUES(?,?,?)", (ident("u"), name, digest(token))
            )
        return token

    @staticmethod
    def owned(db, table, owner, key):
        # Table names come only from the fixed handlers below.
        row = db.execute(f"SELECT * FROM {table} WHERE id=? AND owner=?", (key, owner)).fetchone()
        if row is None:
            raise Problem(404, "not_found")
        return row

    @staticmethod
    def once(db, owner, scope, key, body, make):
        params = encode(body)
        row = db.execute(
            "SELECT * FROM requests WHERE owner=? AND scope=? AND key=?", (owner, scope, key)
        ).fetchone()
        if row:
            if row["body"] != params:
                raise Problem(409, "idempotency_conflict")
            return row["resource"]
        resource = make()
        db.execute("INSERT INTO requests VALUES(?,?,?,?,?)", (owner, scope, key, params, resource))
        return resource

    def video(self, upload):
        return self.root / "uploads" / (upload + ".mp4")

    def create_job(self, db, owner, project, model, options):
        encoded = encode(options)
        same = db.execute(
            """SELECT id FROM jobs WHERE project=? AND model=? AND options=?
                             AND state IN ('queued','running')""",
            (project, model, encoded),
        ).fetchone()
        if same:
            return same["id"]
        self.check_quota(db, owner)
        job = ident("j")
        db.execute(
            "INSERT INTO jobs(id,owner,project,model,options,state,created) VALUES(?,?,?,?,?,'queued',?)",
            (job, owner, project, model, encoded, time.time()),
        )
        db.execute(
            "INSERT INTO steps(job,seq,stage,lane,args,state) VALUES(?,0,'prepare','cpu','[]','pending')",
            (job,),
        )
        event(db, job, "queued")
        return job

    @staticmethod
    def check_quota(db, owner):
        n = db.execute(
            "SELECT count(*) FROM jobs WHERE owner=? AND state IN ('queued','running','cancelling')", (owner,)
        ).fetchone()[0]
        if n >= 20:
            raise Problem(429, "queue_full")

    def recover(self):
        with self.db(True) as db:
            db.execute(
                """INSERT OR IGNORE INTO gpu_usage SELECT job,seq,started,ended FROM steps
                          WHERE lane='gpu' AND started IS NOT NULL AND ended>?""",
                (time.time() - 3600,),
            )
            for j in db.execute(
                "SELECT id,state FROM jobs WHERE state IN ('running','cancelling')"
            ).fetchall():
                cancel = j["state"] == "cancelling"
                # Charge the observed portion of an interrupted step, excluding time offline.
                db.execute(
                    """INSERT OR IGNORE INTO gpu_usage SELECT job,seq,started,
                              MAX(started,(SELECT MAX(at) FROM events WHERE job=?)) FROM steps
                              WHERE job=? AND state='running' AND lane='gpu'""",
                    (j["id"], j["id"]),
                )
                if cancel:
                    self.skip_pending(db, j["id"])
                db.execute(
                    "UPDATE steps SET state=?,started=NULL,current=0,total=0 WHERE job=? AND state='running'",
                    ("cancelled" if cancel else "pending", j["id"]),
                )
                db.execute(
                    "UPDATE jobs SET state=? WHERE id=?", ("cancelled" if cancel else "queued", j["id"])
                )
                event(db, j["id"], "cancelled" if cancel else "recovered")

    @staticmethod
    def skip_pending(db, job):
        db.execute("UPDATE steps SET state='skipped' WHERE job=? AND state='pending'", (job,))

    def claim(self, lane, slots):
        now = time.time()
        with self.db(True) as db:
            if (
                db.execute("SELECT count(*) FROM steps WHERE lane=? AND state='running'", (lane,)).fetchone()[
                    0
                ]
                >= slots
            ):
                return None
            row = db.execute(
                """
              SELECT s.seq,s.stage,s.args,j.*,p.upload FROM steps s JOIN jobs j ON j.id=s.job
              JOIN projects p ON p.id=j.project
              WHERE s.state='pending' AND s.lane=? AND j.state IN ('queued','running')
              AND NOT EXISTS(SELECT 1 FROM steps prev WHERE prev.job=j.id AND prev.seq<s.seq
                             AND prev.state NOT IN ('done','cached'))
              AND NOT EXISTS(SELECT 1 FROM jobs held WHERE held.project=j.project AND held.id!=j.id
                             AND held.state IN ('running','cancelling'))
              AND (j.state='running' OR NOT EXISTS(SELECT 1 FROM jobs older WHERE older.project=j.project
                             AND older.state='queued' AND older.rowid<j.rowid))
              AND (?='gpu' OR NOT EXISTS(SELECT 1 FROM steps busy JOIN jobs bj ON bj.id=busy.job
                  WHERE busy.state='running' AND busy.lane='cpu' AND bj.owner=j.owner))
              ORDER BY COALESCE((SELECT SUM(h.ended-MAX(h.started,?)) FROM gpu_usage h JOIN jobs hj ON hj.id=h.job
                  WHERE h.ended>? AND hj.owner=j.owner),0),j.rowid LIMIT 1
              """,
                (lane, lane, now - 3600, now - 3600),
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE steps SET state='running',started=? WHERE job=? AND seq=?",
                (now, row["id"], row["seq"]),
            )
            db.execute("UPDATE jobs SET state='running' WHERE id=?", (row["id"],))
            event(db, row["id"], "step_started", stage=row["stage"])
            return dict(row)

    def finish(self, step, outcome, error=None, artifact=None):
        job, seq = step["id"], step["seq"]
        with self.db(True) as db:
            cancelled = db.execute("SELECT state FROM jobs WHERE id=?", (job,)).fetchone()[0] == "cancelling"
            state = (
                "cancelled"
                if cancelled
                else "failed"
                if error
                else "cached"
                if outcome.get("cached")
                else "done"
            )
            db.execute(
                "UPDATE steps SET state=?,ended=? WHERE job=? AND seq=?", (state, time.time(), job, seq)
            )
            db.execute(
                """INSERT INTO gpu_usage SELECT job,seq,started,ended FROM steps
                          WHERE job=? AND seq=? AND lane='gpu'""",
                (job, seq),
            )
            event(db, job, "step_finished", stage=step["stage"], state=state)
            if cancelled or error:
                self.skip_pending(db, job)
                db.execute(
                    "UPDATE jobs SET state=?,error=? WHERE id=?", (state, None if cancelled else error, job)
                )
                event(db, job, state)
            elif step["stage"] == "prepare":
                for i, (name, args) in enumerate(outcome["plan"], 1):
                    if name not in GPU | CPU:
                        raise ValueError("Unsupported worker stage: " + name)
                    db.execute(
                        "INSERT INTO steps(job,seq,stage,lane,args,state) VALUES(?,?,?,?,?,'pending')",
                        (job, i, name, "gpu" if name in GPU else "cpu", encode(args)),
                    )
                db.execute(
                    "UPDATE jobs SET result=?,frames=?,fps=? WHERE id=?",
                    (outcome["result"], outcome["frames"], outcome["fps"], job),
                )
            elif artifact:
                db.execute(
                    "UPDATE jobs SET state='succeeded',artifact=?,bytes=? WHERE id=?", (*artifact, job)
                )
                event(db, job, "succeeded")
