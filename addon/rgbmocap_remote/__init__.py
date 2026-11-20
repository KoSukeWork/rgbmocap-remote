import math
import queue
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode

import bpy
from bpy.app.handlers import persistent
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)

from .client import Client
from .translations import messages

TERMS = "https://github.com/toyxyz/toyxyz_RGBMocap/blob/42844135d0612c7e574fd43f6c42414f5fce1219/THIRD_PARTY_NOTICES.md"
_results = queue.Queue()
_epoch = 0
_busy = False
_session = None
_status = ""
_next_refresh = 0.0
_submission = None
_stop = None
_token = ""
_pages = ("",)
_has_older = False


def clear_session(state=None, context=None):
    global _epoch, _session, _busy, _status, _submission, _pages, _has_older
    _epoch += 1
    if _stop:
        _stop.set()
    _session, _busy, _status, _submission = None, False, "", None
    _pages, _has_older = ("",), False
    if bpy.context.window_manager and hasattr(bpy.context.window_manager, "rgbmocap"):
        state = bpy.context.window_manager.rgbmocap
        state.jobs.clear()
        state.index = 0
        state.agree = False


def get_token(_state):
    return _token


def set_token(_state, value):
    global _token
    _token = value


def reset_credentials(state, context):
    set_token(state, "")
    clear_session()


@persistent
def reset_on_load(_unused):
    reset_credentials(None, None)


class RemoteJob(bpy.types.PropertyGroup):
    job_id: StringProperty()
    status: StringProperty()
    model: StringProperty()
    stage: StringProperty()
    error: StringProperty()


class RemoteState(bpy.types.PropertyGroup):
    address: StringProperty(
        name="Server", default="http://127.0.0.1:8765", update=reset_credentials, options={"SKIP_SAVE"}
    )
    token: StringProperty(
        name="Personal token",
        subtype="PASSWORD",
        get=get_token,
        set=set_token,
        update=clear_session,
        options={"SKIP_SAVE"},
    )
    agree: BoolProperty(name="I accept the provider terms for noncommercial use", options={"SKIP_SAVE"})
    video: StringProperty(name="Video", subtype="FILE_PATH", options={"SKIP_SAVE"})
    model: EnumProperty(name="Model", items=[(n.lower(), n, "") for n in ("ExMR", "GVHMR", "FlowHMR")])
    start: FloatProperty(name="Start (s)", min=0, default=0)
    end: FloatProperty(name="End (s, 0 = full clip)", min=0, default=0)
    settings: BoolProperty(name="Reconstruction options", default=False)
    low_memory: BoolProperty(name="Low memory")
    correction: BoolProperty(name="Contact correction", default=True)
    static_camera: BoolProperty(name="Static camera", default=True)
    smooth: BoolProperty(name="Smooth motion", default=True)
    refine: BoolProperty(name="Refine body (HTD)", default=True)
    footmr: BoolProperty(name="Refine ankles (FootMR)", default=True)
    handflow: BoolProperty(name="Track fingers (HandFlow)", default=True)
    jobs: CollectionProperty(type=RemoteJob, options={"SKIP_SAVE"})
    index: IntProperty(min=0, options={"SKIP_SAVE"})


def selected(state):
    return state.jobs[state.index] if 0 <= state.index < len(state.jobs) else None


def update_jobs(state, jobs, focus=None):
    current = selected(state)
    wanted = focus or (current.job_id if current else "")
    state.jobs.clear()
    state.index = 0
    for i, remote in enumerate(jobs):
        row = state.jobs.add()
        row.job_id, row.status, row.model = remote["id"], remote["state"], remote["model"]
        row.name = remote.get("project_name", remote["project"])
        row.error = remote["error"] or ""
        step = next((s for s in remote["steps"] if s["state"] == "running"), None)
        row.stage = step["stage"] if step else ""
        if step and step["total"]:
            row.stage += f" {step['current']}/{step['total']}"
        if row.job_id == wanted:
            state.index = i


def redraw():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def launch(operation, message):
    global _busy, _status, _stop
    generation = _epoch
    _busy, _status = True, message
    _stop = stop = threading.Event()

    def notify(value):
        if stop.is_set():
            raise InterruptedError("Session closed")
        _results.put((generation, "progress", value))

    def work():
        try:
            result = operation(notify)
            _results.put((generation, "done", result))
        except Exception as exc:
            _results.put((generation, "error", str(exc)))

    threading.Thread(target=work, daemon=True, name="RGBMoCap network").start()


