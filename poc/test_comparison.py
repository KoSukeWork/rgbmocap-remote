"""Regression checks for the phase-0 verdict. No model files are used."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from poc_headless import compare


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.ours, self.gui = root / 'ours', root / 'gui'
        self.args = SimpleNamespace(out=root, gui_project=self.gui, tolerance=.0001)
        for project in (self.ours, self.gui):
            project.mkdir()
            state = dict(source_sha256='fixture', start=0, end=1, frames=1, fps=1, pipeline={'model': 'gvhmr'})
            (project / 'project.json').write_text(json.dumps(state))
            manifest = dict(kind='initial', representation='smplx', names=['root'], frames=1, fps=1,
                            frames_file='pose.npy', joints_file='joints.npy', reference_joints_file='ref.npy',
                            bvh_file='motion.bvh', asset_file='asset.npz')
            (project / 'initial_mixamo.json').write_text(json.dumps(manifest))
            for file in ('pose.npy', 'ref.npy', 'joints.npy'):
                np.save(project / file, np.zeros((1, 1, 3)))
            np.savez(project / 'asset.npz', parents=np.array([-1]))
            np.savez(project / 'initial_joints.npz', joints=np.zeros((1, 1, 3)))
            (project / 'motion.bvh').write_text(
                'HIERARCHY\nROOT root\n{\n OFFSET 0 0 0\n CHANNELS 3 Xrotation Yrotation Zrotation\n'
                '}\nMOTION\nFrames: 1\nFrame Time: 1\n0 0 0\n')
        (root / 'poc_report.json').write_text(json.dumps(dict(
            status='ok', project=str(self.ours), model='gvhmr', result='initial')))

    def check_verdict(self, expected):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(compare(self.args), expected)
        saved = json.loads((self.args.out / 'compare_report.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['verdict'], 'PASS' if expected else 'FAIL')

    def test_identical_independent_outputs(self):
        self.check_verdict(True)

    def test_numeric_tolerance(self):
        for value, expected in [(0.00001, True), (0.001, False), (float('nan'), False)]:
            with self.subTest(value=value):
                np.save(self.gui / 'pose.npy', np.full((1, 1, 3), value))
                self.check_verdict(expected)

    def test_missing_from_both(self):
        for project in (self.ours, self.gui):
            (project / 'ref.npy').unlink()
        self.check_verdict(False)

    def test_npz_missing_member(self):
        np.savez(self.ours / 'asset.npz', parents=np.array([-1]), faces=np.array([1]))
        self.check_verdict(False)

    def test_bvh_channel_order(self):
        path = self.gui / 'motion.bvh'
        path.write_text(path.read_text().replace('Xrotation Yrotation', 'Yrotation Xrotation'))
        self.check_verdict(False)

    def test_cannot_compare_project_to_itself(self):
        self.args.gui_project = self.ours
        self.check_verdict(False)


if __name__ == '__main__':
    unittest.main()
