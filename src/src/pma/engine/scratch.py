"""Skrecze (DJ scratch) zsynchronizowane z bitem: wykrywanie tempa podkładu i synteza ruchu „płyty”
tam i z powrotem po krótkiej próbce wokalu (początek zaznaczenia, ok. jedno uderzenie).

Model: pozycja igły p(t) w próbce (sekundy). Prędkość dp/dt = 1 to normalne odtwarzanie, ujemna – do tyłu.
Wysokość dźwięku zmienia się sama (odczyt z interpolacją), a głośność maleje przy wolnym ruchu, jak na winylu.
Crossfader (bramka) robi „transformer” i ucina powroty w „chirp”."""
import hashlib
import os

import numpy as np
import soundfile as sf
from scipy import signal

from .. import log as L

STYLES = ["baby", "chirp", "transformer", "mix"]
CACHE = os.path.join(os.path.expanduser("~/.cache"), "linux-project-mastering-audio", "bpm")


# ------------------------------------------------------------------ tempo
def detect_bpm(path, lo=70.0, hi=180.0):
    """Tempo z obwiedni początków dźwięków (spectral flux) i autokorelacji; wynik w zakresie 70–180 BPM."""
    os.makedirs(CACHE, exist_ok=True)
    key = hashlib.sha1(f"{path}|{os.path.getmtime(path)}|bpm-v1".encode()).hexdigest()[:16]
    cp = os.path.join(CACHE, key)
    if os.path.exists(cp):
        with open(cp) as f:
            return float(f.read())
    from .common import load
    x, fs = load(path)
    m = x.mean(axis=1)
    m = m[: int(fs * 90)]                                   # 90 s wystarczy
    hop, n = 512, 2048
    f, _t, z = signal.stft(m, fs, nperseg=n, noverlap=n - hop, boundary=None, padded=False)
    mag = np.log1p(np.abs(z[(f > 30) & (f < 8000)]) * 100)
    flux = np.maximum(0, np.diff(mag, axis=1)).sum(axis=0)
    flux = flux - signal.medfilt(flux, 31)
    flux = np.maximum(flux, 0)
    flux = (flux - flux.mean()) / (flux.std() + 1e-9)
    ac = signal.correlate(flux, flux, mode="full", method="fft")[len(flux) - 1:]
    frame_s = hop / fs
    lags = np.arange(len(ac)) * frame_s
    best, score = 120.0, -1e9
    for bpm in np.arange(lo, hi + 0.01, 0.5):
        period = 60.0 / bpm
        s = 0.0
        for k, w in ((1, 1.0), (2, 0.5), (4, 0.25), (0.5, 0.5)):  # okres i jego wielokrotności
            i = int(round(period * k / frame_s))
            if 0 < i < len(ac):
                s += w * ac[max(0, i - 1):i + 2].max()
        s *= np.exp(-0.5 * (np.log2(bpm / 110.0) / 1.2) ** 2)     # lekka preferencja typowych temp
        if s > score:
            best, score = bpm, s
    with open(cp, "w") as fh:
        fh.write(f"{best:.1f}")
    L.log(f"{os.path.basename(path)}: tempo {best:.1f} BPM", "ok", tag="scratch")
    return float(best)


# ------------------------------------------------------------------ synteza
def _smooth(u):
    """0..1 -> 0..1, start i koniec z zerową prędkością (ręka zwalnia przy zawracaniu)."""
    return 0.5 - 0.5 * np.cos(np.pi * np.clip(u, 0, 1))


def _motion(style, beat, fs, L_s, beats, seed):
    """Pozycja w próbce (s) i bramka crossfadera (0/1) dla `beats` uderzeń."""
    n_beat = int(round(beat * fs))
    rng = np.random.default_rng(seed)
    pos, gate = [], []
    for k in range(beats):
        st = style if style != "mix" else ["baby", "chirp", "baby", "transformer"][k % 4]
        u = np.arange(n_beat) / n_beat
        if st == "baby":                                     # ósemki: do przodu, do tyłu
            ph = (u * 2) % 1
            fwd = (u * 2) < 1
            p = np.where(fwd, _smooth(ph), 1 - _smooth(ph)) * L_s
            g = np.ones(n_beat)
        elif st == "chirp":                                  # szybko do przodu, crossfader zamyka powrót
            ph = (u * 2) % 1
            p = np.where(ph < 0.5, _smooth(ph * 2), 1 - _smooth((ph - 0.5) * 2)) * L_s * 0.8
            g = (ph < 0.5).astype(float)
        else:                                                # transformer: wolny ruch, bramka szesnastkami
            p = _smooth(u) * L_s if k % 2 == 0 else (1 - _smooth(u)) * L_s
            g = (((u * 4) % 1) < 0.55).astype(float)
        jitter = 1 + 0.06 * rng.standard_normal()           # ręka nigdy nie jest idealna
        pos.append(np.clip(p * jitter, 0, L_s))
        gate.append(g)
    p, g = np.concatenate(pos), np.concatenate(gate)
    ramp = max(1, int(0.003 * fs))                           # 3 ms na przełączeniu crossfadera – bez trzasków
    g = np.convolve(g, np.ones(ramp) / ramp, mode="same")
    return p, g


def render_scratch(sample, fs, bpm, style="mix", beats=4, seed=7):
    """sample: (n, ch) – próbka (początek zaznaczenia). Zwraca skrecz o długości `beats` uderzeń."""
    beat = 60.0 / bpm
    L_s = min(len(sample) / fs, beat * 0.9)                  # zakres ruchu płyty ≤ jedno uderzenie
    p, g = _motion(style, beat, fs, L_s, beats, seed)
    v = np.abs(np.gradient(p) * fs)                          # prędkość (1 = normalnie)
    vol = np.clip(v / 1.2, 0, 1) ** 0.6                      # wolno = ciszej, jak na winylu
    idx = p * fs
    t = np.arange(len(sample))
    out = np.stack([np.interp(idx, t, sample[:, c]) for c in range(sample.shape[1])], axis=1)
    out *= (vol * g)[:, None]
    sos = signal.butter(2, [120, 9000], "bandpass", fs=fs, output="sos")   # charakter „z płyty”
    out = signal.sosfilt(sos, out, axis=0)
    peak = np.abs(out).max()
    ref = np.abs(sample).max()
    if peak > 0:
        out *= min(1.5, ref / peak)                          # poziom jak oryginał
    return out.astype(np.float32)
