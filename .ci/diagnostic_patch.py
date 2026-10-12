"""Fixed EXP016 integration on disposable, verified source; no hardware access."""
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

PIN = '61a449255d385aeb468f64e5d5042e126f387b90'
TARGET = 'WhateverXe/kern_xe.cpp'
VARIANT = 'exp016-panel-a-diagnostic'
PATCH = Path(__file__).resolve().parent/'patches/EXP-016-panel-a-redacted-observer.diff'
PATCH_BYTES = 3468
PATCH_SHA256 = 'f806150ef6200b8ae5739a07d31a8e5fae93ee7e2ede42d0d3bd73d8f5f17aec'
BASE_SHA256 = 'b87d62f064782ee8b7afecd34c287b19b85b93166bdfaf5a8d51577c51539b20'
PATCHED_SHA256 = 'ba87bac89886ce078294b55ee58619c23f09d180307d6f73827fc2c09e4bcbaf'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def manifest(root):
    result = {}
    for p in sorted(root.rglob('*')):
        if p.is_symlink():
            raise ValueError('source symlink rejected')
        if p.is_file():
            result[p.relative_to(root).as_posix()] = sha(p)
    return result


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=True)+'\n', encoding='utf-8')


def apply_command(source, reverse=False, check=False):
    command = ['git', '-c', 'core.autocrlf=false', 'apply']
    if reverse:
        command.append('--reverse')
    if check:
        command.append('--check')
    # Fixed hash input, no shell, no arbitrary patch path or offset fallback.
    command.append(str(PATCH))
    subprocess.run(command, cwd=source, check=True, capture_output=True, timeout=60)


def apply_fixed(source, before, pin, output, scratch):
    if pin != PIN:
        raise ValueError('diagnostic source pin mismatch')
    if PATCH.is_symlink() or PATCH.stat().st_size != PATCH_BYTES or sha(PATCH) != PATCH_SHA256:
        raise ValueError('fixed diagnostic patch size/hash mismatch')
    if before.get(TARGET) != BASE_SHA256 or manifest(source) != before:
        raise ValueError('diagnostic base source mismatch')
    save(output/'base-source-manifest.json', before)
    patch_record = {'variant': VARIANT, 'base_revision': PIN, 'path': PATCH.name,
                    'bytes': PATCH_BYTES, 'sha256': PATCH_SHA256,
                    'target': TARGET, 'base_sha256': BASE_SHA256,
                    'patched_sha256': PATCHED_SHA256,
                    'kernel_read_safety': 'NOT_TESTED', 'NEC_internal_panel': 'NOT_TESTED'}
    save(output/'patch-manifest.json', patch_record)
    apply_command(source, check=True)
    apply_command(source)
    expected = dict(before)
    expected[TARGET] = PATCHED_SHA256
    after = manifest(source)
    if after != expected:
        raise ValueError('diagnostic patch changed unexpected content/files')
    save(output/'patched-source-manifest.json', after)
    apply_command(source, reverse=True, check=True)
    # Actual reverse is confined to a second fresh copy, never the build input.
    shutil.copytree(source, scratch)
    apply_command(scratch, reverse=True)
    if manifest(scratch) != before:
        raise ValueError('diagnostic reverse did not restore exact base')
    patch_record['apply_reverse'] = 'PASS'
    save(output/'patch-manifest.json', patch_record)
    return after, patch_record


def verify_source_after_build(source, expected):
    # Only upstream's build/ output tree is allowed to add files.
    actual = {n: h for n, h in manifest(source).items() if not n.startswith('build/')}
    if actual != expected:
        raise ValueError('patched source modified by build/tests or unexpected file added')
    if sha(PATCH) != PATCH_SHA256 or PATCH.stat().st_size != PATCH_BYTES:
        raise ValueError('diagnostic patch modified during build/tests')
