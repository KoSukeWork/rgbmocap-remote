"""Check a Windows offline bundle at its current location."""
import argparse
import hashlib
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys


# Includes the optional refinements exposed by the three service pipelines.
MODEL_KEYS = (
    "yolo", "person_reid", "gvhmr", "hmr2", "vitpose", "pva", "footmr",
    "vitpose_wholebody", "handflow", "handflow_stats", "hand_hamer", "hand_config",
    "hand_mean", "hand_detector", "exmr", "exmr_config", "exmr_sapiens", "flowhmr",
    "flow_config", "flow_metadata", "flow_sam", "flow_sam_config", "flow_mhr",
    "flow_vggt", "smplx", "mano_right", "smplh",
)


def file_hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_manifest(path):
    rows = read_json(path)
    assets = {row["key"]: row for row in rows}
    if set(assets) != set(MODEL_KEYS) or len(rows) != len(MODEL_KEYS):
        raise ValueError(f"Missing, duplicate or unexpected models in {path}")
    return assets


def load_models(engine):
    os.environ["RGB_MOCAP_DATA"] = str(engine)
    sys.path.insert(0, str(engine))
    from mocap.models import BY_KEY, ready
    return BY_KEY, ready


def check_file(path, row, full):
    info = path.stat()
    if info.st_size != row["bytes"]:
        raise ValueError(f"Incomplete model: {path}. Copy the file again.")
    changed_time = info.st_mtime_ns != row["mtime_ns"]
    if full or changed_time:
        print(f"Checking {row['file']}", flush=True)
        if file_hash(path) != row["sha256"]:
            raise ValueError(f"Incorrect SHA-256: {path}. Copy the file again.")
        if changed_time:
            # RDP and archive tools may round timestamps used by upstream receipts.
            os.utime(path, ns=(info.st_atime_ns, row["mtime_ns"]))
            if path.stat().st_mtime_ns != row["mtime_ns"]:
                raise ValueError(f"Cannot preserve model timestamp: {path}. Use NTFS.")


def check_models(root, full):
    assets = read_manifest(root / "assets.json")
    models, ready = load_models(root / "engine")
    for key, row in assets.items():
        model = models[key]
        if row["file"] != Path(model.relative).as_posix():
            raise ValueError(f"Incorrect model path in assets.json: {key}")
        if model.expected_sha256 and row["sha256"] != model.expected_sha256:
            raise ValueError(f"Unexpected SHA-256 in assets.json: {key}")
        check_file(model.path, row, full)
        if "receipt" in row:
            receipt = read_json(Path(str(model.path) + ".receipt.json"))
            if any(receipt.get(k) != v for k, v in row["receipt"].items()):
                raise ValueError(f"Incorrect model receipt: {model.path}")
        if not ready(model):
            raise ValueError(f"Upstream model manager rejected {key}. Check its file and receipt.")


def probe_profile(engine, profile):
    sys.path.insert(0, str(engine))
    from mocap.runtime_registry import activate
    activate(engine, profile)
    if profile in {"shared", "exmr"}:
        import torch
        if torch.ones(1, device="cuda").add_(1).item() != 2:
            raise RuntimeError(f"{profile}: CUDA tensor check failed")
        print(profile, torch.__version__, torch.cuda.get_device_name(0), flush=True)
    else:
        print(profile, "runtime ready", flush=True)


def check_runtime(root):
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / "poc"))
    from poc_headless import verify_install
    from service import PIN
    for module in ("service.api", "win32job", "uvicorn"):
        importlib.import_module(module)
    for line in (root / "service/requirements.txt").read_text(encoding="utf-8").splitlines():
        name, expected = line.split("==")
        if importlib.metadata.version(name) != expected:
            raise ValueError(f"Service requires {line}. Rebuild its Python from the prepared installation.")

    engine = root / "engine"
    verify_install(engine)
    git = root / "git/cmd/git.exe"
    rev = subprocess.check_output([git, "-C", engine, "rev-parse", "HEAD"], text=True).strip()
    if rev != PIN:
        raise ValueError(f"Incorrect upstream commit: {rev}")
    env = dict(os.environ, RGB_MOCAP_DATA=str(engine), RGB_MOCAP_MANAGED="1")
    for profile in ("shared", "exmr", "flow", "hand", "htd", "gui"):
        runtime = "runtime_exmr" if profile == "exmr" else "runtime"
        subprocess.run([engine / runtime / "python.exe", "-I", "-B", "-X", "utf8",
                        __file__, "--profile", profile], env=env, check=True)
    for binary in ("tools/bin/ffmpeg.exe", "tools/bin/ffprobe.exe", "runtime_character/blender.exe"):
        flag = "--version" if binary.endswith("blender.exe") else "-version"
        version = subprocess.check_output([engine / binary, flag], text=True, encoding="utf-8", errors="replace")
        print(version.splitlines()[0], flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick", action="store_true", help="Hash models only when timestamps changed")
    parser.add_argument("--profile", help=argparse.SUPPRESS)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    if args.profile:
        probe_profile(root / "engine", args.profile)
        return
    check_models(root, full=not args.quick)
    if not args.quick:
        check_runtime(root)
        print(f"Passed: {len(MODEL_KEYS)} models, service imports, upstream, six profiles, CUDA and export tools.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, KeyError, ValueError, subprocess.SubprocessError) as exc:
        sys.exit(str(exc))
