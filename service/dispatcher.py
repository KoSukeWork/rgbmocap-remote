"""Dispatch one GPU step and up to two CPU steps, with OS-owned process trees."""

import hashlib
import json
import logging
import os
import shutil
import subprocess
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import win32api
import win32con
import win32job

from .store import event

LOG = logging.getLogger("uvicorn.error")


class Dispatcher:
    def __init__(self, store, root):
        self.store, self.root = store, root
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.handles = {}
        self.thread = threading.Thread(target=self.serve, name="step-scheduler")

    def start(self):
        self.store.recover()
        self.thread.start()

    def close(self):
        self.stop.set()
        with self.lock:
            for handle in self.handles.values():
                win32job.TerminateJobObject(handle, 1)
        self.thread.join()

    def cancel(self, job):
        with self.lock:
            if job in self.handles:
                with self.store.db() as db:
                    state = db.execute("SELECT state FROM jobs WHERE id=?", (job,)).fetchone()[0]
                if state == "cancelling":
                    win32job.TerminateJobObject(self.handles[job], 1)

    def serve(self):
        futures = {}
        with ThreadPoolExecutor(max_workers=3) as pool:
            while not self.stop.is_set():
                for future, step in list(futures.items()):
                    if not future.done():
                        continue
                    try:
                        outcome, error, artifact = future.result()
                        self.store.finish(step, outcome, error, artifact)
                    except Exception:
                        LOG.exception("Step failed for %s", step["id"])
                        self.store.finish(step, {}, "execution_error")
                    del futures[future]
                for lane, slots in (("cpu", 2), ("gpu", 1)):
                    while len(futures) < 3 and not self.stop.is_set():
                        step = self.store.claim(lane, slots)
                        if step is None:
                            break
                        futures[pool.submit(self.run, step)] = step
                self.stop.wait(0.15)

    def persist_fbx(self, path):
        with path.open("rb") as source:
            sha = hashlib.file_digest(source, "sha256").hexdigest()
        final = self.store.root / "artifacts" / sha
        if not final.exists():
            # The job may retry; the shared artifact must be independent of its working file.
            tmp = final.with_suffix("." + uuid.uuid4().hex + ".tmp")
            try:
                shutil.copyfile(path, tmp)
                os.replace(tmp, final)
            finally:
                tmp.unlink(missing_ok=True)
        return sha, final.stat().st_size

    def run(self, step):
        job = step["id"]
        out = self.store.root / "jobs" / job
        out.mkdir(exist_ok=True)
        request = dict(
            root=str(self.root),
            out=str(out),
            video=str(self.store.video(step["upload"])),
            model=step["model"],
            options=json.loads(step["options"]),
            result=step["result"],
            stage=step["stage"],
            arguments=json.loads(step["args"]),
        )
        handle = win32job.CreateJobObject(None, "RGBMoCapRemote-" + uuid.uuid4().hex)
        info = win32job.QueryInformationJobObject(handle, win32job.JobObjectExtendedLimitInformation)
        info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(handle, win32job.JobObjectExtendedLimitInformation, info)
        proc = None
        try:
            with (out / "service.log").open("ab") as log:
                command = [
                    self.root / "runtime/python.exe",
                    "-I",
                    "-B",
                    "-X",
                    "utf8",
                    "-u",
                    Path(__file__).with_name("stage_worker.py"),
                ]
                proc = subprocess.Popen(
                    command,
                    cwd=self.root,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=log,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                access = win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE
                process = win32api.OpenProcess(access, False, proc.pid)
                try:
                    win32job.AssignProcessToJobObject(handle, process)
                finally:
                    process.Close()
                with self.lock:
                    self.handles[job] = handle
                    with self.store.db() as db:
                        cancelled = (
                            db.execute("SELECT state FROM jobs WHERE id=?", (job,)).fetchone()[0]
                            == "cancelling"
                        )
                    if self.stop.is_set() or cancelled:
                        return {}, "interrupted", None
                    proc.stdin.write(json.dumps(request).encode() + b"\n")
                    proc.stdin.close()
                outcome, error = None, None
                for raw in proc.stdout:
                    message = json.loads(raw)
                    if message["event"] == "done":
                        outcome = message
                    elif message["event"] == "failed":
                        error = message["code"]
                    elif message["event"] == "progress":
                        current, total = int(message.get("current") or 0), int(message.get("total") or 0)
                        with self.store.db(True) as db:
                            db.execute(
                                "UPDATE steps SET current=?,total=? WHERE job=? AND seq=?",
                                (current, total, job, step["seq"]),
                            )
                            event(db, job, "progress", stage=step["stage"], current=current, total=total)
                if proc.wait() or outcome is None or error:
                    return {}, error or "worker_failed", None
            artifact = self.persist_fbx(out / "motion.fbx") if step["stage"] == "save_fbx" else None
            return outcome, None, artifact
        finally:
            with self.lock:
                self.handles.pop(job, None)
                handle.Close()
            if proc is not None:
                if proc.poll() is None:
                    proc.kill()
                proc.wait()
                proc.stdin.close()
                proc.stdout.close()
