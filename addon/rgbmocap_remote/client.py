"""Blender-independent client for the versioned RGBMoCap API."""

import hashlib
import http.client
import ipaddress
import json
import os
import re
import socket
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit


class RemoteError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code
        super().__init__(f"HTTP {status} ({code})")


@contextmanager
def cache_lock(path):
    # OS locks survive neither a process crash nor a machine restart.
    with path.open("a+b") as lock:
        if os.name == "nt":
            import msvcrt

            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise ValueError("Another Blender is downloading this motion.") from exc
        else:
            import fcntl

            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("Another Blender is downloading this motion.") from exc
        yield


NETWORK_ERRORS = (
    TimeoutError,
    ConnectionError,
    http.client.IncompleteRead,
    http.client.RemoteDisconnected,
    socket.gaierror,
)


def file_hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class Client:
    def __init__(self, address, token):
        url = urlsplit(address.strip())
        if (
            not url.hostname
            or url.scheme not in {"http", "https"}
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
        ):
            raise ValueError("Enter the server origin, including http:// or https://.")
        if url.scheme == "http" and url.hostname != "localhost":
            try:
                local = ipaddress.ip_address(url.hostname).is_loopback
            except ValueError:
                local = False
            if not local:
                raise ValueError("LAN connections require HTTPS.")
        self.connection = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
        self.host, self.port = url.hostname, url.port
        self.token = token.strip()
        if not self.token:
            raise ValueError("Enter your personal token.")

    @contextmanager
    def open(self, method, path, *, data=None, headers=None):
        conn = self.connection(self.host, self.port, timeout=90)
        try:
            conn.request(
                method,
                "/api/v1/" + path,
                body=data,
                headers={"Authorization": "Bearer " + self.token, **(headers or {})},
            )
            response = conn.getresponse()
            if not 200 <= response.status < 300:
                try:
                    code = json.loads(response.read(8192))["error"]["code"]
                except (ValueError, KeyError, TypeError):
                    code = response.reason
                raise RemoteError(response.status, code)
            yield response
        finally:
            conn.close()

    def json(self, method, path, body=None, key=None):
        headers = {"Content-Type": "application/json"}
        if key is not None:
            headers["Idempotency-Key"] = key
        data = None if body is None else json.dumps(body).encode("utf8")
        # Only repeat requests whose effects can be recovered with the same identity.
        for attempt in range(2):
            try:
                with self.open(method, path, data=data, headers=headers) as response:
                    return json.load(response)
            except NETWORK_ERRORS:
                if attempt or (method != "GET" and not key):
                    raise

    def submit(self, video, model, options, notify=lambda message: None, submission=None):
        video = Path(video)
        if video.suffix.lower() not in {".mp4", ".mov", ".m4v"} or not video.is_file():
            raise ValueError("Choose an MP4 or MOV file.")
        if model not in {"exmr", "gvhmr", "flowhmr"}:
            raise ValueError("Unsupported model.")
        size = video.stat().st_size
        if not 0 < size <= 10 * 1024**3:
            raise ValueError("Video size must be between one byte and 10 GiB.")
        notify("Checking video")
        sha = file_hash(video)
        body = {"bytes": size, "sha256": sha}

        def resume():
            return self.json("POST", "uploads", body, key=sha)

        upload = resume()
        with video.open("rb") as stream:
            offset, retries = upload["offset"], 0
            while offset < size:
                notify(f"Upload {offset * 100 // size}%")
                stream.seek(offset)
                chunk = stream.read(min(8 * 1024**2, upload["chunk_bytes"], size - offset))
                if not chunk:
                    raise ValueError("Video changed during upload.")
                try:
                    with self.open("PUT", f"uploads/{upload['id']}?offset={offset}", data=chunk) as response:
                        new_offset = json.load(response)["offset"]
                    if new_offset != offset + len(chunk):
                        raise ValueError("Unexpected upload offset.")
                    offset, retries = new_offset, 0
                except (RemoteError, *NETWORK_ERRORS) as exc:
                    if retries >= 2 or (isinstance(exc, RemoteError) and exc.code != "offset_conflict"):
                        raise
                    offset = resume()["offset"]
                    retries += 1
        if not upload["complete"]:
            self.json("POST", f"uploads/{upload['id']}/complete")
        submission = submission or uuid.uuid4().hex
        project = self.json(
            "POST", "projects", {"name": video.stem[:120], "upload": upload["id"]}, key=submission
        )
        return self.json(
            "POST", f"projects/{project['id']}/models/{model}/reconstruct", options, key=submission
        )["id"]

    def download(self, job, directory, notify=lambda message: None):
        sha, size = job["artifact"], job["bytes"]
        if job["state"] != "succeeded" or not isinstance(sha, str) or not re.fullmatch("[0-9a-f]{64}", sha):
            raise ValueError("Motion is not ready.")
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / (sha + ".fbx")
        if target.is_file() and target.stat().st_size == size and file_hash(target) == sha:
            return target
        with cache_lock(directory / (sha + ".lock")):
            return self._download_file(sha, size, target, notify)

    def _download_file(self, sha, size, target, notify):
        partial = target.with_suffix(".part")
        offset = partial.stat().st_size if partial.is_file() else 0
        if offset >= size:
            offset = 0
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        with self.open("GET", "artifacts/" + sha, headers=headers) as response:
            if response.status == 200:
                offset = 0
            elif (
                response.status != 206
                or response.getheader("Content-Range") != f"bytes {offset}-{size - 1}/{size}"
            ):
                raise ValueError("Unexpected download range.")
            with partial.open("ab" if offset else "wb") as stream:
                while chunk := response.read(1024**2):
                    offset += len(chunk)
                    if offset > size:
                        raise ValueError("Unexpected download size.")
                    stream.write(chunk)
                    notify(f"Download {offset * 100 // size}%")
        if offset != size:
            raise ValueError("Incomplete download. Download the motion again to resume.")
        if file_hash(partial) != sha:
            partial.unlink()
            raise ValueError("Motion checksum mismatch. Download the motion again.")
        partial.replace(target)
        return target
