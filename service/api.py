import asyncio
import hashlib
import json
import math
import os
import subprocess
import sys
import time
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import PIN
from .dispatcher import Dispatcher
from .store import LICENSE_VERSION, Problem, Store, digest, encode, event, ident

PREFIX = "/api/v1"
CHUNK = 8 * 1024**2
Model = Literal["gvhmr", "exmr", "flowhmr"]
Key = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Upload(Body):
    bytes: int = Field(gt=0, le=10 * 1024**3)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class Project(Body):
    upload: str
    name: str = Field(min_length=1, max_length=120)


class Acceptance(Body):
    version: str


class Options(Body):
    start: float = Field(default=0, ge=0)
    end: float | None = Field(default=None, gt=0)
    compute_mode: Literal["auto", "low_memory"] = "auto"
    refine: bool = True
    footmr: bool = True
    handflow: bool = True
    correction: bool = True
    static_camera: bool = True
    smooth: bool = True

    @model_validator(mode="after")
    def valid_trim(self):
        if self.end is not None and self.end <= self.start:
            raise ValueError("Invalid trim range")
        return self


def inspect_upload(root, path, expected):
    with path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
            raise Problem(422, "hash_mismatch")
    command = [
        root / "tools/bin/ffprobe.exe",
        "-v",
        "error",
        "-protocol_whitelist",
        "file",
        "-format_whitelist",
        "mov",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,codec_name:format=duration",
        "-of",
        "json",
        path,
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, check=True, timeout=45, creationflags=subprocess.CREATE_NO_WINDOW
        )
        data = json.loads(result.stdout)
        stream = data["streams"][0]
        duration = float(data["format"]["duration"])
        if not math.isfinite(duration) or duration <= 0 or min(stream["width"], stream["height"]) < 1:
            raise ValueError()
        return dict(
            duration=duration, width=stream["width"], height=stream["height"], codec=stream["codec_name"]
        )
    except (subprocess.SubprocessError, ValueError, KeyError, IndexError):
        raise Problem(422, "invalid_mp4") from None


