"""Build and check a standalone Windows x64 companion; never start an experiment."""
from pathlib import Path
import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if sys.platform != 'win32' or sys.maxsize <= 2**32:
    raise SystemExit('Use Windows x64 Python 3.12 with Tcl/Tk.')
import tkinter
tkinter.Tcl()  # Fail clearly if the build Python lacks its own matching Tcl data.
import PyInstaller.__main__

PyInstaller.__main__.run([
    '--noconfirm', '--clean', '--onedir', '--windowed', '--noupx', '--name', 'CHIBackup',
    '--distpath', str(ROOT/'dist'), '--workpath', str(ROOT/'build/pyinstaller'),
    '--specpath', str(ROOT/'build'), '--paths', str(ROOT/'app'), '--hidden-import', 'self_check',
    '--exclude-module', 'matplotlib', '--exclude-module', 'numpy', '--exclude-module', 'pandas',
    '--exclude-module', 'IPython', '--exclude-module', 'pytest', str(ROOT/'app/chi_backup.py'),
])
bundle = ROOT/'dist/CHIBackup'
env = {k:v for k,v in os.environ.items() if k not in ('PYTHONPATH','PYTHONHOME','TCL_LIBRARY','TK_LIBRARY')}
with tempfile.TemporaryDirectory(prefix='chi-check-') as temp:
    check = Path(temp)
    subprocess.run([str(bundle/'CHIBackup.exe'), '--self-test', str(check), '--ui-check'],
                   env=env, cwd=bundle, timeout=60, check=True)
    result = json.loads((check/'result.json').read_text(encoding='utf-8'))
    if not (result['passed'] and result['ui_startup']):
        raise RuntimeError('Portable application self-check failed.')
    shutil.copytree(check/'examples', bundle/'examples')
    shutil.copy2(check/'result.json', bundle/'build-verification.json')
shutil.copy2(ROOT/'docs/使用说明.txt', bundle/'使用说明.txt')
(bundle/'source').mkdir()
for f in (ROOT/'app').glob('*.py'):
    shutil.copy2(f, bundle/'source'/f.name)
licenses = bundle/'licenses'; licenses.mkdir()
shutil.copy2(Path(sys.base_prefix)/'LICENSE.txt', licenses/'Python-LICENSE.txt')
dist = importlib.metadata.distribution('pyinstaller')
for f in dist.files or []:
    if str(f).endswith('licenses/COPYING.txt'):
        shutil.copy2(dist.locate_file(f), licenses/'PyInstaller-COPYING.txt')
for name in ('tcl8.6','tk8.6'):
    for f in (Path(sys.base_prefix)/'tcl'/name).glob('license*'):
        shutil.copy2(f, licenses/(name+'-'+f.name))
(bundle/'THIRD_PARTY_NOTICES.txt').write_text(
    'Python and Tcl/Tk: see licenses/Python-LICENSE.txt and bundled Tcl/Tk notices.\n'
    'PyInstaller bootloader: see licenses/PyInstaller-COPYING.txt.\n'
    'CHI software is not included.\n', encoding='utf-8')
hashes={p.relative_to(bundle).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
        for p in bundle.rglob('*') if p.is_file()}
(bundle/'SHA256.json').write_text(json.dumps(hashes,ensure_ascii=False,indent=2),encoding='utf-8')
archive=ROOT/'dist/CHIBackup-0.1-Windows-x64.zip'
with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED,compresslevel=7) as z:
    for p in sorted(bundle.rglob('*')):
        if p.is_file():z.write(p,Path('CHIBackup')/p.relative_to(bundle))
archive.with_suffix('.zip.sha256').write_text(
    hashlib.sha256(archive.read_bytes()).hexdigest()+'  '+archive.name+'\n',encoding='ascii')
print('Verified build:', archive)
