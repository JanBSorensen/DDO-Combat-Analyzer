"""Indra Direct OCR: recognition-only ONNX path, shared by Windows and Linux.

Processes the already-isolated DDO Combat Log text rows directly using Indra's
rec.onnx. Indra Full remains selectable as a fallback. Model weights are not
distributed with the application.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent


def candidate_model_dirs():
    """Find the installed or downloaded Indra recognition model."""
    seen = set()

    def emit(root):
        if not root:
            return None
        try:
            p = Path(root).expanduser()
            key = str(p.absolute()).casefold()
            if key not in seen:
                seen.add(key)
                return p
        except (ValueError, OSError):
            pass
        return None

    direct = [
        os.environ.get('INDRA_OCR_MODEL_DIR'), os.environ.get('INDRA_REC_MODEL_DIR'),
        HERE / 'Models' / 'Indra', HERE.parent / 'Models' / 'Indra',
        Path.home() / 'AppData' / 'Local' / 'DdoCombatAnalyzerDotNet' / 'Models' / 'Indra-v1.1.0',
        Path.home() / 'AppData' / 'Local' / 'DdoCombatAnalyzerDotNet' / 'Models' / 'Indra',
        Path.home() / '.cache' / 'huggingface' / 'hub' / 'models--datalab-to--indra-ocr' / 'snapshots',
    ]
    for item in direct:
        p = emit(item)
        if p is None:
            continue
        if p.name == 'snapshots' and p.is_dir():
            for q in sorted(p.iterdir(), reverse=True):
                c = emit(q)
                if c is not None:
                    yield c
        else:
            yield p
    downloads = Path.home() / 'Downloads'
    if downloads.is_dir():
        for glob in ('DDO_Combat_Analyzer_DotNet_*/Models/Indra',
                     'DDO_NET_*/Models/Indra'):
            for q in downloads.glob(glob):
                c = emit(q)
                if c is not None:
                    yield c


def valid_model_dir(path):
    try:
        folder = Path(path)
        rec, meta = folder / 'rec.onnx', folder / 'meta.json'
        if rec.stat().st_size < 1_000_000:
            return False
        obj = json.loads(meta.read_text(encoding='utf-8'))
        vocab = obj['vocab']
        if isinstance(vocab, str):
            vocab = json.loads(vocab)
        return (isinstance(vocab['chars'], list) and len(vocab['chars']) > 1
                and int(vocab['blank']) == 0 and int(obj.get('rec_height', 32)) > 0)
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        return False


def find_model_dir():
    for p in candidate_model_dirs():
        if valid_model_dir(p):
            return p
    return None


def availability():
    """Return (ready, explanation) without importing full Indra or ONNX."""
    import importlib.util
    if importlib.util.find_spec('onnxruntime') is None:
        return False, 'ONNX Runtime is missing. Run platform SETUP or inspect direct_setup.log.'
    p = find_model_dir()
    if p is None:
        return False, 'Indra rec.onnx + meta.json not found. Run platform SETUP or inspect direct_setup.log.'
    return True, f'Direct ONNX model: {p}'


def line_to_tensor(gray, target_height, window, variant=0):
    """Match the .NET recognition-only preprocessor on unmodified row crops."""
    if gray is None or not isinstance(gray, np.ndarray) or gray.ndim != 2 or gray.dtype != np.uint8:
        raise ValueError('Direct ONNX requires a 2D uint8 grayscale line crop')
    h, w = gray.shape
    if h < 2 or w < 8:
        return None
    tlight = {0: 105, 1: 92, 2: 120}.get(variant, 105)
    tdark = {0: 150, 1: 165, 2: 135}.get(variant, 150)
    hpad = {0: .30, 1: .45, 2: .22}.get(variant, .30)
    vpad = {0: .20, 1: .26, 2: .14}.get(variant, .20)
    border = np.concatenate((gray[0, ::4], gray[-1, ::4]))
    dark = float(np.mean(border, dtype=np.float64)) < 115.0
    is_ink = (gray[:, 1:-1] >= tlight) if dark else (gray[:, 1:-1] <= tdark)
    ys, xs = np.nonzero(is_ink)
    if xs.size == 0:
        return None
    left, right = int(xs.min()) + 1, int(xs.max()) + 1
    top, bottom = int(ys.min()), int(ys.max())
    if right - left < 6:
        return None
    ink_h = float(bottom - top + 1)
    out_h = int(target_height) * int(window)
    pad_y = (window * (1.0 + 2.0 * vpad) - 1.0) / 2.0
    x0 = math.floor(left - hpad * ink_h)
    x1 = math.ceil(right + 1 + hpad * ink_h)
    y0 = math.floor(top - pad_y * ink_h)
    y1 = math.ceil(bottom + 1 + pad_y * ink_h)
    crop_w, crop_h = max(1, x1 - x0), max(1, y1 - y0)
    used_w = max(8, min(4096, round(crop_w * out_h / crop_h)))
    padded_w = ((used_w + 7) // 8) * 8
    xx = x0 + (np.arange(used_w, dtype=np.float32) + .5) * (crop_w / used_w) - .5
    yy = y0 + (np.arange(out_h, dtype=np.float32) + .5) * (crop_h / out_h) - .5
    mx, my = np.meshgrid(xx, yy)
    sample = cv2.remap(gray, mx, my, interpolation=cv2.INTER_LINEAR,
                       borderMode=cv2.BORDER_CONSTANT, borderValue=(0 if dark else 255))
    tensor = np.zeros((out_h, padded_w), dtype=np.float32)
    tensor[:, :used_w] = sample.astype(np.float32) / 255.0 if dark else (255.0 - sample) / 255.0
    return tensor, used_w


class DirectIndraRecognizer:
    """Indra's recognition-only CTC model with the same I/O as .NET."""

    def __init__(self, threads=2, model_dir=None, session=None):
        import onnxruntime as ort
        folder = Path(model_dir) if model_dir is not None else find_model_dir()
        if folder is None or not valid_model_dir(folder):
            raise FileNotFoundError('No complete Indra rec.onnx / meta.json; run SETUP.')
        meta = json.loads((folder / 'meta.json').read_text(encoding='utf-8'))
        vocab = meta['vocab']
        if isinstance(vocab, str):
            vocab = json.loads(vocab)
        if int(vocab['blank']) != 0:
            raise ValueError('Expected CTC blank index 0')
        self.tokens = [str(char) for char in vocab['chars']]
        self.height = max(16, min(128, int(meta.get('rec_height', 32))))
        self.window = max(1, min(5, int(meta.get('rec_window', 1))))
        options = ort.SessionOptions()
        options.intra_op_num_threads = max(1, min(8, int(threads)))
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.session = session if session is not None else ort.InferenceSession(
            str(folder / 'rec.onnx'), sess_options=options, providers=['CPUExecutionProvider'])
        self.model_dir = str(folder)

    def recognize_batch(self, lines, variant=0):
        if not lines:
            return []
        prepared = [line_to_tensor(crop, self.height, self.window, variant) for crop in lines]
        texts = [''] * len(prepared)
        valid = [i for i, crop in enumerate(prepared) if crop is not None]
        valid.sort(key=lambda i: prepared[i][1])
        for start in range(0, len(valid), 12):
            group = valid[start:start+12]
            max_width = max(prepared[i][0].shape[1] for i in group)
            x = np.zeros((len(group), 1, self.height * self.window, max_width), dtype=np.float32)
            widths = np.empty((len(group),), dtype=np.int64)
            for pos, index in enumerate(group):
                tensor, used_width = prepared[index]
                x[pos, 0, :, :tensor.shape[1]] = tensor
                widths[pos] = used_width
            ids, confidence, lengths = self.session.run(['ids', 'conf', 'lengths'],
                                                          {'x': x, 'widths': widths})
            ids = np.asarray(ids)
            confidence = np.asarray(confidence)
            lengths = np.asarray(lengths).reshape(-1)
            for pos, index in enumerate(group):
                chars, scores, prev = [], [], -1
                for t in range(min(int(lengths[pos]), ids.shape[1])):
                    symbol = int(ids[pos, t])
                    if 0 < symbol <= len(self.tokens) and symbol != prev:
                        chars.append(self.tokens[symbol - 1])
                        scores.append(float(confidence[pos, t]))
                    prev = symbol
                mean_confidence = sum(scores) / len(scores) if scores else 0.0
                texts[index] = ''.join(chars) if mean_confidence >= 0.5 else ''
        return texts

    def recognize_variants(self, line):
        results = []
        for variant in (0, 1, 2):
            try:
                text = self.recognize_batch([line], variant=variant)[0]
            except Exception:
                continue
            if text and text not in results:
                results.append(text)
        return results
