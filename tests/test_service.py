"""Regression tests for ownership, resumable transfers and stage scheduling."""

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from service.api import create_app
from service.store import LICENSE_VERSION, Store


class ServiceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.upstream = self.root / "upstream"
        self.upstream.mkdir()
        self.store = Store(self.root / "data")
        # Exercise HTTP handlers with a dormant dispatcher. Real workers need an installed upstream.
        self.client = TestClient(
            create_app(self.upstream, self.store.root), base_url="http://testserver/api/v1/"
        )
        self.addCleanup(self.client.close)
        self.a, self.b = (self.account(n) for n in ("a", "b"))

    def account(self, name):
        token = self.store.add_user(name)
        headers = {"Authorization": "Bearer " + token, "Idempotency-Key": "test"}
        self.assertEqual(self.client.get("jobs", headers=headers).status_code, 403)
        me = self.client.get("me", headers=headers).json()
        self.assertFalse(me["accepted"])
        self.assertEqual(
            self.client.post(
                "me/license", headers=headers, json={"version": me["license_version"]}
            ).status_code,
            200,
        )
        return headers

    def upload(self, headers, content=b"video", key="upload"):
        desc = {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
        headers = {**headers, "Idempotency-Key": key}
        r = self.client.post("uploads", headers=headers, json=desc)
        self.assertEqual(r.status_code, 201)
        return r.json()["id"], desc

    def project(self, headers, name):
        uid, _ = self.upload(headers, key=name)
        r = self.client.put(f"uploads/{uid}?offset=0", headers=headers, content=b"video")
        self.assertEqual(r.status_code, 200)
        with patch("service.api.inspect_upload", return_value={"duration": 10}):
            self.assertEqual(self.client.post(f"uploads/{uid}/complete", headers=headers).status_code, 200)
        r = self.client.post(
            "projects", headers={**headers, "Idempotency-Key": name}, json={"upload": uid, "name": name}
        )
        self.assertEqual(r.status_code, 201)
        return r.json()["id"]

    def job(self, headers, project, n=1):
        r = self.client.post(
            f"projects/{project}/models/exmr/reconstruct",
            headers={**headers, "Idempotency-Key": str(n)},
            json={"end": n},
        )
        self.assertEqual(r.status_code, 202)
        return r.json()["id"]

    def test_tokens_and_acceptance(self):
        with self.store.db() as db:
            stored = db.execute("SELECT token_hash FROM users WHERE name='a'").fetchone()[0]
            token = self.a["Authorization"].split()[1]
            self.assertEqual(stored, hashlib.sha256(token.encode()).hexdigest())
            self.assertGreater(
                db.execute(
                    "SELECT accepted_at FROM license_acceptances WHERE version=?", (LICENSE_VERSION,)
                ).fetchone()[0],
                0,
            )
        self.assertEqual(self.client.get("me").status_code, 401)
        with self.store.db(True) as db:
            db.execute("UPDATE users SET enabled=0 WHERE name='a'")
        self.assertEqual(self.client.get("me", headers=self.a).status_code, 401)

    def test_transfer_resume_and_hash_mismatch(self):
        up, desc = self.upload(self.a, b"abcd")
        h = {**self.a, "Idempotency-Key": "upload"}
        path = "uploads/" + up
        self.assertEqual(self.client.put(path + "?offset=0", headers=self.b, content=b"ab").status_code, 404)
        self.assertEqual(self.client.put(path + "?offset=1", headers=h, content=b"ab").status_code, 409)
        self.assertEqual(self.client.put(path + "?offset=0", headers=h, content=b"ab").json()["offset"], 2)
        self.assertEqual(self.client.post("uploads", headers=h, json=desc).json()["offset"], 2)
        self.assertEqual(self.client.post(path + "/complete", headers=h).status_code, 409)
        self.assertEqual(self.client.put(path + "?offset=2", headers=h, content=b"X" * 3).status_code, 413)
        self.assertEqual(self.client.put(path + "?offset=2", headers=h, content=b"XY").status_code, 200)
        self.assertEqual(
            self.client.post(path + "/complete", headers=h).json()["error"]["code"], "hash_mismatch"
        )
        fresh = self.client.post("uploads", headers=h, json=desc).json()
        self.assertNotEqual(fresh["id"], up)
        self.assertEqual(fresh["offset"], 0)

    def test_project_lock_cancel_retry_and_restart(self):
        p = self.project(self.a, "clip")
        one, two = self.job(self.a, p), self.job(self.a, p, 2)
        page = self.client.get("jobs", headers=self.a, params={"before": two, "limit": 1}).json()
        self.assertEqual([j["id"] for j in page], [one])
        self.assertEqual(self.client.get("jobs", headers=self.b, params={"before": two}).status_code, 404)
        self.assertEqual(self.job(self.a, p), one)
        self.assertEqual(self.client.get(f"projects/{p}", headers=self.b).status_code, 404)
        self.assertEqual(self.client.get(f"jobs/{one}", headers=self.b).status_code, 404)
        first = self.store.claim("cpu", 2)
        self.assertEqual(first["id"], one)
        self.assertIsNone(self.store.claim("cpu", 2))
        self.store.finish(
            first, {"plan": [["track", []], ["save_fbx", []]], "result": "initial", "frames": 30, "fps": 30}
        )
        self.assertIsNone(self.store.claim("cpu", 2))
        step = self.store.claim("gpu", 1)
        self.assertEqual(step["id"], one)
        self.store.recover()
        again = self.store.claim("gpu", 1)
        self.assertEqual(again["seq"], step["seq"])
        self.assertEqual(self.client.post(f"jobs/{one}/cancel", headers=self.a).json()["state"], "cancelling")
        self.store.finish(again, {}, "interrupted")
        self.assertEqual(
            self.client.get(f"jobs/{one}", headers=self.a).json()["steps"][-1]["state"], "skipped"
        )
        self.assertEqual(self.client.post(f"jobs/{one}/retry", headers=self.a).status_code, 202)
        self.assertEqual(self.client.post(f"jobs/{two}/cancel", headers=self.a).json()["state"], "cancelled")
        self.assertEqual(self.store.claim("gpu", 1)["id"], one)

    def test_lane_capacity_and_recent_gpu_time(self):
        projects = [(h, self.project(h, n)) for h, n in ((self.a, "a1"), (self.a, "a2"), (self.b, "b"))]
        a1, a2, b = [self.job(h, p) for h, p in projects]
        first, second = self.store.claim("cpu", 2), self.store.claim("cpu", 2)
        self.assertEqual((first["id"], second["id"]), (a1, b))
        self.assertIsNone(self.store.claim("cpu", 2))
        outcome = {"plan": [["track", []], ["save_fbx", []]], "result": "initial", "frames": 30, "fps": 30}
        self.store.finish(first, outcome)
        self.store.finish(second, outcome)
        third = self.store.claim("cpu", 2)
        self.assertEqual(third["id"], a2)
        self.store.finish(third, outcome)
        first_gpu = self.store.claim("gpu", 1)
        self.assertEqual(first_gpu["id"], a1)
        self.assertIsNone(self.store.claim("gpu", 1))
        self.store.finish(first_gpu, {})
        self.assertEqual(self.store.claim("gpu", 1)["id"], b)

    def test_artifact_visibility_and_range(self):
        """Only the owning account can consume the content-addressed motion."""
        p = self.project(self.a, "asset")
        job = self.job(self.a, p)
        blob = b"example artifact"
        sha = hashlib.sha256(blob).hexdigest()
        (self.store.root / "artifacts" / sha).write_bytes(blob)
        with self.store.db(True) as db:
            db.execute(
                "UPDATE jobs SET state='succeeded',artifact=?,bytes=? WHERE id=?", (sha, len(blob), job)
            )
        self.assertEqual(self.client.get("artifacts/" + sha, headers=self.b).status_code, 404)
        result = self.client.get("artifacts/" + sha, headers={**self.a, "Range": "bytes=3-"})
        self.assertEqual((result.status_code, result.content), (206, blob[3:]))
        self.assertEqual(self.client.get("jobs/" + job, headers=self.a).json()["project_name"], "asset")

    def test_cancelled_job_retry_obeys_quota(self):
        project = self.project(self.a, "quota")
        first = self.job(self.a, project, 1)
        self.client.post(f"jobs/{first}/cancel", headers=self.a).raise_for_status()
        for i in range(1, 21):
            self.job(self.a, project, 1 + i / 10)
        self.assertEqual(self.client.post(f"jobs/{first}/retry", headers=self.a).status_code, 429)


if __name__ == "__main__":
    unittest.main()
