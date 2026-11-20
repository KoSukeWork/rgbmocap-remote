"""RGBMoCap Remote, phase 0: headless proof of concept.

Runs the worker stage chain that the RGBMoCap GUI runs for one video and one
person (GVHMR, ExMR or FlowHMR with the GUI's default options), without the Qt
GUI, then exports an FBX. It can also compare the result with a model project
that the GUI produced from the same video, trim and options.

Run it with the RGBMoCap install's own shared runtime. Close the GUI first:
the script holds the same app lock as the GUI.

  cd /d <ROOT>
  runtime\\python.exe -I -X utf8 -u -B <path>\\poc_headless.py ^
      --root <ROOT> --video D:\\clips\\walk.mp4 --model gvhmr --out D:\\poc\\gvhmr

  rem Compare with the GUI result (its model project folder):
  runtime\\python.exe -I -X utf8 -u -B <path>\\poc_headless.py ^
      --root <ROOT> --compare-only --out D:\\poc\\gvhmr --gui-project <folder>

Pinned against upstream toyxyz/toyxyz_RGBMocap commit 4284413 (2026-11-16).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from pathlib import Path

PREFIX = '@@MOCAP@@'
RESULT_STAGES = {'export', 'flow_export', 'footmr', 'handflow', 'correct'}
PROJECT_STAGES = RESULT_STAGES | {'sam31_people'}


def parse_args():
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    p.add_argument('--root', type=Path, required=True, help='RGBMoCap install folder')
    p.add_argument('--out', type=Path, required=True, help='Output folder for this run')
    p.add_argument('--video', type=Path)
    p.add_argument('--model', choices=('gvhmr', 'exmr', 'flowhmr'), default='gvhmr')
    p.add_argument('--start', type=float, help='Source seconds; default is the start of the video')
    p.add_argument('--end', type=float, help='Source seconds; default is the end of the video')
    p.add_argument('--no-refine', action='store_true', help='GVHMR: HTD Refine off')
    p.add_argument('--no-footmr', action='store_true', help='GVHMR: FootMR off')
    p.add_argument('--no-handflow', action='store_true', help='GVHMR: HandFlow off')
    p.add_argument('--no-correction', action='store_true', help='Motion Correction off')
    p.add_argument('--compute-mode', choices=('auto', 'low_memory'), default='auto')
    p.add_argument('--gui-project', type=Path, help='GUI model project folder to compare with')
    p.add_argument('--compare-only', action='store_true', help='Compare the headless or GUI reference run in --out')
    p.add_argument('--tolerance', type=float, default=1e-4)
    return p.parse_args()


def configure_environment(root: Path, args) -> dict:
    """Mirror launch_source.py, app.py (runtime_limits) and gui.next_job."""
    env = dict(os.environ)
    for name in ('PYTHONHOME', 'PYTHONPATH', 'RGB_MOCAP_EFFECTIVE_MODE', 'RGB_MOCAP_STAGE'):
        env.pop(name, None)
    env.update({
        'RGB_MOCAP_MANAGED': '1',
        'RGB_MOCAP_DATA': str(root),
        'PYTHONDONTWRITEBYTECODE': '1',
        'HF_HOME': str(root / 'cache' / 'huggingface'),
        'TORCH_HOME': str(root / 'cache' / 'torch'),
        'XDG_CACHE_HOME': str(root / 'cache'),
        'OPENBLAS_NUM_THREADS': '1', 'OMP_NUM_THREADS': '1',
        'MKL_NUM_THREADS': '1', 'NUMEXPR_NUM_THREADS': '1',
        'PYTHONIOENCODING': 'utf-8', 'PYTHONUNBUFFERED': '1',
        'RGB_MOCAP_COMPUTE_MODE': args.compute_mode,
        'RGB_MOCAP_RESOURCE_SAFETY': '1',
        # GUI defaults (workspace_ui.py): FlowHMR all on except HaMeR, auto camera.
        'RGB_MOCAP_FLOW_INTERVAL': '0',
        'RGB_MOCAP_FLOW_FAST_SAM': '1', 'RGB_MOCAP_FLOW_BATCH4': '1',
        'RGB_MOCAP_FLOW_GPU_CROP': '1', 'RGB_MOCAP_FLOW_HAND_STRIDE4': '1',
        'RGB_MOCAP_FLOW_HAMER': '0',
        # GUI defaults: ExMR static camera, Smooth Motion on.
        'RGB_MOCAP_EXMR_OPTIONS': json.dumps({'static_camera': True, 'smoothing': 'global'}),
    })
    # mocap.paths reads RGB_MOCAP_DATA at import; runtime_registry reads
    # RGB_MOCAP_MANAGED at call time. Both must be set in this process too.
    os.environ.clear()
    os.environ.update(env)
    return env


def kill_tree(pid: int):
    subprocess.run(['taskkill.exe', '/PID', str(pid), '/T', '/F'], capture_output=True)


def verify_install(root: Path):
    setup = root / '.setup'
    if (setup / 'git-sync.pending').exists():
        raise SystemExit('Base setup is incomplete. Finish install.cmd before running the PoC.')
    # These are the same four contracts checked by the pinned launch_source.py.
    contracts = dict(lock_sha256='installer/runtime-lock.json', registry_sha256='mocap/runtime_registry.py',
                     support_sha256='installer/git-support-lock.json', assets_sha256='installer/external-assets.json')
    try:
        installed = json.loads((setup / 'git-runtime-state.json').read_text(encoding='utf-8'))
        for name, relative in contracts.items():
            if installed[name] != hashlib.sha256((root / relative).read_bytes()).hexdigest():
                raise ValueError(f'Installed runtime does not match {relative}')
    except (OSError, KeyError, ValueError) as exc:
        raise SystemExit(f'Run install.cmd before running the PoC: {exc}') from exc


def run_stage(root: Path, env: dict, stage: str, arguments: list[str], log):
    import psutil
    from gpu_usage import GpuSampler
    from mocap.processing_guard import ProcessingGuard
    from mocap.paths import worker_command
    command = worker_command(stage, *arguments)
    print(f'\n=== {stage} ===', flush=True)
    log.write(f'\n=== {stage} ===\n$ {json.dumps(command)}\n')
    started = time.monotonic()
    proc = subprocess.Popen(command, cwd=str(root), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    events: list[dict] = []
    last_progress = [0.0]
    last_output = [time.monotonic()]
    log_lock = threading.Lock()

    def pump(stream, is_stdout):
        for raw in iter(stream.readline, b''):
            line = raw.decode('utf-8', 'replace').replace('\r', '').rstrip('\n')
            last_output[0] = time.monotonic()
            with log_lock:
                log.write(('' if is_stdout else '[stderr] ') + line + '\n')
            if not is_stdout or not line.startswith(PREFIX):
                continue
            try:
                event = json.loads(line[len(PREFIX):])
            except ValueError:
                print('  invalid worker event: ' + line[:200], flush=True)
                continue
            if not isinstance(event, dict) or not isinstance(event.get('type'), str):
                continue
            kind = event['type']
            if kind in ('failed', 'blocked', 'complete', 'prepared', 'batch_item_done'):
                events.append(event)
            if kind == 'progress':
                now = time.monotonic()
                if now - last_progress[0] >= 1.0:
                    last_progress[0] = now
                    total = event.get('total') or 0
                    done = f"{event.get('current', 0)}/{total}" if total else '...'
                    print(f"  {done} {event.get('message', '')}", flush=True)
            elif kind in ('failed', 'blocked', 'info', 'resource_mode', 'complete', 'prepared'):
                print(f"  [{kind}] {event.get('message') or event.get('output') or event.get('project') or ''}", flush=True)

    threads = [threading.Thread(target=pump, args=(proc.stdout, True), daemon=True),
               threading.Thread(target=pump, args=(proc.stderr, False), daemon=True)]
    for t in threads:
        t.start()
    sampler = GpuSampler()
    try:
        try:
            guard = ProcessingGuard(proc.pid)
        except psutil.NoSuchProcess:
            # A cached worker can publish its result and exit before the first sample.
            if proc.poll() is None:
                raise
            guard = None
        while guard is not None and proc.poll() is None:
            guard.last_output = last_output[0]
            sampler.sample(guard.process)
            issue = guard.check()
            if issue:
                events.append(dict(type='failed', message=issue))
                kill_tree(proc.pid)
                break
            try:
                proc.wait(timeout=.5)
            except subprocess.TimeoutExpired:
                pass
        code = proc.wait()
    finally:
        if proc.poll() is None:
            kill_tree(proc.pid)
            proc.wait()
        for t in threads:
            t.join()
        telemetry = sampler.finish()
    log.flush()
    return code, events, time.monotonic() - started, telemetry


def process(args, root: Path, env: dict):
    sys.path.insert(0, str(root))
    from mocap.motion_tools import MOTION_TOOLS, result_kind
    from mocap.queue_groups import BATCH_STAGES, join_single_pipeline, remove_request, write_request
    from mocap.storage import atomic_json

    tool = MOTION_TOOLS[args.model]
    # Same switches as gui.run_full with the GUI's default check boxes.
    refine = bool(tool.refinement) and not args.no_refine
    foot = args.model == 'gvhmr' and not args.no_footmr
    hand = args.model == 'gvhmr' and not args.no_handflow
    correction = not args.no_correction
    stages = tool.stages(refine, foot, correction, handflow=hand)
    result = result_kind(refine, foot, correction, handflow=hand)

    video = args.video.resolve()
    # A newly opened GUI source uses zero-based clip edges from VideoPreview.
    start = 0.0 if args.start is None else args.start
    end = args.end
    if end is None:
        import cv2
        capture = cv2.VideoCapture(str(video), cv2.CAP_FFMPEG, [cv2.CAP_PROP_N_THREADS, 2,
            cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 1500, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 1500])
        try:
            frames, fps = int(capture.get(cv2.CAP_PROP_FRAME_COUNT)), capture.get(cv2.CAP_PROP_FPS)
            if not capture.isOpened() or frames < 1 or not math.isfinite(fps) or fps <= 0:
                raise ValueError(f'Cannot read the source frame range: {video}')
            end = frames / fps
        finally:
            capture.release()
    workspace = (args.out / 'workspace').resolve()
    workspace_manifest = workspace / 'project.json'
    if workspace_manifest.exists():
        previous = json.loads(workspace_manifest.read_text(encoding='utf-8'))
        source = previous.get('source', {}).get('path')
        if not source or Path(source).resolve() != video or previous.get('active_model') != args.model:
            raise SystemExit('This output already belongs to a different source or model. Use a new output folder.')
        existing_take = workspace / args.model / 'project.json'
        if existing_take.exists():
            from mocap.storage import sha256
            if json.loads(existing_take.read_text(encoding='utf-8')).get('source_sha256') != sha256(video):
                raise SystemExit('The video changed after this workspace was created. Use a new output folder.')
    prepare_args = ['--video', str(video), '--start', str(start), '--end', str(end)]
    prepare_args += ['--project', str(workspace), '--motion-tool', args.model, '--result', result]
    if result.startswith('corrected_'):
        # GUI defaults: auto floor (no --floor), normal, flat, foot lock on, hand lock off.
        prepare_args += ['--correction-preset', 'normal', '--correction-mode', 'flat',
                         '--foot-lock', '1', '--hand-lock', '0', '--pelvis-offset', '0.0']

    jobs = [('prepare', prepare_args)]
    for stage in stages:
        jobs.append((stage, ['--result', result] if stage in RESULT_STAGES else None))

    print(f'model={args.model} result={result}\nstages={stages}', flush=True)
    report = {'model': args.model, 'result': result, 'video': str(video), 'start': start,
              'end': end, 'compute_mode': args.compute_mode, 'stages': []}
    project: Path | None = None
    args.out.mkdir(parents=True, exist_ok=True)
    def checked(stage, arguments, log):
        report_path = args.out / 'poc_report.json'
        atomic_json(report_path, dict(report, status='running', current_stage=stage))
        try:
            code, events, elapsed, telemetry = run_stage(root, env, stage, arguments, log)
        except Exception as exc:
            atomic_json(report_path, dict(report, status='failed', current_stage=stage, error=str(exc)))
            raise
        errors = [event.get('message') or event['type'] for event in events if event['type'] in ('blocked', 'failed')]
        blockers = [event for event in events if event['type'] == 'blocked']
        report['stages'].append(dict(stage=stage, exit_code=code, elapsed_s=round(elapsed, 2),
            errors=errors, blockers=blockers, gpu=telemetry, complete=[event for event in events if event['type'] == 'complete'],
            batch_items=[event for event in events if event['type'] == 'batch_item_done']))
        if code or errors:
            status = 'blocked' if blockers and not any(e['type'] == 'failed' for e in events) else 'failed'
            atomic_json(report_path, dict(report, status=status, current_stage=stage))
            raise SystemExit(f'Stage {stage} {status} (exit {code}): {errors}. See {args.out / "worker.log"}')
        return events

    with open(args.out / 'worker.log', 'w', encoding='utf-8') as log:
        while jobs:
            if project is not None:
                jobs = join_single_pipeline(jobs, project)
            stage, arguments = jobs.pop(0)
            if arguments is None:
                arguments = ['--project', str(project)]
            elif stage in PROJECT_STAGES and '--project' not in arguments:
                arguments = ['--project', str(project), *arguments]
            job_project = arguments[arguments.index('--project') + 1] if '--project' in arguments else None
            request = None
            if stage in BATCH_STAGES:
                request = write_request(stage, arguments)
                arguments = ['--request', str(request), '--project', str(job_project)]
            try:
                events = checked(stage, arguments, log)
            finally:
                remove_request(request)
            for event in events:
                if event['type'] == 'prepared':
                    project = Path(event['project'])
            if stage == 'prepare' and (project is None or not (project / 'project.json').is_file()):
                raise ValueError('prepare exited without publishing a project')
            if project is not None:
                report['project'] = str(project)

        mixamo = project / f'{result}_mixamo.json'
        if not mixamo.exists():
            checked('rig', ['--project', str(project), '--result', result], log)
        record = json.loads(mixamo.read_text(encoding='utf-8'))
        target = (args.out / f'{args.model}_{result}.fbx').resolve()
        row = dict(project=str(project), kind=result, source=str(project / record['bvh_file']), target=str(target),
                   rig_name='Person 1', prepend_tpose=False, export_face=False, face_format='txt',
                   bvh_profile='generic', place_at_origin=False, symmetric_rest=False,
                   origin_group=str(project.resolve()), animate_camera=True, background_refine=False,
                   camera_translation_scale=1.0)
        request = project / ('.fbx-export-' + uuid.uuid4().hex + '.json')
        atomic_json(request, [row])
        try:
            checked('save_fbx', ['--project', str(project), '--output', str(request)], log)
        finally:
            request.unlink(missing_ok=True)
        if not target.is_file() or not target.stat().st_size:
            raise SystemExit('save_fbx failed')

    report.update(status='ok', project=str(project), fbx=str(target), files=fingerprint(project, result))
    (args.out / 'poc_report.json').write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    total = sum(s['elapsed_s'] for s in report['stages'])
    print(f'\nDone in {total:.0f} s.\nProject: {project}\nFBX:     {target}', flush=True)
    return report


def fingerprint(project: Path, result: str) -> dict:
    files = {}
    for path in sorted(project.glob(f'{result}*')):
        if path.is_file():
            digest = hashlib.sha256()
            with open(path, 'rb') as stream:
                for chunk in iter(lambda: stream.read(1 << 20), b''):
                    digest.update(chunk)
            files[path.name] = {'bytes': path.stat().st_size, 'sha256': digest.hexdigest()}
    return files


def referenced_arrays(project: Path, result: str) -> dict[str, Path]:
    """Require the published artifacts, even when both runs lost the same file."""
    manifest = project / f'{result}_mixamo.json'
    if not manifest.is_file():
        raise ValueError(f'{manifest} not found. In the GUI, run Prepare Preview for this result, then compare again.')
    record = json.loads(manifest.read_text(encoding='utf-8'))

    keys = ['frames_file', 'joints_file', 'reference_joints_file', 'asset_file', 'bvh_file']
    if record.get('representation') == 'smplh':
        keys.extend(['raw_file', 'raw_asset'])
    found = {'export:joints.npz': project / f'{result}_joints.npz'}
    expected_suffix = dict(frames_file='.npy', joints_file='.npy', reference_joints_file='.npy',
                           asset_file='.npz', bvh_file='.bvh', raw_file='.npy', raw_asset='.npz')
    for key in keys:
        filename = record.get(key)
        if (not isinstance(filename, str) or Path(filename).name != filename
                or Path(filename).suffix != expected_suffix[key]):
            raise ValueError(f'{manifest}: missing or invalid {key}')
        found['mixamo:' + key] = project / filename
    return found


def bvh_motion(path: Path):
    import numpy as np
    hierarchy, offsets = [], []
    channels = 0
    with path.open(encoding='utf-8') as stream:
        for line in stream:
            fields = line.split()
            if fields == ['MOTION']:
                break
            if fields[:1] == ['OFFSET']:
                offsets.append([float(x) for x in fields[1:]])
            else:
                hierarchy.append(' '.join(fields))
                if fields[:1] == ['CHANNELS']:
                    channels += int(fields[1])
        frame_line, time_line = stream.readline().split(), stream.readline().split()
        if frame_line[:1] != ['Frames:'] or time_line[:2] != ['Frame', 'Time:']:
            raise ValueError(f'Invalid BVH frame header in {path}')
        frames, step = int(frame_line[1]), float(time_line[2])
        motion = np.loadtxt(stream, ndmin=2)
    if frames < 1 or channels < 1 or motion.shape != (frames, channels) or step <= 0:
        raise ValueError(f'Invalid BVH frame count, channel count or frame time in {path}')
    return dict(hierarchy=np.asarray(hierarchy), offsets=np.asarray(offsets),
                frame_time=np.asarray(step), motion=motion)


def compare(args):
    import numpy as np
    reference = args.out / 'poc_report.json'
    reference_label = 'headless'
    if not reference.is_file():
        reference = args.out / 'gui_report.json'
        reference_label = 'gui_reference'
    if not reference.is_file():
        raise SystemExit('Comparison needs a completed headless or GUI reference report in --out.')
    report = json.loads(reference.read_text(encoding='utf-8'))
    if report.get('status') != 'ok' or not math.isfinite(args.tolerance) or args.tolerance < 0:
        raise SystemExit('Comparison needs a completed run and a finite nonnegative tolerance.')
    result = report['result']
    ours, theirs = Path(report['project']).resolve(), args.gui_project.resolve()
    rows = []
    worst = 0.0

    def check(label, x, y):
        nonlocal worst
        if x.shape != y.shape or x.dtype != y.dtype:
            raise ValueError(f'{label}: {x.shape}/{x.dtype} vs {y.shape}/{y.dtype}')
        diff = 0.0
        if np.issubdtype(x.dtype, np.floating):
            for start in range(0, x.size, 262144):
                a = x.flat[start:start + 262144].astype(np.float64)
                b = y.flat[start:start + 262144].astype(np.float64)
                if not (np.isfinite(a).all() and np.isfinite(b).all()):
                    raise ValueError(f'{label}: non-finite values')
                diff = max(diff, float(np.max(np.abs(a - b))))
        elif not np.array_equal(x, y):
            raise ValueError(f'{label}: discrete values differ')
        worst = max(worst, diff)
        rows.append(dict(item=label, max_abs_diff=diff, passed=diff <= args.tolerance))

    try:
        if ours.samefile(theirs):
            raise ValueError('The two runs must use separate project folders.')
        states = [json.loads((p / 'project.json').read_text(encoding='utf-8')) for p in (ours, theirs)]
        for key in ('source_sha256', 'start', 'end', 'frames', 'fps'):
            if states[0][key] != states[1][key]:
                raise ValueError(f'Project inputs differ: {key}')
        for state in states:
            if state['pipeline']['model'] != report['model']:
                raise ValueError('Model does not match the run report.')
        for key in ('flow_options', 'exmr_options', 'correction'):
            if states[0].get(key) != states[1].get(key):
                raise ValueError(f'Run options differ: {key}')
        rigs = [json.loads((p / f'{result}_mixamo.json').read_text(encoding='utf-8')) for p in (ours, theirs)]
        for key in ('names', 'representation', 'kind', 'frames', 'fps'):
            if rigs[0][key] != rigs[1][key]:
                raise ValueError(f'Rig manifests differ: {key}')
        if (rigs[0]['kind'] != result or rigs[0]['frames'] != states[0]['frames']
                or rigs[0]['fps'] != states[0]['fps'] or not rigs[0]['names'] or rigs[0]['frames'] < 1):
            raise ValueError('Rig manifest does not describe the selected motion.')
        left, right = (referenced_arrays(p, result) for p in (ours, theirs))
        if left.keys() != right.keys():
            raise ValueError('Rig manifests declare different file sets.')
        for key in sorted(left):
            a, b = left[key], right[key]
            if a.suffix != b.suffix:
                raise ValueError(f'{key}: file types differ')
            if a.suffix == '.bvh':
                av, bv = bvh_motion(a), bvh_motion(b)
                for name in av:
                    check(f'{key}:{name}', av[name], bv[name])
            elif a.suffix == '.npz':
                with np.load(a, allow_pickle=False) as za, np.load(b, allow_pickle=False) as zb:
                    if not za.files or set(za.files) != set(zb.files):
                        raise ValueError(f'{key}: empty archive or different member names')
                    if key == 'export:joints.npz' and ('joints' not in za or not za['joints'].size):
                        raise ValueError(f'{key}: no joint positions')
                    for name in sorted(za.files):
                        check(f'{key}:{name}', za[name], zb[name])
            else:
                x, y = (np.load(p, mmap_mode='r', allow_pickle=False) for p in (a, b))
                if not x.size or not x.shape or x.shape[0] != rigs[0]['frames']:
                    raise ValueError(f'{key}: wrong number of motion frames')
                check(key, x, y)
    except (OSError, KeyError, ValueError, EOFError, zipfile.BadZipFile) as exc:
        rows.append(dict(item='validation', passed=False, error=str(exc)))
    passed = bool(rows) and all(row['passed'] for row in rows)
    verdict = 'PASS' if passed else 'FAIL'
    for row in rows:
        print(row)
    print(f'\n{verdict} (tolerance {args.tolerance:g})')
    comparison = {'verdict': verdict, 'worst': worst, 'tolerance': args.tolerance,
                  reference_label: str(ours), 'gui': str(theirs), 'rows': rows}
    (args.out / 'compare_report.json').write_text(json.dumps(comparison,
        indent=2, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    return passed


def main():
    args = parse_args()
    root = args.root.resolve()
    if not (root / 'worker_boot.py').is_file():
        raise SystemExit(f'{root} is not an RGBMoCap install folder')
    args.out = args.out.resolve()
    if args.compare_only:
        if not args.gui_project:
            raise SystemExit('--compare-only needs --gui-project')
        raise SystemExit(0 if compare(args) else 1)
    if not args.video:
        raise SystemExit('--video is required')
    env = configure_environment(root, args)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.path.insert(0, str(root))
    from tools.session_lock import app_lock
    with app_lock(root):
        verify_install(root)
        try:
            process(args, root, env)
        except (Exception, KeyboardInterrupt, SystemExit) as exc:
            path = args.out / 'poc_report.json'
            if path.is_file():
                current = json.loads(path.read_text(encoding='utf-8'))
                if current.get('status') == 'running':
                    from mocap.storage import atomic_json
                    atomic_json(path, dict(current, status='failed', error=str(exc)))
            raise
    if args.gui_project:
        raise SystemExit(0 if compare(args) else 1)


if __name__ == '__main__':
    main()
