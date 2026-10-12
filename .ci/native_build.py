#!/usr/bin/env python3
"""Pinned native CI build; --prepare-only is an offline integrity audit, not BUILD."""
import argparse
import ast
import hashlib
import json
import os
import platform
import plistlib
import shutil
import stat
import struct
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=True) + '\n', encoding='utf-8')


def tree_manifest(root):
    return {p.relative_to(root).as_posix(): sha(p)
            for p in sorted(root.rglob('*')) if p.is_file()}


def verify_archive(path, dep):
    if path.stat().st_size != dep['bytes'] or sha(path) != dep['sha256']:
        raise ValueError('archive size/SHA-256 mismatch: ' + dep['archive'])


def extract_archive(path, dest, root):
    """No traversal, duplicate entries, links or devices. Validate before writing."""
    with zipfile.ZipFile(path) as z:
        entries, names = z.infolist(), set()
        if sum(i.file_size for i in entries) > 128 * 1024 * 1024:
            raise ValueError('unpacked dependency exceeds budget')
        for i in entries:
            p = PurePosixPath(i.filename)
            mode = i.external_attr >> 16
            key = i.filename.rstrip('/').casefold()
            if (p.is_absolute() or '..' in p.parts or '\\' in i.filename
                    or ':' in i.filename or not p.parts or p.parts[0] != root
                    or key in names or stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
                raise ValueError('unsafe dependency archive entry')
            names.add(key)
        if z.testzip() is not None:
            raise ValueError('dependency ZIP CRC failure')
        dest.mkdir(parents=True, exist_ok=False)
        z.extractall(dest)
    return dest / root


def fetch_verified(dep, cache, downloads):
    path = cache / dep['archive']
    if downloads is not None:
        shutil.copyfile(downloads / dep['archive'], path)
    else:
        request = urllib.request.Request(dep['url'], headers={'User-Agent': 'NEC-XE-pinned-build'})
        with urllib.request.urlopen(request, timeout=90) as response, path.open('xb') as f:
            remaining = dep['bytes']
            while chunk := response.read(min(1024 * 1024, remaining + 1)):
                remaining -= len(chunk)
                if remaining < 0:
                    raise ValueError('dependency download exceeds pinned size')
                f.write(chunk)
    verify_archive(path, dep)
    return path


def prepare(lock, work, output, downloads=None):
    roots, manifests = {}, {}
    cache = work / 'downloads'
    cache.mkdir()
    for name, dep in lock['dependencies'].items():
        path = fetch_verified(dep, cache, downloads)
        roots[name] = extract_archive(path, work / name, dep['root'])
        manifests[name] = tree_manifest(roots[name])
    sdk, lilu, source = roots['MacKernelSDK'], roots['Lilu'], roots['WhateverXe']
    for p in (sdk/'Headers/Availability.h', sdk/'Library/x86_64/libkmod.a',
              lilu/'Contents/Resources/Headers/kern_api.hpp',
              lilu/'Contents/Resources/Library/plugin_start.cpp',
              source/'tools/build.py', source/'tools/test.py', source/'AGENTS.md'):
        if not p.is_file():
            raise ValueError('missing required dependency layout')
    with (lilu/'Contents/Info.plist').open('rb') as f:
        if plistlib.load(f)['CFBundleVersion'] != lock['dependencies']['Lilu']['version']:
            raise ValueError('Lilu version mismatch')
    licenses = output/'licenses'
    licenses.mkdir()
    shutil.copyfile(source/'LICENSE.txt', licenses/'WhateverXe-LICENSE.txt')
    shutil.copyfile(sdk/'LICENSE.txt', licenses/'MacKernelSDK-LICENSE.txt')
    lilu_notice = fetch_verified(lock['dependencies']['Lilu']['license'], cache, downloads)
    shutil.copyfile(lilu_notice, licenses/'Lilu-LICENSE.txt')
    write_json(output/'input-manifests.json', manifests)
    return roots, manifests


def capture(command, cwd, output, name):
    with (output / name).open('w', encoding='utf-8') as f:
        f.write(json.dumps(command) + '\n')
        f.flush()
        result = subprocess.run(command, cwd=cwd, stdout=f, stderr=subprocess.STDOUT,
                                timeout=1800, check=False)
    print(name + ': exit ' + str(result.returncode), flush=True)
    return result.returncode


def macho_kext_header(exe):
    with exe.open('rb') as f:
        raw = f.read(32)
    if len(raw) != 32:
        raise ValueError('truncated Mach-O header')
    magic, cpu, subtype, filetype, ncmds, size, flags, reserved = struct.unpack('<8I', raw)
    if magic != 0xFEEDFACF or cpu != 0x01000007 or filetype != 0xB:
        raise ValueError('expected thin x86_64 MH_KEXT_BUNDLE numeric header')
    return {'magic': magic, 'cputype': cpu, 'cpusubtype': subtype, 'filetype': filetype,
            'ncmds': ncmds, 'sizeofcmds': size, 'flags': flags, 'reserved': reserved}


def expected_cpu_jobs(source):
    # Read the pinned runner's literal list without executing its module.
    tree = ast.parse((source/'tools/test.py').read_text(encoding='utf-8'))
    names = next(ast.literal_eval(node.value) for node in tree.body
                 if isinstance(node, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == 'python_tests' for t in node.targets))
    if not isinstance(names, list) or any(not isinstance(n, str) or not n.isidentifier() for n in names):
        raise ValueError('unexpected upstream Python test list')
    cpp = [p.stem for p in sorted((source/'tests').glob('*.cpp')) if not p.stem.endswith('_fixture')]
    jobs = [('cpp', n) for n in cpp] + [('python', n) for n in names]
    if len(set(jobs)) != len(jobs):
        raise ValueError('duplicate test identity within one language')
    return jobs


def qualify_cpu_results(source, output, runner_rc, expected_count):
    jobs = expected_cpu_jobs(source)
    matrix = json.loads((source/'build/tests/results.json').read_text())
    stems = {n for kind, n in jobs}
    if (len(jobs) != expected_count or set(matrix) != stems
            or any(type(v) is not int for v in matrix.values())):
        raise ValueError('upstream default test matrix incomplete')
    collisions = {n for n in stems if sum(stem == n for kind, stem in jobs) > 1}
    qualified = {kind+':'+n: matrix[n] for kind, n in jobs if n not in collisions}
    if collisions:
        logs = output/'duplicate-checks'
        binaries = source/'build/ci-qualified-tests'
        logs.mkdir(); binaries.mkdir()
        for kind, n in jobs:
            if n not in collisions:
                continue
            log = kind+'_'+n+'.log'
            if kind == 'cpp':
                exe = binaries/n
                cmd = ['clang++', '-std=c++17', '-O1', '-g', '-fsanitize=address,undefined',
                       '-fno-omit-frame-pointer', '-I'+str(source/'WhateverXe'),
                       str(source/'tests'/(n+'.cpp')), '-o', str(exe)]
                rc = capture(cmd, source, logs, log)
                if rc == 0:
                    rc = capture([str(exe)], source, logs, kind+'_'+n+'-execute.log')
            else:
                rc = capture([sys.executable, str(source/'tests'/(n+'.py'))], source, logs, log)
            qualified[kind+':'+n] = rc
    status = 'PASS' if runner_rc == 0 and len(qualified) == expected_count and all(v == 0 for v in qualified.values()) else 'FAIL'
    report = {'status': status, 'original_runner_exit': runner_rc, 'scheduled_jobs': len(jobs),
              'original_json_items': len(matrix), 'colliding_names': sorted(collisions),
              'independent_duplicate_rechecks': len(collisions)*2,
              'results': qualified, 'exact_donor': 'NOT_TESTED'}
    write_json(output/'cpu-qualified-results.json', report)
    return report


def inspect_kexts(build, output=None):
    products = {}
    for name in ('WhateverXe', 'AppleIntelTGLCompat'):
        bundle = build / (name + '.kext') / 'Contents'
        exe = bundle / 'MacOS' / name
        with (bundle/'Info.plist').open('rb') as f:
            info = plistlib.load(f)
        arch = subprocess.check_output(['xcrun', 'lipo', '-archs', str(exe)], text=True).strip()
        header = subprocess.check_output(['xcrun', 'otool', '-hv', str(exe)], text=True)
        # Apple otool prints KEXTBUNDLE; validate numeric fields, not display text.
        if output is not None:
            (output/(name+'-inspection.log')).write_text(arch+'\n'+header, encoding='utf-8')
        numeric = macho_kext_header(exe)
        if arch != 'x86_64':
            raise ValueError('expected x86_64 lipo architecture: ' + name)
        if info['CFBundleIdentifier'] != 'org.b00t0x.' + name or info['CFBundleExecutable'] != name:
            raise ValueError('unexpected bundle metadata')
        products[name] = {'sha256': sha(exe), 'architecture': arch, 'mach_header': header, 'numeric_header': numeric,
                          'identifier': info['CFBundleIdentifier'], 'version': info['CFBundleVersion']}
    manifest = json.loads((build/'build-manifest.json').read_text())
    for name, product in products.items():
        if manifest[name]['sha256'] != product['sha256'] or manifest[name]['configuration'] != 'RELEASE':
            raise ValueError('upstream build manifest mismatch')
    return products


def preserve_build_diagnostics(build, output):
    stage = output/'build-diagnostics'
    stage.mkdir()
    (stage/'README.txt').write_text(
        'UNVERIFIED compiled outputs retained for diagnosis.\n'
        'Compiler exit success does not establish CPU, format or runtime acceptance.\n'
        'See result.json, CPU results and inspection logs before considering any use.\n',
        encoding='utf-8')
    for name in ('WhateverXe', 'AppleIntelTGLCompat'):
        for rel in ('Contents/Info.plist', 'Contents/MacOS/'+name):
            src = build/(name+'.kext')/rel
            dest = stage/(name+'.kext')/rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
    shutil.copyfile(build/'build-manifest.json', output/'build-manifest.json')


def package_kexts(build, output):
    with zipfile.ZipFile(output/'kexts.zip', 'x', compression=zipfile.ZIP_DEFLATED) as z:
        for name in ('WhateverXe', 'AppleIntelTGLCompat'):
            for p in sorted((build/(name+'.kext')).rglob('*')):
                if p.is_file():
                    z.write(p, p.relative_to(build).as_posix())
        for name in ('WhateverXe', 'MacKernelSDK', 'Lilu'):
            notice = output/'licenses'/(name+'-LICENSE.txt')
            z.write(notice, 'licenses/'+notice.name)


def finalize(output, result, redactions):
    # Only this fresh output directory is uploaded; no environment dump or local evidence.
    for p in output.rglob('*.log'):
        text = p.read_text(encoding='utf-8', errors='replace')
        for raw, replacement in redactions:
            text = text.replace(raw, replacement)
        p.write_text(text, encoding='utf-8')
    write_json(output/'result.json', result)
    write_json(output/'artifact-manifest.json', tree_manifest(output))
    sums = tree_manifest(output)
    (output/'SHA256SUMS').write_text(''.join(f'{h}  {n}\n' for n, h in sums.items()), encoding='utf-8')
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a', encoding='utf-8') as f:
            f.write('WhateverXe native CI: ' + result['status'] + '\n\n')
            f.write('BUILD: '+result['build']+'; CPU: '+result['tests']+'\n\n')
            f.write('Exact donor / NEC boot / internal panel / acceleration: NOT_TESTED.\n')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--downloads', type=Path, help='existing pinned archives; no network')
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args(argv)
    work, output = args.work.resolve(), args.output.resolve()
    if work == output or work in output.parents or output in work.parents:
        parser.error('work/output must be separate fresh directories')
    if work.exists() or output.exists():
        parser.error('refusing to overwrite existing directories')
    work.mkdir(parents=True)
    output.mkdir(parents=True)
    lock = json.loads((HERE/'dependencies.lock.json').read_text())
    write_json(output/'dependencies.lock.json', lock)
    result = {'status': 'FAIL', 'prepare': 'NOT_TESTED', 'build': 'NOT_TESTED',
              'tests': 'NOT_TESTED', 'artifact_validation': 'NOT_TESTED', 'products': {}, 'exact_donor': 'NOT_TESTED',
              'NEC_boot': 'NOT_TESTED', 'NEC_internal_panel': 'NOT_TESTED',
              'acceleration': 'NOT_TESTED', 'ci_commit': os.environ.get('GITHUB_SHA', 'LOCAL'),
              'run_id': os.environ.get('GITHUB_RUN_ID', 'LOCAL')}
    redactions = [(str(work), '/ci-work'), (str(output), '/ci-output'),
                  (str(HERE.parent), '/nec-xe-ci'), (str(Path.home()), '/ci-home')]
    try:
        if not args.prepare_only:
            if platform.system() != 'Darwin' or platform.machine() != 'x86_64':
                result['status'] = 'BLOCKED'
                raise RuntimeError('BUILD BLOCKED: macOS x86_64 required')
            tc = lock['toolchain']
            if platform.python_version() != tc['python']:
                raise RuntimeError('fixed Python version unavailable')
            def text(cmd):
                return subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT).strip()
            env = {'OS': text(['sw_vers']), 'architecture': platform.machine(),
                   'python': platform.python_version(), 'Xcode': text(['xcodebuild', '-version']),
                   'clang': text(['xcrun', 'clang++', '--version']),
                   'ld': text(['xcrun', 'ld', '-v']), 'SDK': text(['xcrun', '--sdk', 'macosx', '--show-sdk-version']),
                   'CPU_SDKROOT': text(['xcrun', '--sdk', 'macosx', '--show-sdk-path']),
                   'image_version': os.environ.get('ImageVersion', 'UNKNOWN'),
                   'image_os': os.environ.get('ImageOS', 'UNKNOWN')}
            write_json(output/'environment.json', env)
            if env['Xcode'] != 'Xcode '+tc['xcode_version']+'\nBuild version '+tc['xcode_build'] or env['SDK'] != tc['macos_sdk']:
                raise RuntimeError('pinned Xcode/SDK unavailable; no automatic fallback')
            if not Path(env['CPU_SDKROOT']).is_dir():
                raise RuntimeError('CPU SDKROOT directory unavailable')
            # Upstream fixtures invoke clang++ via PATH; use the same Apple compiler.
            compiler_dir = str(Path(text(['xcrun', '--find', 'clang++'])).parent)
            os.environ['PATH'] = compiler_dir + os.pathsep + os.environ['PATH']
        roots, before = prepare(lock, work, output, args.downloads)
        result['prepare'] = 'PASS'
        if args.prepare_only:
            result['status'] = 'PREPARE_PASS_BUILD_NOT_TESTED'
        else:
            source = roots['WhateverXe']
            build = source/'build'
            rc_build = capture([sys.executable, 'tools/build.py', '--release', '--sdk',
                                str(roots['MacKernelSDK']), '--lilu', str(roots['Lilu'])],
                               source, output, 'build.log')
            result['build'] = 'PASS' if rc_build == 0 else 'FAIL'
            if rc_build == 0:
                preserve_build_diagnostics(build, output)
            result['tests'] = 'FAIL'
            original_sdkroot = os.environ.get('SDKROOT')
            os.environ['SDKROOT'] = env['CPU_SDKROOT']
            try:
                rc_tests = capture([sys.executable, 'tools/test.py'], source, output, 'tests.log')
                tests = build/'tests'
                if tests.is_dir():
                    (output/'tests').mkdir()
                    for p in sorted(tests.iterdir()):
                        if p.is_file() and p.suffix in ('.log', '.json'):
                            shutil.copyfile(p, output/'tests'/p.name)
                cpu = qualify_cpu_results(source, output, rc_tests, lock['default_cpu_tests'])
                result['tests'] = cpu['status']
                result['qualified_cpu_tests'] = len(cpu['results'])
            finally:
                if original_sdkroot is None:
                    os.environ.pop('SDKROOT', None)
                else:
                    os.environ['SDKROOT'] = original_sdkroot
            for name, root in roots.items():
                if any(not (root/p).is_file() or sha(root/p) != h for p, h in before[name].items()):
                    raise ValueError('dependency/source modified by build or tests')
            result['input_integrity'] = 'PASS'
            if rc_build == 0:
                result['artifact_validation'] = 'FAIL'
                result['products'] = inspect_kexts(build, output)
                result['artifact_validation'] = 'PASS'
            if result['build'] == 'PASS' and result['tests'] == 'PASS':
                package_kexts(build, output)
                result['status'] = 'PASS'
    except Exception as exc:
        result['error'] = str(exc)
        for raw, replacement in redactions:
            result['error'] = result['error'].replace(raw, replacement)
        print(result['error'], file=sys.stderr)
    finally:
        finalize(output, result, redactions)
    print(json.dumps({k: result[k] for k in ('status', 'prepare', 'build', 'tests')}))
    return 0 if result['status'] in ('PASS', 'PREPARE_PASS_BUILD_NOT_TESTED') else 1


if __name__ == '__main__':
    sys.exit(main())
