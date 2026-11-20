"""Run with Blender 5.3 in background mode and the installed extension enabled."""

import argparse
import importlib
import json
import os
import sys
import time

import bpy


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", required=True)
    parser.add_argument("--module", default="bl_ext.user_default.rgbmocap_remote")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--video", help="Submit a new clip from this path")
    source.add_argument("--job", help="Download an existing job from this account's recent history")
    parser.add_argument("--model", choices=("exmr", "gvhmr", "flowhmr"), default="exmr")
    parser.add_argument("--end", type=float, default=0)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1 :])
    if not bpy.app.background:
        raise RuntimeError("Use a background Blender process for this check.")
    addon = importlib.import_module(args.module)
    state = bpy.context.window_manager.rgbmocap
    state.address = args.server
    state.token = os.environ.pop("RGBMOCAP_TEST_TOKEN")
    deadline = time.monotonic() + 3600

    def request(action):
        if bpy.ops.rgbmocap.request(action=action) != {"FINISHED"}:
            raise RuntimeError(action)
        # Background Python occupies Blender's main loop, so dispatch the add-on timer here.
        while addon._busy:
            if time.monotonic() >= deadline:
                raise TimeoutError(action)
            time.sleep(0.1)
            addon.tick()
        if addon._status not in ("", "Motion imported"):
            raise RuntimeError(addon._status)

    request("connect")
    if not addon._session["accepted"]:
        raise RuntimeError("The test account must already have accepted the provider terms.")
    job = args.job
    if args.video:
        state.video, state.model, state.end = args.video, args.model, args.end
        request("submit")
        job = addon.selected(state).job_id
    while True:
        state.index = next(i for i, row in enumerate(state.jobs) if row.job_id == job)
        row = addon.selected(state)
        if row.status == "succeeded":
            break
        if row.status in {"failed", "cancelled"}:
            raise RuntimeError(f"{row.status} {row.error}")
        if time.monotonic() >= deadline:
            raise TimeoutError(job)
        time.sleep(2)
        request("refresh")

    request("download")
    scene = bpy.context.scene
    rig = next(obj for obj in scene.objects if obj.type == "ARMATURE" and obj.get("rgbmocap_job") == job)
    action = rig.animation_data.action
    first, last = action.frame_range
    if len(rig.data.bones) != 65 or last < first or scene.render.fps != 30:
        raise AssertionError("Unexpected skeleton or animation timing")
    objects = len(scene.objects)
    scene.render.fps = 24
    scene.frame_end += 10
    request("download")
    if len(scene.objects) != objects:
        raise AssertionError("Importing the same job duplicated the rig")
    if scene.render.fps != 30 or scene.frame_end != round(last):
        raise AssertionError("Reusing an existing rig did not restore its playback timing")
    print(
        json.dumps(
            {
                "model": row.model,
                "job": job,
                "bones": len(rig.data.bones),
                "action": action.name,
                "frames": round(last - first) + 1,
                "fps": scene.render.fps,
            }
        )
    )


if __name__ == "__main__":
    main()
