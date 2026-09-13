#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, os, shutil, subprocess, sys, zipfile
from pathlib import Path

ROOT = Path(os.environ.get('AOSP_ROOT', Path.cwd())).resolve()
APP_ROOT = ROOT / '.worklog' / 'synthetic-mthp-apk'
TEMPLATES = Path(os.environ.get('MTHP_SYNTH_TEMPLATE_DIR', Path(__file__).resolve().parent / 'mthp_synth_templates')).resolve()
DEFAULT_OUT = APP_ROOT / 'out'

TOOLS = {
    'aapt2': ROOT / 'out/host/linux-x86/bin/aapt2',
    'd8': ROOT / 'out/host/linux-x86/bin/d8',
    'apksigner': ROOT / 'out/host/linux-x86/bin/apksigner',
    'zipalign': ROOT / 'out/host/linux-x86/bin/zipalign',
    'javac': ROOT / 'prebuilts/jdk/jdk21/linux-x86/bin/javac',
    'jar': ROOT / 'prebuilts/jdk/jdk21/linux-x86/bin/jar',
    'android_jar': ROOT / 'prebuilts/sdk/current/public/android.jar',
    'testkey_pk8': ROOT / 'build/make/target/product/security/testkey.pk8',
    'testkey_cert': ROOT / 'build/make/target/product/security/testkey.x509.pem',
}
JDK_BIN = ROOT / 'prebuilts/jdk/jdk21/linux-x86/bin'
NDK_BIN = Path.home() / 'android-sdk/ndk/android-ndk-r27d/toolchains/llvm/prebuilt/linux-x86_64/bin'
CXX = NDK_BIN / 'x86_64-linux-android35-clang++'
CC = NDK_BIN / 'x86_64-linux-android35-clang'

BASE = {
    'vma_size_kb': 64,
    'small_alloc_bytes': 128,
    'large_alloc_bytes': 65536,
    'filemap_threads': 0,
    'filemap_file_mb': 0,
    'java_object_kb': 64,
    'java_churn_ms': 1000,
    'gc_period_ms': 0,
}


