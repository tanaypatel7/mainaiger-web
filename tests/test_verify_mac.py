"""scripts/verify_mac.py: the Mac verification report must run its probes through the Lab's own modules,
survive a crashing probe, label results honestly, keep the home folder out of the report, and drive the
self-improvement example with the real model path (decide.ask / models.chat) — here with a fake Ollama."""
import importlib.util
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.test_examples import make_fake_model
from tests.test_memory_import import chatgpt_rows

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('verify_mac', ROOT / 'scripts' / 'verify_mac.py')
vm = importlib.util.module_from_spec(spec); spec.loader.exec_module(vm)


def args(**over):
    base = {'docker': False, 'model': None, 'repo_loop': False, 'export': None, 'ssh_host': None, 'no_tests': True, 'loop_timeout': 300, 'state_dir': Path('/nonexistent'), 'out': Path('/nonexistent')}
    base.update(over)
    return SimpleNamespace(**base)


class ReportTests(unittest.TestCase):
    def test_crashing_probe_becomes_a_fail_and_home_is_redacted(self):
        r = vm.Report()
        def boom(rep): raise RuntimeError('probe exploded at ' + str(Path.home() / 'secret'))
        r.run('x', boom)
        r.add('y', 'ok', 'fine', path=str(Path.home() / 'Documents' / 'MainAIger Lab'))
        self.assertEqual([s['status'] for s in r.sections], ['fail', 'ok'])
        self.assertIn('probe exploded at ~/secret', r.sections[0]['detail']['traceback'])
        self.assertEqual(r.sections[1]['detail']['path'], '~/Documents/MainAIger Lab')
        data = r.to_json(args())
        self.assertEqual(data['summary'], {'ok': 1, 'fail': 1, 'skip': 0})
        md = r.to_markdown(args())
        self.assertIn('| y | ok | fine |', md); self.assertNotIn(str(Path.home()), md)

    def test_skips_are_labelled_with_the_flag_to_add(self):
        r = vm.Report()
        vm.probe_docker_session(r, args()); vm.probe_self_improve(r, args()); vm.probe_export(r, None); vm.probe_ssh(r, None); vm.probe_tests(r, True)
        self.assertEqual([s['status'] for s in r.sections], ['skip'] * 5)
        self.assertIn('--docker', r.sections[0]['summary']); self.assertIn('--model', r.sections[1]['summary']); self.assertIn('--export', r.sections[2]['summary'])

    def test_model_picking(self):
        with patch.object(vm.lab_models, 'running', return_value='0.40.12'), patch.object(vm.lab_models, 'installed_models', return_value=[{'name': 'llama3.2:3b'}, {'name': 'qwen2.5-coder:7b'}]):
            self.assertEqual(vm.pick_model(None, 'auto'), 'qwen2.5-coder:7b', 'auto prefers a coder model')
            self.assertEqual(vm.pick_model(None, 'llama3.2:3b'), 'llama3.2:3b'); self.assertIsNone(vm.pick_model(None, 'missing:1b'))
        with patch.object(vm.lab_models, 'running', return_value=None):
            self.assertIsNone(vm.pick_model(None, 'auto'))


class ExportProbeTests(unittest.TestCase):
    def test_real_parser_path_reports_counts_not_titles(self):
        with tempfile.TemporaryDirectory() as tmp:
            z = Path(tmp) / 'export.zip'
            with zipfile.ZipFile(z, 'w') as a: a.writestr('conversations.json', json.dumps(chatgpt_rows())); a.writestr('user.json', '{}')
            r = vm.Report(); vm.probe_export(r, z)
            s = r.sections[0]
            self.assertEqual(s['status'], 'ok'); self.assertIn('detected chatgpt: 2 conversation(s), 5 message(s)', s['summary'])
            self.assertNotIn('titles', s['detail']); self.assertNotIn('Fraud model talk', json.dumps(s))
            r = vm.Report(); vm.probe_export(r, Path(tmp) / 'nope.zip')
            self.assertEqual(r.sections[0]['status'], 'fail')


class LoopProbeTests(unittest.TestCase):
    def test_self_improve_runs_on_a_local_kernel_through_the_real_model_path(self):
        fake = make_fake_model(True)
        # verify_mac asks through decide.ask (schema) exactly like the server; route decide.ask to the fake by its cfg-shaped call.
        def fake_decide(model, question, context, schema, attempts=2):
            out = fake({'model': model, 'output': 'proposal' if 'source' in schema.get('properties', {}) else 'verdict', 'schema': schema}, question)
            return {'ok': out['ok'], 'decision': out['value'], 'raw': out['raw'], 'error': out['error'], 'attempts': 1, 'model': model, 'elapsed_ms': 1}
        with patch.object(vm.lab_decide, 'ask', side_effect=fake_decide), patch.object(vm.lab_models, 'running', return_value='0.40.12'), patch.object(vm.lab_models, 'installed_models', return_value=[{'name': 'qwen2.5-coder:7b'}]):
            r = vm.Report(); vm.probe_self_improve(r, args(model='auto'))
        loop, skip = r.sections
        self.assertEqual(loop['status'], 'ok', loop['summary'])
        self.assertEqual(loop['detail']['loop']['fires'], 1); self.assertEqual(loop['detail']['steps'], 12)
        self.assertEqual([c['output'] for c in loop['detail']['calls']], ['proposal', 'verdict', 'proposal', 'verdict'], 'every model call is logged for the report')
        self.assertTrue(all(c['ok'] for c in loop['detail']['calls'])); self.assertIn('last verdict: keep', loop['detail']['report'])
        self.assertIn('model qwen2.5-coder:7b', loop['summary']); self.assertEqual(skip['status'], 'skip'); self.assertIn('--repo-loop', skip['summary'])


if __name__ == '__main__':
    unittest.main()
