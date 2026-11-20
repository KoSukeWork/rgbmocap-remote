"""Build a relocatable Windows x64 RGBMoCap server from local, licensed inputs."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile

from check_server import MODEL_KEYS, file_hash, load_models, read_json, read_manifest

DEPLOY = Path(__file__).resolve().parent
REPO = DEPLOY.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "poc"))
from poc_headless import verify_install
from service import PIN

RUNTIMES = ("runtime", "runtime_exmr", "runtime_flow", "runtime_hand", "runtime_htd",
            "runtime_gui", "runtime_pear", "runtime_character", "tools/bin")
SETUP = ("git-runtime-state.json", "git-components.json", "pear-runtime.json", "pear-alignment.json")


def copy_file(source, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)


def copy_tree(source, dest):
    if not source.is_dir():
        raise FileNotFoundError(source)
    print(f"Copying {source.name} ...", flush=True)
    status = subprocess.run(["robocopy", source, dest, "/E", "/XJ", "/COPY:DAT", "/DCOPY:T",
                             "/MT:8", "/R:2", "/W:1", "/NP", "/NJH", "/NJS", "/NFL", "/NDL"]).returncode
    if status >= 8:
        raise RuntimeError(f"robocopy failed ({status}): {source}")


def prepare_engine(source, engine):
    # Fetch just the pinned tree, without source-local settings, alternates, reflogs or remotes.
    git = ["git", "-C", engine]
    engine.mkdir()
    subprocess.run([*git, "init", "--quiet", "--template="], check=True)
    subprocess.run([*git, "config", "core.logAllRefUpdates", "false"], check=True)
    subprocess.run([*git, "fetch", "--quiet", "--no-tags", "--no-write-fetch-head",
                    "--depth=1", source.as_uri(), PIN], check=True)
    subprocess.run([*git, "checkout", "--quiet", "--detach", PIN], check=True)
    for name in SETUP:
        copy_file(source / ".setup" / name, engine / ".setup" / name)
    verify_install(engine)
    # Only the pin's installer overlays supplement tracked source. Runtime-generated
    # checkpoint aliases under third_party and the ExMR cache are intentionally omitted.
    overlays = read_json(engine / "installer/external-assets.json")["files"]
    native = read_json(engine / "installer/git-support-lock.json")["correction"]
    native["path"] = "third_party/KimodoMotionCorrection/bin/" + Path(native["path"]).name
    for entry in [*overlays, native]:
        dest = engine / entry["path"]
        copy_file(source / entry["path"], dest)
        if dest.stat().st_size != entry["bytes"] or file_hash(dest) != entry["sha256"]:
            raise ValueError(f"Unexpected installed support file: {entry['path']}")


def copy_models(source, engine, seed):
    models, _ = load_models(engine)
    previous = read_manifest(seed / "assets.json") if seed else {}
    rows = []
    for key in MODEL_KEYS:
        model = models[key]
        relative = Path(model.relative)
        src = source / "models" / relative
        old = previous.get(key)
        stat = src.stat()
        if old and (old["file"] != relative.as_posix() or old["bytes"] != stat.st_size):
            raise ValueError(f"Source bundle inventory mismatch: {key}")
        mtime = old["mtime_ns"] if old else stat.st_mtime_ns
        checksum = model.expected_sha256 or (old["sha256"] if old else file_hash(src))
        if old and old["sha256"] != checksum:
            raise ValueError(f"Source bundle checksum disagrees with upstream: {key}")
        row = dict(key=key, file=relative.as_posix(), bytes=stat.st_size, mtime_ns=mtime, sha256=checksum)
        dest = engine / "models" / relative
        copy_file(src, dest)
        receipt = Path(str(src) + ".receipt.json")
        if receipt.exists():
            raw = read_json(receipt)
            if raw["stat"] != [stat.st_size, mtime] or raw["sha256"] != checksum:
                raise ValueError(f"Model receipt mismatch: {key}")
            # Upstream only needs these fields. Manual import receipts may also contain user paths.
            row["receipt"] = dict(stat=raw["stat"], sha256=checksum)
            if old and "receipt" in old and old["receipt"] != row["receipt"]:
                raise ValueError(f"Source bundle receipt disagrees with assets.json: {key}")
            Path(str(dest) + ".receipt.json").write_text(json.dumps(row["receipt"]), encoding="utf-8")
        elif old and "receipt" in old:
            raise FileNotFoundError(receipt)
        license_file = src.with_name("LICENSE.txt")
        if license_file.is_file():
            copy_file(license_file, dest.with_name("LICENSE.txt"))
        rows.append(row)
    (engine.parent / "assets.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")


def prepare_python(source, seed, dest, offline):
    if seed:
        copy_tree(seed / "python", dest)
        return
    copy_tree(source / ".setup/python/cpython-3.12.10-windows-x86_64-none", dest)
    cmd = ["uv", "--no-config"]
    if offline:
        cmd.append("--offline")
    subprocess.run([*cmd, "pip", "install", "--no-build", "--link-mode", "copy",
                    "--python", dest / "python.exe", "--target", dest / "Lib/site-packages",
                    "-r", REPO / "service/requirements.txt", "-c", DEPLOY / "service-constraints.txt"], check=True)


def prepare_git(seed, dest):
    if seed:
        source = seed / "git"
        licenses = source / "licenses"
    else:
        source = Path(shutil.which("git")).resolve().parents[1]
        licenses = source / "mingw64/share/licenses"
    for name in ("cmd/git.exe", "mingw64/bin/git.exe", "LICENSE.txt"):
        copy_file(source / name, dest / name)
    for dll in (source / "mingw64/bin").glob("*.dll"):
        copy_file(dll, dest / "mingw64/bin" / dll.name)
    copy_tree(licenses, dest / "licenses")


def prepare_service(stage):
    for name in ("__init__.py", "__main__.py", "api.py", "dispatcher.py",
                 "store.py", "stage_worker.py", "requirements.txt"):
        copy_file(REPO / "service" / name, stage / "service" / name)
    for name in ("gpu_usage.py", "poc_headless.py"):
        copy_file(REPO / "poc" / name, stage / "poc" / name)
    for name in ("Check-Server.cmd", "Create-User.cmd", "Start-Server.cmd", "check_server.py", "README.md"):
        copy_file(DEPLOY / name, stage / name)
    addon = REPO / "addon/rgbmocap_remote"
    with (addon / "blender_manifest.toml").open("rb") as stream:
        manifest = tomllib.load(stream)
    client = stage / "client"
    client.mkdir()
    with zipfile.ZipFile(client / f"{manifest['id']}-{manifest['version']}.zip", "x", zipfile.ZIP_DEFLATED) as archive:
        for name in ("blender_manifest.toml", "__init__.py", "client.py", "translations.py"):
            archive.write(addon / name, name)


def check_destination(dest, source):
    if dest.exists() or dest.is_symlink():
        raise FileExistsError(f"Use a new output directory: {dest}")
    if dest.is_relative_to(source) or source.is_relative_to(dest):
        raise ValueError("Output overlaps the input directory.")
    if REPO.is_relative_to(dest) or (dest.is_relative_to(REPO) and not dest.is_relative_to(REPO / ".local")):
        raise ValueError("Inside the source repository, put output under .local.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--upstream", type=Path, help="Prepared upstream Windows Git installation")
    source.add_argument("--from-bundle", type=Path, help="Previously delivered bundle containing engine and assets.json")
    parser.add_argument("--output", type=Path, default=REPO / ".local/RGBMoCap-Server")
    parser.add_argument("--offline", action="store_true", help="Use only cached wheels when installing service dependencies")
    args = parser.parse_args()
    seed = args.from_bundle.resolve(strict=True) if args.from_bundle else None
    upstream = seed / "engine" if seed else args.upstream.resolve(strict=True)
    dest = args.output.resolve()
    check_destination(dest, seed or upstream)
    rev = subprocess.check_output(["git", "-C", upstream, "rev-parse", "HEAD"], text=True).strip()
    if rev != PIN:
        raise ValueError(f"Expected upstream {PIN}, found {rev}")
    verify_install(upstream)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # A failed check or interrupted copy cannot create a finished package with the requested name.
    with tempfile.TemporaryDirectory(prefix=".rgbmocap-build-", dir=dest.parent) as temp:
        stage = Path(temp) / "bundle"
        stage.mkdir()
        engine = stage / "engine"
        prepare_engine(upstream, engine)
        prepare_python(upstream, seed, stage / "python", args.offline)
        prepare_git(seed, stage / "git")
        prepare_service(stage)
        for name in RUNTIMES:
            copy_tree(upstream / name, engine / name)
        print(f"Copying {len(MODEL_KEYS)} model assets ...", flush=True)
        copy_models(upstream, engine, seed)
        subprocess.run([stage / "python/python.exe", "-I", "-B", "-X", "utf8", stage / "check_server.py"], check=True)
        stage.rename(dest)
    print(f"Ready: {dest}")


if __name__ == "__main__":
    try:
        main()
    except (KeyError, ValueError, OSError, subprocess.SubprocessError) as exc:
        sys.exit(str(exc))