def round_up(value: int, step: int) -> int:
    return ((value + step - 1) // step) * step


def tune_vma_geometry(name: str, cfg: dict) -> None:
    """Choose a fully resident anonymous VMA geometry that stays runnable on 8G CVD."""
    cow_pages = int(cfg.get('cow_pages_per_child', 0))
    vma_count = max(1, int(cfg.get('vma_count', 1)))

    base_pages_per_vma = 4  # 16 KiB minimum, so every synthetic VMA is mTHP-sized.
    if name.startswith(('java_', 'scudo_', 'dlopen_', 'file_', 'mixed_')):
        base_pages_per_vma = 8  # 32 KiB: more resident pressure without huge sparse VA.

    pages_per_vma = base_pages_per_vma
    if cow_pages > 0:
        # Ensure the main process owns enough resident pages for its COW target.
        pages_per_vma = max(pages_per_vma, round_up((cow_pages + vma_count - 1) // vma_count, 4))

    cfg['touch_pages_per_vma'] = pages_per_vma
    cfg['vma_size_kb'] = pages_per_vma * 4


def tune_resident_touch_density(name: str, cfg: dict) -> None:
    """Record full-fault accounting; runtime writes every page of every anonymous VMA."""
    vma_size_kb = int(cfg.get('vma_size_kb', BASE['vma_size_kb']))
    vma_count = int(cfg.get('vma_count', 0))
    pages_per_vma = max(1, vma_size_kb // 4)
    target = vma_count * pages_per_vma
    cfg['parent_touch_pages'] = target
    cfg['anon_full_fault_pages'] = target
    cfg['anon_full_fault_mb'] = target * 4 // 1024
    cfg['parent_touch_mb'] = cfg['anon_full_fault_mb']
    cfg['anon_fault_mode'] = 'full_write'
    cfg['so_fault_mode'] = 'full_read'
    cfg['filemap_fault_mode'] = 'full_read'


def scale_anon_and_cow_geometry(cfg: dict, anon_vma_size_scale: float, cow_pages_scale: float) -> None:
    if cow_pages_scale != 1.0:
        old_cow_pages = int(cfg.get('cow_pages_per_child', 0))
        new_cow_pages = int(round(old_cow_pages * cow_pages_scale))
        cfg['cow_pages_per_child'] = max(0, new_cow_pages)
        cfg['cow_pages_scale'] = cow_pages_scale
        if old_cow_pages != cfg['cow_pages_per_child']:
            cfg['cow_pages_per_child_unscaled'] = old_cow_pages

    if anon_vma_size_scale != 1.0:
        old_pages_per_vma = max(1, int(cfg.get('touch_pages_per_vma', 1)))
        scaled_pages = int(old_pages_per_vma * anon_vma_size_scale)
        scaled_pages = max(4, (scaled_pages // 4) * 4)
        cfg['touch_pages_per_vma'] = scaled_pages
        cfg['vma_size_kb'] = scaled_pages * 4
        cfg['anon_vma_size_scale'] = anon_vma_size_scale
        if old_pages_per_vma != scaled_pages:
            cfg['touch_pages_per_vma_unscaled'] = old_pages_per_vma
            cfg['vma_size_kb_unscaled'] = old_pages_per_vma * 4

    resident_pages = int(cfg.get('vma_count', 0)) * max(1, int(cfg.get('touch_pages_per_vma', 1)))
    cow_pages = int(cfg.get('cow_pages_per_child', 0))
    if cow_pages > resident_pages:
        cfg['cow_pages_per_child_capped_from'] = cow_pages
        cfg['cow_pages_per_child'] = resident_pages

PROFILE_SPECS = [
    ('java_s',        dict(process_count=1, java_live_mb=64,  scudo_threads=1, scudo_live_mb=16,  vma_count=800,  parent_touch_pages=512,   dlopen_lib_count=4,  fork_children=0, cow_pages_per_child=0)),
    ('java_m',        dict(process_count=1, java_live_mb=192, scudo_threads=1, scudo_live_mb=32,  vma_count=1200, parent_touch_pages=1024,  dlopen_lib_count=8,  fork_children=0, cow_pages_per_child=0)),
    ('java_l',        dict(process_count=1, java_live_mb=384, scudo_threads=2, scudo_live_mb=64,  vma_count=1600, parent_touch_pages=2048,  dlopen_lib_count=8,  fork_children=0, cow_pages_per_child=0, gc_period_ms=10000)),
    ('scudo_s',       dict(process_count=1, java_live_mb=32,  scudo_threads=4, scudo_live_mb=128, vma_count=1000, parent_touch_pages=1024,  dlopen_lib_count=4,  fork_children=0, cow_pages_per_child=0)),
    ('scudo_m',       dict(process_count=1, java_live_mb=64,  scudo_threads=8, scudo_live_mb=256, vma_count=1500, parent_touch_pages=2048,  dlopen_lib_count=8,  fork_children=0, cow_pages_per_child=0)),
    ('scudo_l',       dict(process_count=1, java_live_mb=64,  scudo_threads=16,scudo_live_mb=512, vma_count=2000, parent_touch_pages=4096,  dlopen_lib_count=8,  fork_children=0, cow_pages_per_child=0)),
    ('dlopen_s',      dict(process_count=1, java_live_mb=32,  scudo_threads=2, scudo_live_mb=64,  vma_count=1000, parent_touch_pages=1024,  dlopen_lib_count=16, fork_children=0, cow_pages_per_child=0)),
    ('dlopen_m',      dict(process_count=1, java_live_mb=64,  scudo_threads=2, scudo_live_mb=96,  vma_count=1500, parent_touch_pages=2048,  dlopen_lib_count=32, fork_children=0, cow_pages_per_child=0)),
    ('dlopen_l',      dict(process_count=1, java_live_mb=64,  scudo_threads=4, scudo_live_mb=128, vma_count=2000, parent_touch_pages=4096,  dlopen_lib_count=64, fork_children=0, cow_pages_per_child=0)),
    ('vma_s',         dict(process_count=1, java_live_mb=32,  scudo_threads=2, scudo_live_mb=64,  vma_count=3000, parent_touch_pages=3000,  dlopen_lib_count=8,  fork_children=0, cow_pages_per_child=0)),
    ('vma_m',         dict(process_count=1, java_live_mb=32,  scudo_threads=2, scudo_live_mb=64,  vma_count=6000, parent_touch_pages=6000,  dlopen_lib_count=8,  fork_children=0, cow_pages_per_child=0)),
    ('vma_l',         dict(process_count=1, java_live_mb=32,  scudo_threads=2, scudo_live_mb=64,  vma_count=10000,parent_touch_pages=10000, dlopen_lib_count=8,  fork_children=0, cow_pages_per_child=0)),
    ('cow_s',         dict(process_count=1, java_live_mb=32,  scudo_threads=2, scudo_live_mb=64,  vma_count=3000, parent_touch_pages=4096,  dlopen_lib_count=4,  fork_children=1, cow_pages_per_child=4096)),
    ('cow_m',         dict(process_count=1, java_live_mb=32,  scudo_threads=2, scudo_live_mb=64,  vma_count=4000, parent_touch_pages=8192,  dlopen_lib_count=4,  fork_children=2, cow_pages_per_child=8192)),
    ('cow_l',         dict(process_count=1, java_live_mb=32,  scudo_threads=2, scudo_live_mb=64,  vma_count=6000, parent_touch_pages=16384, dlopen_lib_count=4,  fork_children=4, cow_pages_per_child=16384)),
    ('cow_xl',        dict(process_count=1, java_live_mb=32,  scudo_threads=2, scudo_live_mb=64,  vma_count=8000, parent_touch_pages=32768, dlopen_lib_count=4,  fork_children=4, cow_pages_per_child=32768)),
    ('cow_xxl',       dict(process_count=1, java_live_mb=32,  scudo_threads=2, scudo_live_mb=64,  vma_count=10000,parent_touch_pages=65536, dlopen_lib_count=4,  fork_children=4, cow_pages_per_child=65536)),
    ('mixed_s',       dict(process_count=1, java_live_mb=96,  scudo_threads=4, scudo_live_mb=128, vma_count=3000, parent_touch_pages=4096,  dlopen_lib_count=16, fork_children=1, cow_pages_per_child=4096)),
    ('mixed_m',       dict(process_count=1, java_live_mb=128, scudo_threads=8, scudo_live_mb=256, vma_count=5000, parent_touch_pages=8192,  dlopen_lib_count=32, fork_children=2, cow_pages_per_child=8192)),
    ('mixed_l',       dict(process_count=1, java_live_mb=192, scudo_threads=8, scudo_live_mb=384, vma_count=7000, parent_touch_pages=16384, dlopen_lib_count=48, fork_children=4, cow_pages_per_child=16384)),
    ('mixed_xl',      dict(process_count=2, java_live_mb=192, scudo_threads=8, scudo_live_mb=384, vma_count=6000, parent_touch_pages=32768, dlopen_lib_count=48, fork_children=4, cow_pages_per_child=32768)),
    ('monster_multiproc', dict(process_count=4, java_live_mb=256, scudo_threads=12, scudo_live_mb=512, vma_count=6000, parent_touch_pages=32768, dlopen_lib_count=64, fork_children=4, cow_pages_per_child=32768)),
    ('file_bg_s',     dict(process_count=1, java_live_mb=32,  scudo_threads=2, scudo_live_mb=64,  vma_count=2000, parent_touch_pages=4096,  dlopen_lib_count=8,  fork_children=1, cow_pages_per_child=4096, filemap_threads=1, filemap_file_mb=64)),
    ('file_bg_m',     dict(process_count=1, java_live_mb=64,  scudo_threads=4, scudo_live_mb=128, vma_count=3000, parent_touch_pages=8192,  dlopen_lib_count=16, fork_children=2, cow_pages_per_child=8192, filemap_threads=2, filemap_file_mb=128)),
    ('java_xl',       dict(process_count=1, java_live_mb=512, scudo_threads=2, scudo_live_mb=64,  vma_count=1800, parent_touch_pages=4096,  dlopen_lib_count=8,  fork_children=0, cow_pages_per_child=0, gc_period_ms=7000)),
    ('java_gc_mix',   dict(process_count=2, java_live_mb=256, scudo_threads=2, scudo_live_mb=96,  vma_count=2400, parent_touch_pages=4096,  dlopen_lib_count=8,  fork_children=1, cow_pages_per_child=4096, gc_period_ms=5000)),
    ('scudo_xl',      dict(process_count=1, java_live_mb=64,  scudo_threads=24,scudo_live_mb=768, vma_count=2600, parent_touch_pages=8192,  dlopen_lib_count=8,  fork_children=0, cow_pages_per_child=0)),
    ('dlopen_xl',     dict(process_count=1, java_live_mb=64,  scudo_threads=4, scudo_live_mb=160, vma_count=2600, parent_touch_pages=8192,  dlopen_lib_count=64, fork_children=1, cow_pages_per_child=4096)),
    ('dlopen_mp',     dict(process_count=2, java_live_mb=96,  scudo_threads=4, scudo_live_mb=192, vma_count=3000, parent_touch_pages=8192,  dlopen_lib_count=64, fork_children=1, cow_pages_per_child=8192)),
    ('vma_xxl',       dict(process_count=1, java_live_mb=32,  scudo_threads=2, scudo_live_mb=64,  vma_count=14000,parent_touch_pages=14000, dlopen_lib_count=8,  fork_children=1, cow_pages_per_child=4096)),
    ('cow_mega',      dict(process_count=1, java_live_mb=32,  scudo_threads=2, scudo_live_mb=64,  vma_count=12000,parent_touch_pages=98304, dlopen_lib_count=4,  fork_children=4, cow_pages_per_child=98304)),
    ('cow_dlopen',    dict(process_count=1, java_live_mb=64,  scudo_threads=4, scudo_live_mb=128, vma_count=8000, parent_touch_pages=32768, dlopen_lib_count=32, fork_children=4, cow_pages_per_child=32768)),
    ('cow_multiproc', dict(process_count=3, java_live_mb=96,  scudo_threads=4, scudo_live_mb=160, vma_count=5000, parent_touch_pages=32768, dlopen_lib_count=16, fork_children=4, cow_pages_per_child=32768)),
    ('mixed_xxl',     dict(process_count=2, java_live_mb=256, scudo_threads=12,scudo_live_mb=512, vma_count=8000, parent_touch_pages=65536, dlopen_lib_count=64, fork_children=4, cow_pages_per_child=65536)),
    ('mixed_service', dict(process_count=4, java_live_mb=160, scudo_threads=8, scudo_live_mb=320, vma_count=4000, parent_touch_pages=32768, dlopen_lib_count=48, fork_children=2, cow_pages_per_child=32768)),
    ('file_bg_l',     dict(process_count=1, java_live_mb=64,  scudo_threads=4, scudo_live_mb=160, vma_count=4000, parent_touch_pages=16384, dlopen_lib_count=16, fork_children=2, cow_pages_per_child=16384, filemap_threads=2, filemap_file_mb=256)),
    ('java_gc_heavy', dict(process_count=1, java_live_mb=640, scudo_threads=2, scudo_live_mb=96,  vma_count=2400, parent_touch_pages=8192,  dlopen_lib_count=8,  fork_children=1, cow_pages_per_child=8192,  gc_period_ms=4000)),
    ('java_mp_gc',    dict(process_count=3, java_live_mb=256, scudo_threads=2, scudo_live_mb=96,  vma_count=2600, parent_touch_pages=8192,  dlopen_lib_count=8,  fork_children=1, cow_pages_per_child=8192,  gc_period_ms=5000)),
    ('java_scudo_gc', dict(process_count=2, java_live_mb=320, scudo_threads=6, scudo_live_mb=192, vma_count=3200, parent_touch_pages=8192,  dlopen_lib_count=16, fork_children=2, cow_pages_per_child=8192,  gc_period_ms=6000)),
    ('scudo_xxl',     dict(process_count=1, java_live_mb=64,  scudo_threads=32,scudo_live_mb=1024,vma_count=3200, parent_touch_pages=16384, dlopen_lib_count=8,  fork_children=0, cow_pages_per_child=0)),
    ('scudo_mp',      dict(process_count=3, java_live_mb=96,  scudo_threads=12,scudo_live_mb=384, vma_count=3000, parent_touch_pages=16384, dlopen_lib_count=8,  fork_children=1, cow_pages_per_child=8192)),
    ('scudo_cow',     dict(process_count=1, java_live_mb=64,  scudo_threads=16,scudo_live_mb=512, vma_count=5000, parent_touch_pages=32768, dlopen_lib_count=16, fork_children=4, cow_pages_per_child=32768)),
    ('dlopen_xxl',    dict(process_count=1, java_live_mb=64,  scudo_threads=4, scudo_live_mb=192, vma_count=3200, parent_touch_pages=16384, dlopen_lib_count=64, fork_children=1, cow_pages_per_child=8192)),
    ('dlopen_cow_h',  dict(process_count=1, java_live_mb=96,  scudo_threads=4, scudo_live_mb=192, vma_count=7000, parent_touch_pages=32768, dlopen_lib_count=64, fork_children=4, cow_pages_per_child=32768)),
    ('dlopen_service',dict(process_count=4, java_live_mb=96,  scudo_threads=4, scudo_live_mb=192, vma_count=3600, parent_touch_pages=16384, dlopen_lib_count=64, fork_children=2, cow_pages_per_child=16384)),
    ('vma_cow_s',     dict(process_count=1, java_live_mb=32,  scudo_threads=2, scudo_live_mb=64,  vma_count=12000,parent_touch_pages=16384, dlopen_lib_count=8,  fork_children=2, cow_pages_per_child=16384)),
    ('vma_cow_m',     dict(process_count=1, java_live_mb=32,  scudo_threads=2, scudo_live_mb=64,  vma_count=16000,parent_touch_pages=32768, dlopen_lib_count=8,  fork_children=4, cow_pages_per_child=32768)),
    ('vma_multiproc', dict(process_count=3, java_live_mb=64,  scudo_threads=2, scudo_live_mb=96,  vma_count=8000, parent_touch_pages=16384, dlopen_lib_count=8,  fork_children=2, cow_pages_per_child=16384)),
    ('cow_burst_s',   dict(process_count=1, java_live_mb=32,  scudo_threads=2, scudo_live_mb=64,  vma_count=6000, parent_touch_pages=32768, dlopen_lib_count=4,  fork_children=6, cow_pages_per_child=32768)),
    ('cow_burst_m',   dict(process_count=1, java_live_mb=32,  scudo_threads=2, scudo_live_mb=64,  vma_count=9000, parent_touch_pages=65536, dlopen_lib_count=4,  fork_children=6, cow_pages_per_child=65536)),
    ('cow_burst_l',   dict(process_count=1, java_live_mb=32,  scudo_threads=2, scudo_live_mb=64,  vma_count=12000,parent_touch_pages=98304, dlopen_lib_count=4,  fork_children=6, cow_pages_per_child=98304)),
    ('cow_service',   dict(process_count=4, java_live_mb=64,  scudo_threads=4, scudo_live_mb=128, vma_count=5000, parent_touch_pages=32768, dlopen_lib_count=8,  fork_children=4, cow_pages_per_child=32768)),
    ('cow_service_h', dict(process_count=4, java_live_mb=96,  scudo_threads=4, scudo_live_mb=160, vma_count=7000, parent_touch_pages=65536, dlopen_lib_count=16, fork_children=4, cow_pages_per_child=65536)),
    ('cow_dlopen_h',  dict(process_count=1, java_live_mb=96,  scudo_threads=4, scudo_live_mb=160, vma_count=10000,parent_touch_pages=65536, dlopen_lib_count=64, fork_children=4, cow_pages_per_child=65536)),
    ('cow_mega_mp',   dict(process_count=2, java_live_mb=96,  scudo_threads=4, scudo_live_mb=160, vma_count=10000,parent_touch_pages=98304, dlopen_lib_count=16, fork_children=4, cow_pages_per_child=98304)),
    ('mixed_cow_h',   dict(process_count=2, java_live_mb=256, scudo_threads=12,scudo_live_mb=512, vma_count=9000, parent_touch_pages=65536, dlopen_lib_count=64, fork_children=4, cow_pages_per_child=65536)),
    ('mixed_dlopen_svc', dict(process_count=4, java_live_mb=160, scudo_threads=8, scudo_live_mb=320, vma_count=5000, parent_touch_pages=32768, dlopen_lib_count=64, fork_children=2, cow_pages_per_child=32768)),
    ('mixed_vma_pressure', dict(process_count=2, java_live_mb=128, scudo_threads=8, scudo_live_mb=256, vma_count=14000,parent_touch_pages=32768, dlopen_lib_count=32, fork_children=4, cow_pages_per_child=32768)),
    ('mixed_forkstorm', dict(process_count=2, java_live_mb=96,  scudo_threads=4, scudo_live_mb=192, vma_count=8000, parent_touch_pages=65536, dlopen_lib_count=16, fork_children=8, cow_pages_per_child=65536)),
    ('file_bg_xl',    dict(process_count=1, java_live_mb=64,  scudo_threads=4, scudo_live_mb=160, vma_count=5000, parent_touch_pages=32768, dlopen_lib_count=16, fork_children=2, cow_pages_per_child=32768, filemap_threads=2, filemap_file_mb=512)),
]


def run(cmd, cwd=None):
    print('+', ' '.join(str(x) for x in cmd), flush=True)
    env = os.environ.copy()
    env['PATH'] = str(JDK_BIN) + os.pathsep + env.get('PATH', '')
    cp = subprocess.run([str(x) for x in cmd], cwd=str(cwd) if cwd else None, env=env, text=True, capture_output=True)
    if cp.returncode != 0:
        print(cp.stdout)
        print(cp.stderr, file=sys.stderr)
        raise SystemExit(cp.returncode)
    return cp


def ensure_tools():
    missing = [str(p) for p in list(TOOLS.values()) + [CXX, CC] if not Path(p).exists()]
    if missing:
        raise SystemExit('missing tools:\n' + '\n'.join(missing))


def clean_dir(path: Path):
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def write_text(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


def build_native(out: Path, max_pads: int, rodata_kb: int, data_kb: int):
    native = out / 'native'
    clean_dir(native)
    core = native / 'libmthpwork.so'
    run([CXX, '-std=c++17', '-O2', '-fPIC', '-shared', '-static-libstdc++', '-fvisibility=hidden',
         '-o', core, TEMPLATES / 'mthpwork.cpp', '-llog', '-ldl'])
    pads = native / 'pads'
    pads.mkdir()
    for i in range(max_pads):
        run([CC, '-std=gnu11', '-O2', '-fPIC', '-shared',
             f'-DPAD_INDEX={i}', f'-DPAD_RODATA_KB={rodata_kb}', f'-DPAD_DATA_KB={data_kb}',
             '-o', pads / f'libmthppad{i:03d}.so', TEMPLATES / 'padlib.c'])
    return native


def build_java(out: Path):
    java_out = out / 'java'
    clean_dir(java_out)
    src_dir = java_out / 'src/com/zzhao/mthp/synthetic'
    src_dir.mkdir(parents=True)
    shutil.copy2(TEMPLATES / 'WorkloadRuntime.java', src_dir / 'WorkloadRuntime.java')
    classes = java_out / 'classes'
    classes.mkdir()
    run([TOOLS['javac'], '-source', '11', '-target', '11', '-encoding', 'UTF-8',
         '-cp', TOOLS['android_jar'], '-d', classes, src_dir / 'WorkloadRuntime.java'])
    classes_jar = java_out / 'classes.jar'
    run([TOOLS['jar'], 'cf', classes_jar, '-C', classes, '.'])
    dex = java_out / 'dex'
    dex.mkdir()
    run([TOOLS['d8'], '--min-api', '29', '--output', dex, classes_jar])
    return dex / 'classes.dex'


def manifest(package: str, label: str) -> str:
    return f'''<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android" package="{package}">
    <uses-permission android:name="android.permission.WAKE_LOCK" />
    <application android:label="{label}" android:debuggable="true" android:extractNativeLibs="false" android:allowBackup="false" android:usesCleartextTraffic="true">
        <activity android:name="com.zzhao.mthp.synthetic.WorkloadRuntime$MainActivity" android:exported="true">
            <intent-filter>
                <action android:name="android.intent.action.MAIN" />
                <category android:name="android.intent.category.LAUNCHER" />
            </intent-filter>
        </activity>
        <service android:name="com.zzhao.mthp.synthetic.WorkloadRuntime$WorkerService1" android:process=":w1" android:exported="false" />
        <service android:name="com.zzhao.mthp.synthetic.WorkloadRuntime$WorkerService2" android:process=":w2" android:exported="false" />
        <service android:name="com.zzhao.mthp.synthetic.WorkloadRuntime$WorkerService3" android:process=":w3" android:exported="false" />
    </application>
</manifest>
'''


def resources_xml(label: str) -> str:
    return f'''<?xml version="1.0" encoding="utf-8"?>
<resources>
    <string name="app_name">{label}</string>
</resources>
'''


def make_profiles(limit: int | None, anon_vma_size_scale: float = 1.0, cow_pages_scale: float = 1.0):
    profiles = []
    for idx, (name, vals) in enumerate(PROFILE_SPECS):
        cfg = dict(BASE)
        cfg.update(vals)
        tune_vma_geometry(name, cfg)
        scale_anon_and_cow_geometry(cfg, anon_vma_size_scale, cow_pages_scale)
        tune_resident_touch_density(name, cfg)
        cfg['profile_index'] = idx
        cfg['profile_name'] = name
        cfg['package'] = f'com.zzhao.mthp.synth.p{idx:02d}'
        cfg['label'] = f'MTHP {idx:02d} {name}'
        cfg['cow_total_mb'] = cfg['fork_children'] * cfg['cow_pages_per_child'] * 4 // 1024
        profiles.append(cfg)
    return profiles[:limit] if limit else profiles


def add_tree_to_zip(zip_path: Path, root: Path):
    with zipfile.ZipFile(zip_path, 'a') as z:
        for p in root.rglob('*'):
            if p.is_file():
                arcname = p.relative_to(root).as_posix()
                compress_type = zipfile.ZIP_STORED if arcname.startswith('lib/') and arcname.endswith('.so') else zipfile.ZIP_DEFLATED
                z.write(p, arcname, compress_type=compress_type)


def build_one(out: Path, native: Path, classes_dex: Path, cfg: dict):
    name = f"p{cfg['profile_index']:02d}_{cfg['profile_name']}"
    work = out / 'work' / name
    clean_dir(work)
    write_text(work / 'AndroidManifest.xml', manifest(cfg['package'], cfg['label']))
    write_text(work / 'res/values/strings.xml', resources_xml(cfg['label']))
    assets = work / 'assets'
    assets.mkdir(parents=True)
    write_text(assets / 'profile.json', json.dumps(cfg, indent=2, sort_keys=True) + '\n')
    compiled = work / 'compiled.zip'
    run([TOOLS['aapt2'], 'compile', '--dir', work / 'res', '-o', compiled])
    unsigned = work / 'unsigned.apk'
    run([TOOLS['aapt2'], 'link', '-I', TOOLS['android_jar'], '--manifest', work / 'AndroidManifest.xml',
         '--min-sdk-version', '29', '--target-sdk-version', '35', '-A', assets, '-o', unsigned, compiled])
    ziproot = work / 'ziproot'
    (ziproot / 'lib/x86_64').mkdir(parents=True)
    shutil.copy2(classes_dex, ziproot / 'classes.dex')
    shutil.copy2(native / 'libmthpwork.so', ziproot / 'lib/x86_64/libmthpwork.so')
    for i in range(int(cfg['dlopen_lib_count'])):
        shutil.copy2(native / 'pads' / f'libmthppad{i:03d}.so', ziproot / f'lib/x86_64/libmthppad{i:03d}.so')
    add_tree_to_zip(unsigned, ziproot)
    aligned = work / 'aligned.apk'
    signed = out / 'apks' / f'{name}.apk'
    signed.parent.mkdir(parents=True, exist_ok=True)
    run([TOOLS['zipalign'], '-f', '-P', '16', '4', unsigned, aligned])
    run([TOOLS['apksigner'], 'sign', '--key', TOOLS['testkey_pk8'], '--cert', TOOLS['testkey_cert'], '--out', signed, aligned])
    run([TOOLS['apksigner'], 'verify', '--verbose', signed])
    return signed


def write_manifest(out: Path, profiles: list[dict], apks: list[Path]):
    rows = []
    keys = [
        'profile_index','profile_name','package','label','process_count',
        'vma_count','vma_size_kb','anon_fault_mode','anon_full_fault_pages','anon_full_fault_mb',
        'parent_touch_pages','touch_pages_per_vma','parent_touch_mb',
        'scudo_threads','scudo_live_mb','java_live_mb',
        'so_fault_mode','dlopen_lib_count',
        'fork_children','cow_pages_per_child','cow_total_mb',
        'filemap_fault_mode','filemap_threads','filemap_file_mb',
        'anon_vma_size_scale','vma_size_kb_unscaled','touch_pages_per_vma_unscaled',
        'cow_pages_scale','cow_pages_per_child_unscaled','cow_pages_per_child_capped_from',
    ]
    for cfg, apk in zip(profiles, apks):
        row = {k: cfg[k] for k in keys if k in cfg}
        row['apk'] = str(apk)
        row['apk_bytes'] = apk.stat().st_size
        rows.append(row)
    write_text(out / 'profiles.json', json.dumps(rows, indent=2, ensure_ascii=False) + '\n')
    write_text(out / 'packages.txt', ''.join(f"{row['package']}\n" for row in rows))
    import csv
    fieldnames = keys + ['apk', 'apk_bytes']
    with (out / 'profiles.tsv').open('w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, delimiter='\t')
        w.writeheader(); w.writerows(rows)


def install_apks(serial: str, apks: list[Path]):
    for apk in apks:
        run(['adb', '-s', serial, 'install', '--no-incremental', '-r', '-g', apk])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out-dir', default=str(DEFAULT_OUT))
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--max-pads', type=int, default=64)
    ap.add_argument('--pad-rodata-kb', type=int, default=256)
    ap.add_argument('--pad-data-kb', type=int, default=64)
    ap.add_argument('--anon-vma-size-scale', type=float, default=1.0,
                    help='Scale anonymous VMA size after normal geometry tuning; rounded down to 16KB granularity and clamped to >=16KB.')
    ap.add_argument('--cow-pages-scale', type=float, default=1.0,
                    help='Scale cow_pages_per_child; capped to resident anonymous pages after VMA-size scaling.')
    ap.add_argument('--install-serial')
    args = ap.parse_args()
    if args.anon_vma_size_scale <= 0:
        raise SystemExit('--anon-vma-size-scale must be > 0')
    if args.cow_pages_scale < 0:
        raise SystemExit('--cow-pages-scale must be >= 0')
    ensure_tools()
    out = Path(args.out_dir)
    clean_dir(out)
    (out / 'apks').mkdir(parents=True, exist_ok=True)
    profiles = make_profiles(args.limit if args.limit > 0 else None,
                             args.anon_vma_size_scale,
                             args.cow_pages_scale)
    max_needed = max(int(p['dlopen_lib_count']) for p in profiles)
    max_pads = max(args.max_pads, max_needed)
    native = build_native(out, max_pads, args.pad_rodata_kb, args.pad_data_kb)
    classes_dex = build_java(out)
    apks = []
    for cfg in profiles:
        apks.append(build_one(out, native, classes_dex, cfg))
    write_manifest(out, profiles, apks)
    print(f'BUILT out={out} apks={len(apks)}')
    if args.install_serial:
        install_apks(args.install_serial, apks)
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
