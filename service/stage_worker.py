"""One scheduled step, running inside the pinned upstream shared interpreter."""

import contextlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace


def emit(kind, **values):
    print(json.dumps({"event": kind, **values}, allow_nan=False), file=sys.__stdout__, flush=True)


def execute(request):
    root, out = Path(request["root"]), Path(request["out"])
    sys.path[:0] = [str(root), str(Path(__file__).resolve().parents[1] / "poc")]
    import poc_headless as pipeline

    opts = request["options"]
    args = SimpleNamespace(
        root=root,
        out=out,
        video=Path(request["video"]),
        model=request["model"],
        start=opts["start"],
        end=opts["end"],
        compute_mode=opts["compute_mode"],
        no_refine=not opts["refine"],
        no_footmr=not opts["footmr"],
        no_handflow=not opts["handflow"],
        no_correction=not opts["correction"],
    )
    env = pipeline.configure_environment(root, args)
    env["RGB_MOCAP_EXMR_OPTIONS"] = json.dumps(
        {"static_camera": opts["static_camera"], "smoothing": "global" if opts["smooth"] else "none"}
    )
    os.environ["RGB_MOCAP_EXMR_OPTIONS"] = env["RGB_MOCAP_EXMR_OPTIONS"]
    from mocap.queue_groups import BATCH_STAGES, remove_request, write_request
    from mocap.storage import atomic_json

    out.mkdir(parents=True, exist_ok=True)
    stage, arguments = request["stage"], request["arguments"]
    project = out / "workspace" / args.model
    result = request["result"]
    if stage == "prepare":
        result, _, _, _, jobs = pipeline.initial_plan(args)
        _, arguments, tail = pipeline.next_job(jobs, None)
    if stage == "rig" and (project / f"{result}_mixamo.json").is_file():
        return {"cached": True}

    temporary = None
    try:
        if stage == "save_fbx":
            temporary = out / "export-request.json"
            atomic_json(temporary, [pipeline.fbx_export_row(project, result, out / "motion.fbx")])
            arguments = ["--project", str(project), "--output", str(temporary)]
        elif stage in BATCH_STAGES:
            temporary = write_request(stage, arguments)
            arguments = ["--request", str(temporary), "--project", str(project)]
        with (out / "worker.log").open("a", encoding="utf8") as log:
            code, events, _, _ = pipeline.run_stage(
                root,
                env,
                stage,
                arguments,
                log,
                on_progress=lambda e: emit("progress", current=e.get("current", 0), total=e.get("total", 0)),
            )
        errors = [e for e in events if e["type"] in ("failed", "blocked")]
        if code or errors:
            emit(
                "failed",
                code="missing_models"
                if code == 3 or any(e["type"] == "blocked" for e in errors)
                else "worker_failed",
            )
            raise RuntimeError(f"{stage} exited with {code}: {errors}")
    finally:
        if temporary is not None:
            if stage in BATCH_STAGES:
                remove_request(temporary)
            else:
                temporary.unlink(missing_ok=True)
    if stage == "prepare":
        actual = next((e.get("project") for e in events if e["type"] == "prepared"), None)
        if not actual or Path(actual).resolve() != project.resolve():
            raise ValueError("Unexpected prepared project")
        plan = []
        while tail:
            name, values, tail = pipeline.next_job(tail, project)
            plan.append([name, values])
        plan.extend([["rig", ["--project", str(project), "--result", result]], ["save_fbx", []]])
        info = json.loads((project / "project.json").read_text(encoding="utf8"))
        return {"result": result, "plan": plan, "frames": info["frames"], "fps": info["fps"]}
    if stage == "save_fbx" and (out / "motion.fbx").stat().st_size == 0:
        raise RuntimeError("Empty FBX")
    complete = [e for e in events if e["type"] == "complete"]
    return {"cached": bool(complete) and all(e.get("cached") for e in complete)}


if __name__ == "__main__":
    # The parent sends the request only after the process belongs to its Windows Job Object.
    line = sys.stdin.buffer.readline()
    if line:
        with contextlib.redirect_stdout(sys.stderr):
            emit("done", **execute(json.loads(line)))