def create_app(root: Path, data: Path):
    root = root.resolve(strict=True)
    data = data.resolve()
    if root.is_relative_to(data) or data.is_relative_to(root):
        raise ValueError("Keep the service data directory separate from the upstream installation.")
    store = Store(data)
    dispatcher = Dispatcher(store, root)

    @asynccontextmanager
    async def lifespan(app):
        sys.path[:0] = [str(root), str(Path(__file__).resolve().parents[1] / "poc")]
        from poc_headless import verify_install
        from tools.session_lock import app_lock

        with ExitStack() as locks:
            locks.enter_context(app_lock(root))
            locks.enter_context(app_lock(data))
            verify_install(root)
            revision = subprocess.check_output(
                ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
            ).strip()
            if revision != PIN:
                raise RuntimeError("The upstream revision does not match the adapter.")
            dispatcher.start()
            try:
                yield
            finally:
                dispatcher.close()

    app = FastAPI(title="RGBMoCap Remote", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(Problem)
    async def problem(request, exc):
        return JSONResponse({"error": {"code": exc.code}}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def invalid(request, exc):
        return JSONResponse({"error": {"code": "invalid_request"}}, status_code=422)

    def account(authorization: Annotated[str | None, Header()] = None):
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token or len(token) > 256:
            raise Problem(401, "invalid_token")
        with store.db() as db:
            row = db.execute(
                "SELECT id,name,accepted FROM users WHERE token_hash=? AND enabled=1", (digest(token),)
            ).fetchone()
        if row is None:
            raise Problem(401, "invalid_token")
        return dict(row)

    def licensed(user=Depends(account)):
        if user["accepted"] != LICENSE_VERSION:
            raise Problem(403, "license_required")
        return user["id"]

    @app.get(PREFIX + "/me")
    def me(user=Depends(account)):
        return dict(
            name=user["name"],
            api_version=1,
            upstream=PIN,
            license_version=LICENSE_VERSION,
            accepted=user["accepted"] == LICENSE_VERSION,
        )

    @app.post(PREFIX + "/me/license")
    def license_agreement(body: Acceptance, user=Depends(account)):
        if body.version != LICENSE_VERSION:
            raise Problem(409, "license_version_changed")
        with store.db(True) as db:
            db.execute("UPDATE users SET accepted=? WHERE id=?", (LICENSE_VERSION, user["id"]))
            db.execute(
                "INSERT OR IGNORE INTO license_acceptances VALUES(?,?,?)",
                (user["id"], LICENSE_VERSION, time.time()),
            )
        return {"accepted": True}

    @app.post(PREFIX + "/uploads", status_code=201)
    def create_upload(body: Upload, key: Key, owner=Depends(licensed)):
        with store.db(True) as db:

            def make():
                uid = ident("up")
                db.execute(
                    "INSERT INTO uploads(id,owner,bytes,sha) VALUES(?,?,?,?)",
                    (uid, owner, body.bytes, body.sha256),
                )
                return uid

            uid = store.once(db, owner, "upload", key, body.model_dump(), make)
            row = store.owned(db, "uploads", owner, uid)
            return dict(id=uid, offset=row["offset"], complete=row["info"] is not None, chunk_bytes=CHUNK)

    @app.put(PREFIX + "/uploads/{uid}")
    async def append(
        uid: str, offset: Annotated[int, Query(ge=0)], request: Request, owner=Depends(licensed)
    ):
        with store.db() as db:
            store.owned(db, "uploads", owner, uid)
        buf = bytearray()
        async for part in request.stream():
            if len(buf) + len(part) > CHUNK:
                raise Problem(413, "chunk_too_large")
            buf.extend(part)
        if not buf:
            raise Problem(422, "empty_chunk")

        def write():
            with store.db(True) as db:
                row = store.owned(db, "uploads", owner, uid)
                if offset != row["offset"] or row["info"] is not None:
                    raise Problem(409, "offset_conflict")
                end = offset + len(buf)
                if end > row["bytes"]:
                    raise Problem(413, "upload_too_large")
                path = store.video(uid)
                with path.open("r+b" if path.exists() else "xb") as file:
                    file.seek(offset)
                    file.write(buf)
                    file.truncate()
                    file.flush()
                    os.fsync(file.fileno())
                db.execute("UPDATE uploads SET offset=? WHERE id=?", (end, uid))
            return {"offset": end}

        return await asyncio.to_thread(write)

    @app.post(PREFIX + "/uploads/{uid}/complete")
    def complete(uid: str, owner=Depends(licensed)):
        with store.db() as db:
            row = store.owned(db, "uploads", owner, uid)
        if row["info"]:
            return json.loads(row["info"])
        if row["offset"] != row["bytes"]:
            raise Problem(409, "incomplete_upload")
        try:
            info = inspect_upload(root, store.video(uid), row["sha"])
        except Problem as exc:
            if exc.code == "hash_mismatch":
                with store.db(True) as db:
                    db.execute(
                        "DELETE FROM requests WHERE owner=? AND scope='upload' AND resource=?", (owner, uid)
                    )
            raise
        with store.db(True) as db:
            db.execute("UPDATE uploads SET info=? WHERE id=?", (encode(info), uid))
        return info

    @app.post(PREFIX + "/projects", status_code=201)
    def create_project(body: Project, key: Key, owner=Depends(licensed)):
        with store.db(True) as db:
            if not store.owned(db, "uploads", owner, body.upload)["info"]:
                raise Problem(409, "incomplete_upload")

            def make():
                pid = ident("p")
                db.execute(
                    "INSERT INTO projects VALUES(?,?,?,?,?)",
                    (pid, owner, body.name, body.upload, time.time()),
                )
                return pid

            return {"id": store.once(db, owner, "project", key, body.model_dump(), make)}

    @app.get(PREFIX + "/projects")
    def projects(owner=Depends(licensed)):
        with store.db() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT id,name,created FROM projects WHERE owner=? ORDER BY created DESC", (owner,)
                )
            ]

    @app.get(PREFIX + "/projects/{pid}")
    def project(pid: str, owner=Depends(licensed)):
        with store.db() as db:
            row = store.owned(db, "projects", owner, pid)
            return {k: row[k] for k in ("id", "name", "upload", "created")}

    @app.post(PREFIX + "/projects/{pid}/models/{model}/reconstruct", status_code=202)
    def reconstruct(pid: str, model: Model, body: Options, key: Key, owner=Depends(licensed)):
        options = body.model_dump()
        with store.db(True) as db:
            p = store.owned(db, "projects", owner, pid)
            duration = json.loads(store.owned(db, "uploads", owner, p["upload"])["info"])["duration"]
            end = body.end if body.end is not None else duration
            if end > duration + 0.05 or body.start >= duration:
                raise Problem(422, "invalid_trim")
            if model == "flowhmr" and math.ceil((end - body.start) * 30) > 5000:
                raise Problem(422, "flowhmr_frame_limit")
            job = store.once(
                db,
                owner,
                f"reconstruct/{pid}/{model}",
                key,
                options,
                lambda: store.create_job(db, owner, pid, model, options),
            )
            return {"id": job}

    def public_job(db, row):
        keys = (
            "id",
            "project",
            "model",
            "state",
            "created",
            "result",
            "frames",
            "fps",
            "error",
            "artifact",
            "bytes",
        )
        data = {key: row[key] for key in keys}
        data["project_name"] = db.execute(
            "SELECT name FROM projects WHERE id=?", (row["project"],)
        ).fetchone()[0]
        data["options"] = json.loads(row["options"])
        data["steps"] = [
            dict(r)
            for r in db.execute(
                """SELECT stage,lane,state,current,total,started,ended
                           FROM steps WHERE job=? ORDER BY seq""",
                (row["id"],),
            )
        ]
        return data

    @app.get(PREFIX + "/jobs")
    def jobs(owner=Depends(licensed), limit: int = Query(50, ge=1, le=100), before: str | None = None):
        with store.db() as db:
            cursor = 2**63 - 1
            if before is not None:
                anchor = db.execute(
                    "SELECT rowid FROM jobs WHERE owner=? AND id=?", (owner, before)
                ).fetchone()
                if anchor is None:
                    raise Problem(404, "not_found")
                cursor = anchor[0]
            return [
                public_job(db, r)
                for r in db.execute(
                    "SELECT * FROM jobs WHERE owner=? AND rowid<? ORDER BY rowid DESC LIMIT ?",
                    (owner, cursor, limit),
                )
            ]

    @app.get(PREFIX + "/jobs/{job}")
    def job_status(job: str, owner=Depends(licensed)):
        with store.db() as db:
            return public_job(db, store.owned(db, "jobs", owner, job))

    @app.get(PREFIX + "/jobs/{job}/events")
    async def events(
        job: str, after: int = Query(0, ge=0), wait: float = Query(0, ge=0, le=20), owner=Depends(licensed)
    ):
        until = time.monotonic() + wait
        while True:
            with store.db() as db:
                row = store.owned(db, "jobs", owner, job)
                changes = [
                    dict(id=e["id"], at=e["at"], **json.loads(e["data"]))
                    for e in db.execute(
                        "SELECT * FROM events WHERE job=? AND id>? ORDER BY id LIMIT 256", (job, after)
                    )
                ]
            if changes or time.monotonic() >= until or row["state"] in ("succeeded", "failed", "cancelled"):
                return changes
            await asyncio.sleep(0.2)

    @app.post(PREFIX + "/jobs/{job}/cancel")
    def cancel(job: str, owner=Depends(licensed)):
        with store.db(True) as db:
            row = store.owned(db, "jobs", owner, job)
            if row["state"] not in ("queued", "running", "cancelling"):
                return {"state": row["state"]}
            running = db.execute("SELECT 1 FROM steps WHERE job=? AND state='running'", (job,)).fetchone()
            state = "cancelling" if running else "cancelled"
            if not running:
                store.skip_pending(db, job)
            db.execute("UPDATE jobs SET state=? WHERE id=?", (state, job))
            event(db, job, state)
        if running:
            dispatcher.cancel(job)
        return {"state": state}

    @app.post(PREFIX + "/jobs/{job}/retry", status_code=202)
    def retry(job: str, owner=Depends(licensed)):
        with store.db(True) as db:
            row = store.owned(db, "jobs", owner, job)
            if row["state"] not in ("failed", "cancelled"):
                raise Problem(409, "job_not_retryable")
            store.check_quota(db, owner)
            db.execute(
                "UPDATE steps SET state='pending',current=0,total=0,started=NULL,ended=NULL WHERE job=? AND state IN ('failed','cancelled','skipped')",
                (job,),
            )
            db.execute("UPDATE jobs SET state='queued',error=NULL WHERE id=?", (job,))
            event(db, job, "queued")
        return {"id": job}

    @app.get(PREFIX + "/artifacts/{sha}")
    def artifact(sha: str, owner=Depends(licensed)):
        with store.db() as db:
            allowed = db.execute(
                "SELECT 1 FROM jobs WHERE owner=? AND state='succeeded' AND artifact=?", (owner, sha)
            ).fetchone()
        if not allowed:
            raise Problem(404, "not_found")
        return FileResponse(
            store.root / "artifacts" / sha, media_type="application/octet-stream", filename="motion.fbx"
        )

    return app
