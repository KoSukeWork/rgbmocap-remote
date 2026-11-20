"""Exercise the installed CUDA runtime and bundled Blender in the caller's Windows session."""
import argparse
import csv
import ctypes
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def run(root, output, expect_service):
    session = ctypes.c_ulong()
    if not ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)):
        raise ctypes.WinError()
    sid_text = subprocess.check_output(['whoami.exe', '/user', '/fo', 'csv', '/nh'])
    account, sid = next(csv.reader(io.StringIO(sid_text.decode('mbcs', 'replace'))))
    output.mkdir(exist_ok=True, parents=True)
    report = dict(session_id=session.value, user=account, sid=sid, root=str(root), checks={})
    cuda = ("import json, torch; a=torch.arange(4, device='cuda'); "
            "torch.cuda.synchronize(); assert a.sum().item()==6; "
            "print(json.dumps(dict(device=torch.cuda.get_device_name(0),torch=torch.__version__,cuda=torch.version.cuda)))")
    blend = output / 'probe.blend'
    blender = root / 'runtime_character' / 'blender.exe'
    scene = ('import bpy, os; assert bpy.app.background; '
             'scene=bpy.context.scene; scene.frame_set(2); '
             'print(bpy.app.version_string); '
             'bpy.ops.wm.save_as_mainfile(filepath=os.environ["RMR_PROBE_BLEND"]); '
             'bpy.ops.wm.open_mainfile(filepath=os.environ["RMR_PROBE_BLEND"]); '
             'assert bpy.context.scene.frame_current == 2')
    env = dict(os.environ, RMR_PROBE_BLEND=str(blend), PYTHONDONTWRITEBYTECODE='1')
    for key in ('PYTHONHOME', 'PYTHONPATH'):
        env.pop(key, None)
    for name, cmd in [
        ('cuda', [root / 'runtime' / 'python.exe', '-I', '-X', 'utf8', '-B', '-c', cuda]),
        ('blender', [blender, '-b', '--factory-startup', '--disable-autoexec', '--python-exit-code', '1',
                     '--python-expr', scene]),
    ]:
        tick = time.monotonic()
        try:
            res = subprocess.run(cmd, cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 timeout=180, encoding='utf-8', errors='replace')
            report['checks'][name] = dict(exit_code=res.returncode, output=res.stdout[-5000:],
                passed=res.returncode == 0 and (name != 'blender' or blend.is_file() and blend.stat().st_size > 0))
        except (OSError, subprocess.TimeoutExpired) as exc:
            report['checks'][name] = dict(passed=False, error=str(exc))
        report['checks'][name]['elapsed_s'] = round(time.monotonic() - tick, 2)
    report['passed'] = (all(check['passed'] for check in report['checks'].values())
                        and (not expect_service or session.value == 0 and sid == 'S-1-5-19'))
    report['service_test'] = expect_service
    (output / 'session_report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--expect-service', action='store_true')
    args = p.parse_args()
    root = args.root.resolve()
    if args.expect_service:
        # The administrator-side test harness holds the upstream lock.
        print_report = run(root, args.out.resolve(), True)
    else:
        sys.path.insert(0, str(root))
        from tools.session_lock import app_lock
        with app_lock(root):
            print_report = run(root, args.out.resolve(), False)
    print(json.dumps(print_report, ensure_ascii=False, indent=2))
    sys.exit(0 if print_report['passed'] else 1)
