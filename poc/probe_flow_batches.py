"""Trace completed Fast SAM calls in a normal FlowHMR headless or Qt run.

Run from the upstream install with its shared interpreter. Pass 'headless' or
'gui' followed by the usual poc_headless.py or gui_baseline.py arguments.
Use a fresh --out so the feature stage does not reuse a previous result.
"""
import functools
import json
import os
from pathlib import Path
import runpy
import sys
from unittest.mock import patch


def trace_worker(boot, output, arguments):
    sys.path.insert(0, str(boot.parent))
    from mocap.flow_fast_sam import extract_batch

    attempts = []

    @functools.wraps(extract_batch)
    def observed(extractor, images, *args, **kwargs):
        call = {'frames': len(images), 'completed': False}
        attempts.append(call)
        result = extract_batch(extractor, images, *args, **kwargs)
        call['completed'] = True
        return result

    sys.argv = [str(boot), *arguments]
    try:
        with patch('mocap.flow_fast_sam.extract_batch', observed):
            runpy.run_path(str(boot), run_name='__main__')
    finally:
        if attempts:
            with (output / f'flow-batches-{os.getpid()}.json').open('x', encoding='utf-8') as stream:
                json.dump({'stage': arguments[0], 'calls': attempts}, stream, indent=2)


def launch(entry, arguments):
    root = Path(arguments[arguments.index('--root') + 1]).resolve()
    output = Path(arguments[arguments.index('--out') + 1]).resolve()
    if output.exists():
        raise SystemExit('Use a new output folder for a fresh feature extraction.')
    sys.path.insert(0, str(root))
    from mocap.paths import worker_command

    def traced_command(stage, *args):
        command = worker_command(stage, *args)
        if stage in ('flow_batch', 'flow_features'):
            boot = str(root / 'worker_boot.py')
            index = command.index(boot)
            command[index:index] = [str(Path(__file__).resolve()), 'worker', str(output)]
        return command

    script = Path(__file__).with_name(entry)
    sys.argv = [str(script), *arguments]
    with patch('mocap.paths.worker_command', traced_command):
        runpy.run_path(str(script), run_name='__main__')


if __name__ == '__main__':
    if sys.argv[1] == 'worker':
        trace_worker(Path(sys.argv[3]), Path(sys.argv[2]), sys.argv[4:])
    else:
        entries = {'gui': 'gui_baseline.py', 'headless': 'poc_headless.py'}
        launch(entries[sys.argv[1]], sys.argv[2:])
