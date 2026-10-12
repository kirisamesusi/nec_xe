"""CI boundary tests. All inputs are artificial; no upstream build/network/hardware."""
import hashlib
import importlib.util
import json
import os
import plistlib
from pathlib import Path
import stat
import struct
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
        (source/'tools').mkdir(); (source/'tests').mkdir()
        (source/'tools/test.py').write_text('python_tests=[]\n')
        for i in range(test_count):
            (source/'tests'/f'mock{i}.cpp').touch()
        sdk = self.root/'cpu-sdk'; sdk.mkdir()
        ci.write_json(source/'build/tests/results.json', {f'mock{i}':0 for i in range(test_count)})
        ci.write_json(source/'build/build-manifest.json', {})
        for name in ('WhateverXe', 'AppleIntelTGLCompat'):
            content = source/'build'/(name+'.kext')/'Contents'
            (content/'MacOS').mkdir(parents=True)
            (content/'Info.plist').write_bytes(plistlib.dumps({'CFBundleExecutable':name}))
            (content/'MacOS'/name).write_bytes(self.header())
        def output(cmd, **kwargs):
            if cmd == ['xcodebuild','-version']: return 'Xcode 16.4\nBuild version 16F6'
            if '--show-sdk-version' in cmd: return '15.5'
            if '--show-sdk-path' in cmd: return str(sdk)
            if '--find' in cmd: return '/mock/compiler/clang++'
            return 'mock tool output'
        def prepare(lock, work, out, downloads):
            return {'WhateverXe':source,'MacKernelSDK':source,'Lilu':source}, {n:{} for n in ('WhateverXe','MacKernelSDK','Lilu')}
        def capture(cmd, cwd, out, name):
            (out/name).write_text('synthetic diagnostic\n')
            self.assertEqual(os.environ.get('SDKROOT'), 'inherited-sdk' if name == 'build.log' else str(sdk))
            return build_rc if name == 'build.log' else tests_rc
        with mock.patch.object(ci.platform, 'system', return_value='Darwin'), \
             mock.patch.object(ci.platform, 'machine', return_value='x86_64'), \
             mock.patch.object(ci.platform, 'python_version', return_value='3.12.10'), \
             mock.patch.object(ci.subprocess, 'check_output', side_effect=output), \
             mock.patch.object(ci, 'prepare', side_effect=prepare), \
             mock.patch.object(ci, 'capture', side_effect=capture), \
             mock.patch.object(ci, 'inspect_kexts', side_effect=ValueError('bad architecture') if invalid_product else None, return_value={}), \
             mock.patch.object(ci, 'package_kexts') as package, \
             mock.patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': '', 'SDKROOT': 'inherited-sdk'}):
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
        self.assertTrue((self.root/'out/build-diagnostics/WhateverXe.kext/Contents/MacOS/WhateverXe').is_file())
        self.assertIn('UNVERIFIED', (self.root/'out/build-diagnostics/README.txt').read_text())

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

    def header(self, cpu=0x01000007, filetype=11, magic=0xFEEDFACF):
        return struct.pack('<8I', magic, cpu, 3, filetype, 0, 0, 0, 0)

    def test_numeric_header_rejects_wrong_arch_type_magic_and_truncation(self):
        p = self.root/'binary'
        for data in (self.header(cpu=0x0100000C), self.header(filetype=6),
                     self.header(magic=0xCAFEBABE), self.header()[:20]):
            p.write_bytes(data)
            with self.assertRaises(ValueError): ci.macho_kext_header(p)
        p.write_bytes(self.header())
        self.assertEqual(ci.macho_kext_header(p)['filetype'], 11)

    def mock_bundles(self, bad_header=False):
        build = self.root/'build'; build.mkdir()
        manifest = {}
        for name in ('WhateverXe', 'AppleIntelTGLCompat'):
            content = build/(name+'.kext')/'Contents'; (content/'MacOS').mkdir(parents=True)
            exe = content/'MacOS'/name
            exe.write_bytes(self.header(filetype=6 if bad_header else 11))
            (content/'Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier':'org.b00t0x.'+name,
                'CFBundleExecutable':name,'CFBundleVersion':'0.0.0'}))
            manifest[name] = {'sha256':ci.sha(exe),'configuration':'RELEASE'}
        ci.write_json(build/'build-manifest.json', manifest)
        return build

    def test_apple_otool_display_spelling_no_longer_false_rejects(self):
        build = self.mock_bundles()
        def output(cmd, **kwargs): return 'x86_64\n' if 'lipo' in cmd else 'MH_MAGIC_64 X86_64 KEXTBUNDLE\n'
        with mock.patch.object(ci.subprocess, 'check_output', side_effect=output):
            products = ci.inspect_kexts(build)
        self.assertEqual(products['WhateverXe']['numeric_header']['filetype'],11)

    def test_display_text_cannot_make_invalid_binary_pass(self):
        build = self.mock_bundles(bad_header=True)
        def output(cmd, **kwargs): return 'x86_64\n' if 'lipo' in cmd else 'KEXTBUNDLE KEXT_BUNDLE\n'
        with mock.patch.object(ci.subprocess, 'check_output', side_effect=output):
            with self.assertRaises(ValueError): ci.inspect_kexts(build)

    def collision_source(self):
        source = self.root/'source'; (source/'tools').mkdir(parents=True)
        (source/'tests').mkdir(); (source/'build/tests').mkdir(parents=True)
        (source/'tools/test.py').write_text("python_tests=['xelp_forcewake_irq','tgl_start_order']\n")
        for name in ('single','xelp_forcewake_irq','tgl_start_order'):
            (source/'tests'/(name+'.cpp')).touch()
        ci.write_json(source/'build/tests/results.json', {n:0 for n in ('single','xelp_forcewake_irq','tgl_start_order')})
        output = self.root/'out'; output.mkdir()
        return source, output

    def test_duplicate_names_get_independent_typed_results(self):
        source, output = self.collision_source()
        original = (source/'build/tests/results.json').read_bytes()
        with mock.patch.object(ci, 'capture', return_value=0) as capture:
            report = ci.qualify_cpu_results(source, output, 0, 5)
        self.assertEqual((report['status'],report['original_json_items'],len(report['results'])), ('PASS',3,5))
        self.assertEqual(report['independent_duplicate_rechecks'],4)
        self.assertEqual(capture.call_count,6)
        self.assertEqual((source/'build/tests/results.json').read_bytes(),original)
        self.assertIn('cpp:xelp_forcewake_irq',report['results'])
        self.assertIn('python:xelp_forcewake_irq',report['results'])

    def test_python_pass_cannot_hide_same_named_cpp_failure(self):
        source, output = self.collision_source()
        def capture(cmd, cwd, out, name): return 9 if name == 'cpp_xelp_forcewake_irq.log' else 0
        with mock.patch.object(ci, 'capture', side_effect=capture):
            report = ci.qualify_cpu_results(source, output, 0, 5)
        self.assertEqual(report['status'],'FAIL')
        self.assertEqual(report['results']['cpp:xelp_forcewake_irq'],9)
        self.assertEqual(report['results']['python:xelp_forcewake_irq'],0)

    def test_incomplete_or_boolean_original_matrix_rejected(self):
        source, output = self.collision_source()
        for matrix in ({'single':0}, {'single':False,'xelp_forcewake_irq':0,'tgl_start_order':0}):
            ci.write_json(source/'build/tests/results.json',matrix)
            with self.assertRaises(ValueError): ci.qualify_cpu_results(source,output,0,5)


if __name__ == '__main__':
    unittest.main()
