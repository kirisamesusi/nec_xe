"""CI boundary tests. All inputs are artificial; no upstream build/network/hardware."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock
import zipfile

SPEC = importlib.util.spec_from_file_location('nec_ci', Path(__file__).resolve().parents[1]/'.ci/native_build.py')
ci = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci)


class Boundaries(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def archive(self, entries):
        p = self.root/'input.zip'
        with zipfile.ZipFile(p, 'w') as z:
            for name, content in entries:
                z.writestr(name, content)
        return p

    def test_hash_and_length_corruption_rejected(self):
        p = self.archive([('source/a', b'original')])
        dep = {'bytes': p.stat().st_size, 'sha256': ci.sha(p), 'archive': p.name}
        ci.verify_archive(p, dep)
        b = bytearray(p.read_bytes()); b[40] ^= 1; p.write_bytes(b)
        with self.assertRaises(ValueError): ci.verify_archive(p, dep)
        p.write_bytes(bytes(b)+b'x')
        with self.assertRaises(ValueError): ci.verify_archive(p, dep)

    def test_archive_traversal_rejected_before_writes(self):
        p = self.archive([('source/safe', b'x'), ('source/../escape', b'y')])
        with self.assertRaises(ValueError): ci.extract_archive(p, self.root/'out', 'source')
        self.assertFalse((self.root/'out').exists())
        self.assertFalse((self.root/'escape').exists())

    def test_symlink_rejected(self):
        entry = zipfile.ZipInfo('source/link')
        entry.create_system = 3
        entry.external_attr = (stat.S_IFLNK | 0o777) << 16
        p = self.archive([(entry, b'../../outside')])
        with self.assertRaises(ValueError): ci.extract_archive(p, self.root/'out', 'source')

    def test_case_collision_rejected(self):
        p = self.archive([('source/A', b'x'), ('source/a', b'y')])
        with self.assertRaises(ValueError): ci.extract_archive(p, self.root/'out', 'source')

    def test_non_mac_stops_before_network_and_preserves_failure(self):
        with mock.patch.object(ci.platform, 'system', return_value='Windows'), \
             mock.patch.object(ci, 'prepare') as prepare, \
             mock.patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': ''}):
            rc = ci.main(['--work', str(self.root/'work'), '--output', str(self.root/'out')])
        self.assertEqual(rc, 1)
        prepare.assert_not_called()
        result = json.loads((self.root/'out/result.json').read_text())
        self.assertEqual((result['status'], result['build'], result['tests']), ('BLOCKED','NOT_TESTED','NOT_TESTED'))
        self.assertTrue((self.root/'out/SHA256SUMS').is_file())
        self.assertFalse((self.root/'out/kexts.zip').exists())

    def test_existing_output_is_not_overwritten(self):
        out = self.root/'out'; out.mkdir(); (out/'keep').write_text('sentinel')
        with self.assertRaises(SystemExit):
            ci.main(['--work', str(self.root/'work'), '--output', str(out), '--prepare-only'])
        self.assertEqual((out/'keep').read_text(), 'sentinel')
        self.assertFalse((self.root/'work').exists())

    def test_log_redaction_and_hash_inventory(self):
        out = self.root/'out'; out.mkdir()
        (out/'build.log').write_text('private-source/path.cpp: error\n')
        with mock.patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': ''}):
            ci.finalize(out, {'status':'FAIL'}, [('private-source','/ci-work')])
        self.assertNotIn('private-source', (out/'build.log').read_text())
        manifest = json.loads((out/'artifact-manifest.json').read_text())
        self.assertEqual(manifest['build.log'], ci.sha(out/'build.log'))
        for line in (out/'SHA256SUMS').read_text().splitlines():
            h, n = line.split('  ', 1)
            self.assertEqual(h, ci.sha(out/n))

    def fake_native(self, build_rc, tests_rc, test_count=56, invalid_product=False):
        source = self.root/'source'; (source/'build/tests').mkdir(parents=True)
        ci.write_json(source/'build/tests/results.json', {f'mock{i}':0 for i in range(test_count)})
        ci.write_json(source/'build/build-manifest.json', {})
        def output(cmd, **kwargs):
            if cmd == ['xcodebuild','-version']: return 'Xcode 16.4\nBuild version 16F6'
            if '--show-sdk-version' in cmd: return '15.5'
            if '--find' in cmd: return '/mock/compiler/clang++'
            return 'mock tool output'
        def prepare(lock, work, out, downloads):
            return {'WhateverXe':source,'MacKernelSDK':source,'Lilu':source}, {n:{} for n in ('WhateverXe','MacKernelSDK','Lilu')}
        def capture(cmd, cwd, out, name):
            (out/name).write_text('synthetic diagnostic\n')
            return build_rc if name == 'build.log' else tests_rc
        with mock.patch.object(ci.platform, 'system', return_value='Darwin'), \
             mock.patch.object(ci.platform, 'machine', return_value='x86_64'), \
             mock.patch.object(ci.platform, 'python_version', return_value='3.12.10'), \
             mock.patch.object(ci.subprocess, 'check_output', side_effect=output), \
             mock.patch.object(ci, 'prepare', side_effect=prepare), \
             mock.patch.object(ci, 'capture', side_effect=capture), \
             mock.patch.object(ci, 'inspect_kexts', side_effect=ValueError('bad architecture') if invalid_product else None, return_value={}), \
             mock.patch.object(ci, 'package_kexts') as package, \
             mock.patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': ''}):
            rc = ci.main(['--work', str(self.root/'work'), '--output', str(self.root/'out')])
        return rc, json.loads((self.root/'out/result.json').read_text()), package

    def test_build_failure_still_runs_tests_no_bundle(self):
        rc, result, package = self.fake_native(1,0)
        self.assertEqual((rc,result['build'],result['tests']), (1,'FAIL','PASS'))
        package.assert_not_called()
        self.assertTrue((self.root/'out/tests/results.json').exists())

    def test_tests_failure_keeps_build_diagnostics_no_bundle(self):
        rc, result, package = self.fake_native(0,1)
        self.assertEqual((rc,result['build'],result['tests']), (1,'PASS','FAIL'))
        package.assert_not_called()

    def test_incomplete_test_matrix_not_pass(self):
        rc, result, package = self.fake_native(0,0,test_count=55)
        self.assertEqual(rc,1)
        self.assertEqual(result['tests'],'FAIL')
        package.assert_not_called()

    def test_invalid_product_not_published(self):
        rc, result, package = self.fake_native(0,0,invalid_product=True)
        self.assertEqual((rc,result['artifact_validation']), (1,'FAIL'))
        package.assert_not_called()

    def test_success_gate_requires_build_tests_and_inspection(self):
        rc, result, package = self.fake_native(0,0)
        self.assertEqual((rc,result['status'],result['artifact_validation']), (0,'PASS','PASS'))
        package.assert_called_once()
        self.assertEqual(result['NEC_internal_panel'],'NOT_TESTED')


if __name__ == '__main__':
    unittest.main()