def import_motion(context, path, job):
    scene = context.scene
    rig = next(
        (
            obj
            for obj in scene.objects
            if obj.type == "ARMATURE"
            and obj.get("rgbmocap_artifact") == job["artifact"]
            and obj.animation_data
            and obj.animation_data.action
        ),
        None,
    )
    if rig is None:
        previous = set(scene.objects)
        status = bpy.ops.wm.fbx_import(filepath=str(path), use_anim=True, anim_offset=0)
        rigs = [
            obj
            for obj in scene.objects
            if obj not in previous
            and obj.type == "ARMATURE"
            and obj.animation_data
            and obj.animation_data.action
        ]
        if "FINISHED" not in status or len(rigs) != 1:
            raise ValueError("The FBX did not import as one animated rig.")
        rig = rigs[0]
        rig["rgbmocap_artifact"] = job["artifact"]
        rig["rgbmocap_job"] = job["id"]
        rig["rgbmocap_fps"] = scene.render.fps
        rig["rgbmocap_fps_base"] = scene.render.fps_base
    scene.render.fps = rig.get("rgbmocap_fps", 30)
    scene.render.fps_base = rig.get("rgbmocap_fps_base", 1.0)
    first, last = rig.animation_data.action.frame_range
    scene.frame_start, scene.frame_end = math.floor(first), math.ceil(last)
    scene.frame_set(scene.frame_start)
    return rig


class RGBMOCAP_OT_import(bpy.types.Operator):
    bl_idname = "rgbmocap.import_download"
    bl_label = "Import motion and set scene timing"
    bl_options = {"UNDO", "INTERNAL"}
    filepath: StringProperty(options={"SKIP_SAVE"})
    sha: StringProperty(options={"SKIP_SAVE"})
    job_id: StringProperty(options={"SKIP_SAVE"})

    def execute(self, context):
        rig = import_motion(context, self.filepath, {"artifact": self.sha, "id": self.job_id})
        self.report({"INFO"}, rig.name)
        return {"FINISHED"}


def submit_action(context, action):
    global _submission
    if _busy:
        raise ValueError("A request is already in progress.")
    if not bpy.app.online_access:
        raise ValueError("Enable Online Access in Blender Preferences > System.")
    state = context.window_manager.rgbmocap
    client = Client(state.address, state.token)
    row = selected(state)
    job_id = row.job_id if row else ""
    scene_id = context.scene.session_uid

    pages = _pages
    if action in {"connect", "accept", "submit"}:
        pages = ("",)
    elif action == "older" and len(state.jobs):
        pages += (state.jobs[-1].job_id,)
    elif action == "newer" and len(pages) > 1:
        pages = pages[:-1]

    def page():
        query = {"limit": 51}
        if pages[-1]:
            query["before"] = pages[-1]
        rows = client.json("GET", "jobs?" + urlencode(query))
        return {"jobs": rows[:50], "pages": pages, "more": len(rows) > 50}

    if action == "connect":

        def operation(notify):
            me = client.json("GET", "me")
            if me["api_version"] != 1:
                raise ValueError("Unsupported server API.")
            return {"me": me, **(page() if me["accepted"] else {})}
    elif action == "accept":
        if not _session or not state.agree:
            raise ValueError("Review and accept the provider terms first.")
        version = _session["license_version"]

        def operation(notify):
            client.json("POST", "me/license", {"version": version})
            return {"me": client.json("GET", "me"), **page()}
    elif action == "submit":
        if not state.video:
            raise ValueError("Choose a video first.")
        video = Path(bpy.path.abspath(state.video)).resolve()
        model = state.model
        options = {
            "start": state.start,
            "end": state.end or None,
            "correction": state.correction,
            "compute_mode": "low_memory" if state.low_memory else "auto",
        }
        if model == "exmr":
            options.update(static_camera=state.static_camera, smooth=state.smooth)
        if model == "gvhmr":
            options.update(refine=state.refine, footmr=state.footmr, handflow=state.handflow)
        if options["end"] and options["end"] <= options["start"]:
            raise ValueError("End must be later than start.")
        signature = (
            str(video),
            video.stat().st_size,
            video.stat().st_mtime_ns,
            model,
            tuple(options.items()),
        )
        if not _submission or _submission[0] != signature:
            _submission = (signature, uuid.uuid4().hex)
        key = _submission[1]

        def operation(notify):
            focus = client.submit(video, model, options, notify, key)
            return {**page(), "focus": focus, "submitted": True}
    elif action == "download":
        cache = bpy.utils.extension_path_user(__package__, path="motion", create=True)

        def operation(notify):
            job = client.json("GET", "jobs/" + job_id)
            path = client.download(job, cache, notify)
            return {"import": (scene_id, str(path), job)}
    else:

        def operation(notify):
            if action in {"cancel", "retry"}:
                client.json("POST", f"jobs/{job_id}/{action}")
            return page()

    launch(operation, {"download": "Downloading motion", "submit": "Checking video"}.get(action, ""))


