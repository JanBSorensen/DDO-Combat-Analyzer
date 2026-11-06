"""Install the pinned Indra Direct recognition model for Windows or Linux.

Model weights are not included with the release. Existing compatible .NET or
Hugging Face installations are reused. Missing weights are downloaded after
SHA-256 verification. --install-runtime also installs ONNX Runtime when missing.
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

from direct_indra_onnx import DirectIndraRecognizer, find_model_dir

REVISION = 'v1.1.0'
RECOGNIZER_SHA256 = '475972a2710be4a3cbfeb2bc7f3624ffd563de07de42476fc7a290d4e1ece484'
SOURCE = 'https://huggingface.co/datalab-to/indra-ocr/resolve/v1.1.0/'
HERE = Path(__file__).resolve().parent


def download(name, destination, sha256=None):
    print('Downloading:', SOURCE + name, flush=True)
    req = urllib.request.Request(SOURCE + name, headers={
        'User-Agent': 'DDO-Combat-Analyzer-Indra-Direct/1.0'})
    digest = hashlib.sha256()
    size = 0
    with urllib.request.urlopen(req, timeout=40) as reader, destination.open('xb') as output:
        while True:
            buf = reader.read(1024 * 1024)
            if not buf:
                break
            size += len(buf)
            if size > 512 * 1024 * 1024:
                raise RuntimeError('Model download unexpectedly large')
            output.write(buf)
            digest.update(buf)
    if sha256 and digest.hexdigest().lower() != sha256.lower():
        raise RuntimeError('Model SHA-256 mismatch; downloaded file rejected')
    print(f'{name}: {size:,} bytes; SHA-256 {digest.hexdigest()}', flush=True)


def main(install_runtime=False):
    if install_runtime and importlib.util.find_spec('onnxruntime') is None:
        print('Installing ONNX Runtime for Direct OCR...', flush=True)
        subprocess.run([sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check',
                        'onnxruntime>=1.19,<2'], check=True, timeout=360)
        importlib.invalidate_caches()
    import onnxruntime as ort
    found = find_model_dir()
    if found is None:
        print('Compatible rec.onnx not found. Downloading pinned model v1.1.0.', flush=True)
        dest = HERE / 'Models' / 'Indra'
        dest.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix='.indra-rec-', dir=dest.parent))
        try:
            download('rec.onnx', stage / 'rec.onnx', RECOGNIZER_SHA256)
            download('meta.json', stage / 'meta.json')
            try:
                download('LICENSE', stage / 'LICENSE')
            except (OSError, ValueError):
                pass
            (stage / 'MODEL_SOURCE.txt').write_text(
                f'Source: {SOURCE}\nRevision: {REVISION}\n'
                'Model license: modified OpenRAIL-M\n', encoding='utf-8')
            from direct_indra_onnx import valid_model_dir
            if not valid_model_dir(stage):
                raise RuntimeError('Downloaded recognizer metadata/model invalid')
            if dest.exists():
                if valid_model_dir(dest):
                    found = dest
                else:
                    raise RuntimeError('Incomplete model directory: ' + str(dest))
            else:
                stage.rename(dest)
                found = dest
        finally:
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)
    print('Indra Direct model directory:', found, flush=True)
    engine = DirectIndraRecognizer(threads=2, model_dir=found)
    print('Indra Direct ready (height={}, window={}, vocabulary={}; ORT={})'.format(
        engine.height, engine.window, len(engine.tokens), ort.__version__), flush=True)


if __name__ == '__main__':
    main(install_runtime='--install-runtime' in sys.argv)
