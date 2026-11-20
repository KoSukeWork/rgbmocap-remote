"""Run the pinned upstream Qt window on a fresh workspace for a phase-0 comparison."""
import argparse
import json
import os
from pathlib import Path
import sys
import time


def baseline(args):
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QSurfaceFormat
    from PySide6.QtWidgets import QApplication
    from mocap.gui import MainWindow
    from mocap.models import missing
    from mocap.motion_tools import MOTION_TOOLS
    from mocap.project_workspace import create
    from mocap.storage import atomic_json
    from mocap.theme import configure_application
    from mocap.video import probe

    output = args.out.resolve()
    output.mkdir(parents=True, exist_ok=False)
    video = args.video.resolve(strict=True)
    workspace = create(output / 'workspace', video, probe(video), args.model)
    surface = QSurfaceFormat.defaultFormat()
    surface.setSwapInterval(0)
    QSurfaceFormat.setDefaultFormat(surface)
    app = QApplication(sys.argv[:1])
    configure_application(app)
    window = MainWindow(None)
    app.aboutToQuit.connect(window.shutdown_preview)
    window.showMinimized()
    report = dict(model=args.model, video=str(video), workspace=str(workspace))
    state = 'open'
    started = time.monotonic()
    timer = QTimer()
    timer.setInterval(300)

    def finish(status, detail=''):
        timer.stop()
        report.update(status=status, detail=detail, elapsed_s=round(time.monotonic() - started, 2))
        atomic_json(output / 'gui_report.json', report)
        (output / 'gui.log').write_text(window.log.toPlainText(), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        app.exit(0 if status in ('ok', 'inspected') else 1)

    def tick():
        nonlocal state
        try:
            if state == 'open':
                if not window.load_project(workspace):
                    return finish('failed', window.status.text())
                state = 'ready'
            elif state == 'ready':
                if not window.video_readable or not window.info:
                    if time.monotonic() - started > 45:
                        finish('failed', 'Video did not open: ' + window.status.text())
                    return
                settings = window.workspace_settings()
                tool = MOTION_TOOLS[args.model]
                stages = tool.stages(bool(tool.refinement) and window.htd_refine.isChecked(),
                    args.model == 'gvhmr' and window.footmr_refine.isChecked(), window.motion_correction.isChecked(),
                    handflow=args.model == 'gvhmr' and window.handflow_refine.isChecked())
                absent = sorted({m.key for stage in stages for m in missing(stage, args.model)})
                report.update(settings=settings, start_end=window.processing_range(), missing_models=absent,
                    result=window.selected_result(), resource_safety=window.resource_safety.isChecked(),
                    compute_mode=window.compute_mode.currentData(), stages=stages)
                if args.inspect:
                    return finish('inspected')
                if absent:
                    return finish('blocked', 'Required model files are missing')
                if not window.run_button.isEnabled():
                    return finish('blocked', window.status.text())
                window.run_button.click()
                if not window.busy:
                    return finish('failed', window.status.text())
                state = 'reconstruction'
            elif not window.busy:
                if window.failed or window.cancelled or window.project is None:
                    return finish('failed', window.status.text())
                report['project'] = str(window.project)
                rig = window.project / (report['result'] + '_mixamo.json')
                if rig.is_file():
                    return finish('ok')
                if state == 'reconstruction':
                    state = 'preview'
                    window.prepare_target_preview()
                else:
                    finish('failed', 'Prepare Preview did not publish the selected rig')
        except Exception as exc:
            finish('failed', str(exc))

    timer.timeout.connect(tick)
    timer.start()
    return app.exec()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--video', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True, help='New directory for an independent GUI run')
    parser.add_argument('--model', choices=('exmr', 'gvhmr', 'flowhmr'), required=True)
    parser.add_argument('--inspect', action='store_true', help='Record actual GUI defaults without inference')
    args = parser.parse_args()
    root = args.root.resolve()
    os.chdir(root)
    sys.path[:0] = [str(Path(__file__).resolve().parent), str(root)]
    for name in ('PYTHONHOME', 'PYTHONPATH'):
        os.environ.pop(name, None)
    os.environ.update(RGB_MOCAP_MANAGED='1', RGB_MOCAP_DATA=str(root), HF_HOME=str(root/'cache/huggingface'),
                      TORCH_HOME=str(root/'cache/torch'), XDG_CACHE_HOME=str(root/'cache'), PYTHONDONTWRITEBYTECODE='1')
    from poc_headless import verify_install
    from tools.session_lock import app_lock
    from mocap.runtime_registry import activate
    from mocap.runtime_limits import configure_preview_cpu
    with app_lock(root):
        verify_install(root)
        activate(root, 'gui')
        configure_preview_cpu()
        sys.exit(baseline(args))