def tick():
    global _busy, _session, _status, _next_refresh, _submission, _pages, _has_older
    state = bpy.context.window_manager.rgbmocap
    changed = False
    while True:
        try:
            generation, kind, payload = _results.get_nowait()
        except queue.Empty:
            break
        if generation != _epoch:
            continue
        changed = True
        if kind == "progress":
            _status = payload
            continue
        _busy = False
        _next_refresh = time.monotonic() + (10 if kind == "error" else 3)
        if kind == "error":
            _status = payload
            continue
        _status = ""
        if "me" in payload:
            _session = payload["me"]
        if "jobs" in payload:
            _pages, _has_older = payload["pages"], payload["more"]
            update_jobs(state, payload["jobs"], payload.get("focus"))
        if "submitted" in payload:
            _submission = None
        if "import" in payload:
            scene_id, path, job = payload["import"]
            if bpy.context.scene and bpy.context.scene.session_uid == scene_id:
                try:
                    bpy.ops.rgbmocap.import_download(filepath=path, sha=job["artifact"], job_id=job["id"])
                    _status = "Motion imported"
                except Exception as exc:
                    _status = str(exc)
            else:
                _status = "Scene changed. Select the job to import again."
    if changed:
        redraw()
    if (
        not _busy
        and _session
        and _session["accepted"]
        and bpy.app.online_access
        and time.monotonic() >= _next_refresh
        and any(j.status in {"queued", "running", "cancelling"} for j in state.jobs)
    ):
        submit_action(bpy.context, "refresh")
    return 0.2 if _busy else 0.7


class RGBMOCAP_OT_request(bpy.types.Operator):
    bl_idname = "rgbmocap.request"
    bl_label = "Remote task"
    action: EnumProperty(
        items=[
            (s, s.title(), "")
            for s in (
                "connect",
                "accept",
                "submit",
                "refresh",
                "older",
                "newer",
                "cancel",
                "retry",
                "download",
            )
        ]
    )

    def execute(self, context):
        try:
            submit_action(context, self.action)
        except (OSError, ValueError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class RGBMOCAP_UL_jobs(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        layout.label(text=f"{item.name} · {item.model}", translate=False)
        layout.label(text=item.status)


def button(layout, action, label, icon="NONE"):
    layout.operator("rgbmocap.request", text=label, icon=icon).action = action


class RGBMOCAP_PT_remote(bpy.types.Panel):
    bl_label = "RGBMoCap Remote"
    bl_idname = "RGBMOCAP_PT_remote"
    bl_space_type, bl_region_type, bl_category = "VIEW_3D", "UI", "RGBMoCap"

    def draw(self, context):
        layout, state = self.layout, context.window_manager.rgbmocap
        col = layout.column()
        col.enabled = not _busy
        col.prop(state, "address")
        col.prop(state, "token")
        if not _session:
            button(col, "connect", "Connect")
        elif not _session["accepted"]:
            col.operator("wm.url_open", text="Read model provider terms", icon="URL").url = TERMS
            col.prop(state, "agree")
            row = col.row()
            row.enabled = state.agree
            button(row, "accept", "Confirm acceptance")
        else:
            col.label(text=_session["name"], icon="USER", translate=False)
            col.prop(state, "video")
            col.prop(state, "model")
            col.prop(state, "start")
            col.prop(state, "end")
            col.prop(state, "settings", icon="TRIA_DOWN" if state.settings else "TRIA_RIGHT", emboss=False)
            if state.settings:
                box = col.column(align=True)
                box.prop(state, "low_memory")
                box.prop(state, "correction")
                if state.model == "exmr":
                    box.prop(state, "static_camera")
                    box.prop(state, "smooth")
                if state.model == "gvhmr":
                    for option in ("refine", "footmr", "handflow"):
                        box.prop(state, option)
            button(col, "submit", "Submit reconstruction", "PLAY")
            col.separator()
            button(col, "refresh", "Refresh jobs", "FILE_REFRESH")
            col.template_list("RGBMOCAP_UL_jobs", "", state, "jobs", state, "index", rows=3)
            if _has_older or len(_pages) > 1:
                nav = col.row(align=True)
                if len(_pages) > 1:
                    button(nav, "newer", "Newer jobs")
                if _has_older:
                    button(nav, "older", "Older jobs")
            job = selected(state)
            if job:
                if job.stage or job.error:
                    col.label(text=job.error or job.stage, translate=False)
                if job.status == "succeeded":
                    button(col, "download", "Import motion and set scene timing", "IMPORT")
                elif job.status in {"queued", "running"}:
                    button(col, "cancel", "Cancel job")
                elif job.status in {"failed", "cancelled"}:
                    button(col, "retry", "Retry job")
        if _status:
            layout.label(text=_status)


_CLASSES = (
    RemoteJob,
    RemoteState,
    RGBMOCAP_OT_import,
    RGBMOCAP_OT_request,
    RGBMOCAP_UL_jobs,
    RGBMOCAP_PT_remote,
)


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.WindowManager.rgbmocap = PointerProperty(type=RemoteState, options={"SKIP_SAVE"})
    bpy.app.translations.register(__package__, messages)
    bpy.app.handlers.load_pre.append(reset_on_load)
    bpy.app.timers.register(tick, persistent=True)


def unregister():
    reset_credentials(None, None)
    bpy.app.timers.unregister(tick)
    bpy.app.handlers.load_pre.remove(reset_on_load)
    bpy.app.translations.unregister(__package__)
    del bpy.types.WindowManager.rgbmocap
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
