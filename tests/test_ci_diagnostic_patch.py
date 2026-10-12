"""Artificial patch-boundary inputs; no downloads, compiler, kernel or hardware."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('diag', ROOT/'.ci/diagnostic_patch.py')
diag = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(diag)
SPEC_CI = importlib.util.spec_from_file_location('native_ci', ROOT/'.ci/native_build.py')
ci = importlib.util.module_from_spec(SPEC_CI)
SPEC_CI.loader.exec_module(ci)


class DiagnosticBoundaries(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root/'source'
        target = self.source/diag.TARGET
        target.parent.mkdir(parents=True)
        target.write_bytes(b'guard=exact\n')
        (self.source/'untouched').write_bytes(b'unchanged')
        self.before = diag.manifest(self.source)
        self.patch = self.root/'fixed.diff'
        self.patch.write_bytes(b'--- a/WhateverXe/kern_xe.cpp\n+++ b/WhateverXe/kern_xe.cpp\n'
                               b'@@ -1 +1,2 @@\n+observer=metadata\n guard=exact\n')
        patched = self.root/'patched'
        patched.write_bytes(b'observer=metadata\nguard=exact\n')
        self.output = self.root/'out'
        self.output.mkdir()
        self.stack = mock.patch.multiple(diag, PATCH=self.patch, PATCH_BYTES=self.patch.stat().st_size,
            PATCH_SHA256=diag.sha(self.patch), BASE_SHA256=diag.sha(target), PATCHED_SHA256=diag.sha(patched))
        self.stack.start()
        self.addCleanup(self.stack.stop)

    def apply(self, pin=diag.PIN):
        return diag.apply_fixed(self.source, self.before, pin, self.output, self.root/'reverse')

    def test_exact_apply_reverse_and_three_manifests(self):
        after, record = self.apply()
        self.assertEqual({n for n in after if after[n] != self.before[n]}, {diag.TARGET})
        self.assertEqual(diag.manifest(self.root/'reverse'), self.before)
        self.assertEqual(record['apply_reverse'], 'PASS')
        for name in ('base-source-manifest', 'patch-manifest', 'patched-source-manifest'):
            self.assertTrue((self.output/(name+'.json')).is_file())

    def test_wrong_pin_stops_before_apply(self):
        with self.assertRaises(ValueError): self.apply('0'*40)
        self.assertEqual(diag.manifest(self.source), self.before)

    def test_patch_byte_tamper_stops_before_apply(self):
        raw = self.patch.read_bytes()
        self.patch.write_bytes(raw.replace(b'metadata', b'Metadata'))
        with self.assertRaises(ValueError): self.apply()
        self.assertEqual(diag.manifest(self.source), self.before)

    def test_patch_length_tamper_stops_before_apply(self):
        self.patch.write_bytes(self.patch.read_bytes()+b'\n')
        with self.assertRaises(ValueError): self.apply()
        self.assertEqual(diag.manifest(self.source), self.before)

    def test_changed_base_or_extra_file_rejected(self):
        (self.source/'extra').write_bytes(b'x')
        with self.assertRaises(ValueError): self.apply()

    def test_unexpected_patched_content_rejected(self):
        original = diag.apply_command
        def apply(source, reverse=False, check=False):
            original(source, reverse, check)
            if not check and not reverse:
                (source/'untouched').write_bytes(b'bad')
        with mock.patch.object(diag, 'apply_command', side_effect=apply):
            with self.assertRaises(ValueError): self.apply()

    def test_build_output_allowed_source_tamper_and_extra_file_rejected(self):
        after, _ = self.apply()
        (self.source/'build').mkdir()
        (self.source/'build/result').write_bytes(b'generated')
        diag.verify_source_after_build(self.source, after)
        (self.source/'extra').write_bytes(b'not generated under build')
        with self.assertRaises(ValueError): diag.verify_source_after_build(self.source, after)
        (self.source/'extra').unlink()
        (self.source/diag.TARGET).write_bytes(b'changed')
        with self.assertRaises(ValueError): diag.verify_source_after_build(self.source, after)

    def test_postbuild_patch_tamper_rejected(self):
        after, _ = self.apply()
        self.patch.write_bytes(self.patch.read_bytes()+b'\n')
        with self.assertRaises(ValueError): diag.verify_source_after_build(self.source, after)

    def test_detached_diagnostic_zip_contains_variant_marker_and_licenses(self):
        build = self.root/'build'
        for name in ('WhateverXe','AppleIntelTGLCompat'):
            bundle = build/(name+'.kext')
            bundle.mkdir(parents=True)
            (bundle/'fake').write_bytes(b'artificial')
        licenses = self.output/'licenses'
        licenses.mkdir()
        for name in ('WhateverXe','MacKernelSDK','Lilu'):
            (licenses/(name+'-LICENSE.txt')).write_bytes(b'artificial license')
        marker = {'variant':diag.VARIANT,'sha256':diag.PATCH_SHA256}
        ci.write_json(self.output/'patch-manifest.json',marker)
        ci.package_kexts(build,self.output)
        with zipfile.ZipFile(self.output/'kexts.zip') as archive:
            self.assertEqual(json.loads(archive.read('diagnostic-variant.json')),marker)
            self.assertEqual(sum(n.startswith('licenses/') for n in archive.namelist()),3)

    def test_prepare_only_integrates_and_retains_base_manifest(self):
        def prepare(lock, work, out, downloads):
            ci.write_json(out/'input-manifests.json', {'WhateverXe':self.before})
            return {'WhateverXe':self.source}, {'WhateverXe':self.before}
        with mock.patch.object(ci, 'prepare', side_effect=prepare), \
             mock.patch.object(ci, 'diagnostic_module', return_value=diag), \
             mock.patch.object(ci, 'capture') as capture, \
             mock.patch.dict(os.environ, {'GITHUB_STEP_SUMMARY':''}):
            rc = ci.main(['--work',str(self.root/'work'), '--output',str(self.root/'ci-out'),
                          '--prepare-only', '--diagnostic-patch'])
        capture.assert_not_called()
        result = json.loads((self.root/'ci-out/result.json').read_text())
        self.assertEqual((rc,result['patch_integration'],result['build'],result['input_integrity']),
                         (0,'PASS','NOT_TESTED','PASS'))
        self.assertEqual(json.loads((self.root/'ci-out/input-manifests.json').read_text())['WhateverXe'],self.before)
        self.assertEqual(result['variant'], diag.VARIANT)

    def test_non_mac_diagnostic_stops_before_download(self):
        with mock.patch.object(ci.platform, 'system', return_value='Windows'), \
             mock.patch.object(ci, 'prepare') as prepare, \
             mock.patch.dict(os.environ, {'GITHUB_STEP_SUMMARY':''}):
            rc = ci.main(['--work',str(self.root/'work'), '--output',str(self.root/'ci-out'), '--diagnostic-patch'])
        prepare.assert_not_called()
        result = json.loads((self.root/'ci-out/result.json').read_text())
        self.assertEqual((rc,result['status'],result['patch_integration']), (1,'BLOCKED','NOT_TESTED'))

    def native_simulation(self, build_rc=0, tests_rc=0, corrupt=False, crash=False):
        (self.source/'tools').mkdir()
        (self.source/'tools/test.py').write_text('python_tests=[]\n')
        (self.source/'tests').mkdir()
        for i in range(56):
            (self.source/'tests'/f'fake{i}.cpp').touch()
        self.before = diag.manifest(self.source)
        sdk = self.root/'sdk'
        sdk.mkdir()
        lilu = self.root/'lilu'
        lilu.mkdir()
        def prepare(lock, work, out, downloads):
            return {'WhateverXe':self.source,'MacKernelSDK':sdk,'Lilu':lilu}, \
                   {'WhateverXe':self.before,'MacKernelSDK':{},'Lilu':{}}
        def tool(cmd, **kwargs):
            if cmd == ['xcodebuild','-version']: return 'Xcode 16.4\nBuild version 16F6'
            if '--show-sdk-version' in cmd: return '15.5'
            if '--show-sdk-path' in cmd: return str(sdk)
            if '--find' in cmd: return str(self.root/'fake-compiler/clang++')
            return 'artificial tool metadata'
        def capture(cmd, source, out, name):
            if crash: raise RuntimeError('artificial compiler exception')
            (out/name).write_text('artificial result\n')
            if name == 'tests.log':
                tests = source/'build/tests'
                tests.mkdir(parents=True)
                ci.write_json(tests/'results.json', {f'fake{i}':0 for i in range(56)})
                if corrupt: (source/diag.TARGET).write_bytes(b'corrupted')
                return tests_rc
            return build_rc
        def package(build, out):
            (out/'kexts.zip').write_bytes(b'artificial package')
        # Patch only metadata queries; leave subprocess.run for actual synthetic git apply.
        with mock.patch.object(ci.platform,'system',return_value='Darwin'), \
             mock.patch.object(ci.platform,'machine',return_value='x86_64'), \
             mock.patch.object(ci.platform,'python_version',return_value='3.12.10'), \
             mock.patch.object(ci.subprocess,'check_output',side_effect=tool), \
             mock.patch.object(ci,'prepare',side_effect=prepare), \
             mock.patch.object(ci,'diagnostic_module',return_value=diag), \
             mock.patch.object(ci,'capture',side_effect=capture), \
             mock.patch.object(ci,'preserve_build_diagnostics'), \
             mock.patch.object(ci,'inspect_kexts',return_value={}), \
             mock.patch.object(ci,'package_kexts',side_effect=package) as packaged, \
             mock.patch.dict(os.environ,{'GITHUB_STEP_SUMMARY':''}):
            rc = ci.main(['--work',str(self.root/'work'), '--output',str(self.root/'ci-out'), '--diagnostic-patch'])
        return rc, json.loads((self.root/'ci-out/result.json').read_text()), packaged

    def test_native_simulated_success_is_labeled_diagnostic(self):
        rc, result, package = self.native_simulation()
        self.assertEqual((rc,result['status'],result['qualified_cpu_tests']), (0,'PASS',56))
        package.assert_called_once()
        self.assertTrue((self.root/'ci-out/kexts-panel-a-diagnostic.zip').is_file())
        self.assertFalse((self.root/'ci-out/kexts.zip').exists())
        self.assertEqual(result['NEC_internal_panel'], 'NOT_TESTED')

    def test_native_simulated_build_failure_does_not_package(self):
        rc, result, package = self.native_simulation(build_rc=1)
        self.assertEqual((rc,result['build'],result['tests'],result['input_integrity']), (1,'FAIL','PASS','PASS'))
        package.assert_not_called()

    def test_native_simulated_test_failure_does_not_package(self):
        rc, result, package = self.native_simulation(tests_rc=1)
        self.assertEqual((rc,result['build'],result['tests']), (1,'PASS','FAIL'))
        package.assert_not_called()

    def test_native_simulated_corruption_does_not_package(self):
        rc, result, package = self.native_simulation(corrupt=True)
        self.assertEqual((rc,result['input_integrity'],result['status']), (1,'FAIL','FAIL'))
        package.assert_not_called()

    def test_native_simulated_exception_still_preserves_integrity(self):
        rc, result, package = self.native_simulation(crash=True)
        self.assertEqual((rc,result['input_integrity'],result['status']), (1,'PASS','FAIL'))
        self.assertTrue((self.root/'ci-out/after-build-source-manifest.json').is_file())
        package.assert_not_called()


if __name__ == '__main__':
    unittest.main()
