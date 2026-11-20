"""Regression tests for rebuilding and copying an offline server bundle."""
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy"))
import build_bundle as build
import check_server as check


class BundleTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()

    def test_copy_rounding_requires_correct_bytes(self):
        model = self.root / "weight"
        model.write_bytes(b"good")
        mtime = model.stat().st_mtime_ns
        row = dict(file="weight", bytes=4, mtime_ns=mtime, sha256=check.file_hash(model))
        rounded = mtime - 2_000_000_000
        os.utime(model, ns=(rounded, rounded))
        check.check_file(model, row, full=False)
        self.assertEqual(model.stat().st_mtime_ns, mtime)
        model.write_bytes(b"oops")
        os.utime(model, ns=(rounded, rounded))
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            check.check_file(model, row, full=False)
        self.assertEqual(model.stat().st_mtime_ns, rounded)
        os.utime(model, ns=(mtime, mtime))
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            check.check_file(model, row, full=True)

    def test_manifest_requires_each_model_once(self):
        path = self.root / "assets.json"
        rows = [{"key": key} for key in check.MODEL_KEYS]
        path.write_text(json.dumps(rows + rows[:1]))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            check.read_manifest(path)
        path.write_text(json.dumps(rows[1:]))
        with self.assertRaisesRegex(ValueError, "Missing"):
            check.read_manifest(path)

    def test_rebuild_uses_registered_files_and_new_service_only(self):
        seed = self.root / "seed"
        src = seed / "engine"
        model = src / "models/body/weight"
        model.parent.mkdir(parents=True)
        model.write_bytes(b"fixture")
        digest = check.file_hash(model)
        receipt = dict(stat=[7, model.stat().st_mtime_ns], sha256=digest)
        model.with_name("weight.receipt.json").write_text(json.dumps(dict(receipt, source="private path")))
        (model.parent / "LICENSE.txt").write_text("model terms")
        (model.parent / "other-model").write_text("unselected")
        (seed / "assets.json").write_text(json.dumps([dict(
            key="local", file="body/weight", bytes=7, mtime_ns=receipt["stat"][1], sha256=digest, receipt=receipt
        )]))
        for name in ("data/upload", "engine/cache/exmr/copy", "engine/third_party/FlowHMR/alias"):
            path = seed / name
            path.parent.mkdir(parents=True)
            path.write_bytes(b"private or derived")
        dest = self.root / "release"
        engine = dest / "engine"
        engine.mkdir(parents=True)
        registered = {"local": SimpleNamespace(relative="body/weight", expected_sha256=None)}
        with patch.object(build, "load_models", return_value=(registered, None)), \
             patch.object(build, "MODEL_KEYS", ("local",)), patch.object(check, "MODEL_KEYS", ("local",)):
            build.copy_models(src, engine, seed)
        self.assertEqual((engine / "models/body/weight").read_bytes(), model.read_bytes())
        self.assertEqual(check.read_json(engine / "models/body/weight.receipt.json"), receipt)
        self.assertEqual((engine / "models/body/LICENSE.txt").read_text(), "model terms")
        build.prepare_service(dest)
        self.assertFalse((dest / "data").exists())
        self.assertFalse((engine / "cache").exists())
        self.assertFalse((engine / "third_party").exists())
        self.assertFalse((engine / "models/body/other-model").exists())
        with ZipFile(next((dest / "client").glob("*.zip"))) as archive:
            self.assertCountEqual(archive.namelist(), (
                "blender_manifest.toml", "__init__.py", "translations.py", "client.py"))

    def test_destination_must_be_new_and_separate(self):
        src = self.root / "input"
        src.mkdir()
        with self.assertRaises(FileExistsError):
            build.check_destination(src, src)
        with self.assertRaisesRegex(ValueError, "overlaps"):
            build.check_destination(src / "output", src)
        with self.assertRaisesRegex(ValueError, "local"):
            build.check_destination(build.REPO / "deploy/nested-output", src)
        build.check_destination(self.root / "release", src)


if __name__ == "__main__":
    unittest.main()
