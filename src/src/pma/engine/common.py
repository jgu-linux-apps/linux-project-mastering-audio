"""Wspólne narzędzia: wczytywanie, głośność głośnych fragmentów, zabezpieczenie szczytów, resampling."""
import math
import os

import numpy as np
import soundfile as sf
from scipy import signal

from . import dsp

CEILING_DBTP = -1.0


DECODE_RATE = 48000      # pliki, których libsndfile nie czyta (M4A, AAC, Opus…), dekoduje ffmpeg do tej częstotliwości


def load(path):
    """WAV / FLAC / AIFF / MP3 / OGG przez libsndfile; reszta (M4A, AAC, Opus, MP4) przez ffmpeg."""
    try:
        x, fs = sf.read(path, dtype="float32", always_2d=True)
    except Exception:  # noqa: BLE001
        from .export import _decode
        x, fs = _decode(path, DECODE_RATE), DECODE_RATE
    if x.shape[1] > 2:
        x = x[:, :2]
    return x, fs


def loud_level(x, fs):
    """Głośność głośnych fragmentów: 95. percentyl short-term (3 s) LUFS."""
    st = dsp.short_term(x, fs, 0.5)
    st = st[st > -70]
    if st.size == 0:
        return -70.0
    return float(np.percentile(st, 95))


def safety_limit(x, fs, ceiling=CEILING_DBTP):
    """Ostateczne zabezpieczenie po zmianie częstotliwości (zwykle nic nie robi)."""
    if dsp.true_peak_db(x) <= ceiling:
        return x
    y, _ = dsp.limiter(x, fs, ceiling - 0.05)
    return y


def to_rate(x, fs, new_fs):
    if fs == new_fs:
        return x
    g = math.gcd(int(fs), int(new_fs))
    return signal.resample_poly(x.astype(np.float64), new_fs // g, fs // g, axis=0).astype(np.float32)


def atomic_write(path, data, fs, subtype="FLOAT"):
    """Zapis do cache przez unikalny plik tymczasowy (pid + losowy sufiks) i os.replace – kilka procesów
    roboczych może liczyć ten sam plik naraz; wcześniej wspólna nazwa .tmp.wav kończyła się błędem."""
    import soundfile as _sf
    import uuid
    tmp = f"{path}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp.wav"
    _sf.write(tmp, data, fs, subtype=subtype)
    os.replace(tmp, path)
