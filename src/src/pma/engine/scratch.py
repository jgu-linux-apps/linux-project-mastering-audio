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

STYLES = ["baby", "chirp", "transformer", "mix"]          # gatunek „Podstawowe”
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


def render_scratch(sample, fs, bpm, style="mix", beats=4, seed=7, max_range=None):
    """sample: (n, ch) – próbka (początek zaznaczenia albo całe zaznaczenie). Zwraca skrecz o długości `beats` uderzeń.
    max_range: zakres ruchu płyty w sekundach (domyślnie ≤ jedno uderzenie; „całe zaznaczenie” – cała próbka)."""
    if style in PATTERNS:                                    # wzorzec gatunku (siatka 16 kroków)
        return render_pattern(sample, fs, bpm, style, beats, seed, max_range)
    beat = 60.0 / bpm
    L_s = min(len(sample) / fs, max_range if max_range else beat * 0.9)
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


# ------------------------------------------------------------------ gotowe źródła skreczu (syntetyczne)
SOURCES = ["ahh", "stab", "fresh", "tone", "kick"]
OUT_DIR = os.path.join(os.path.expanduser("~/.local/share"), "linux-project-mastering-audio", "scratches")


def synth_source(kind, fs, beat):
    """Krótki dźwięk do skreczowania (ok. jedno uderzenie), wygenerowany – bez cudzych sampli."""
    n = int(beat * fs)
    t = np.arange(n) / fs
    rng = np.random.default_rng(3)
    env = np.minimum(1, t / 0.008) * np.exp(-t / (beat * 0.45))
    if kind == "ahh":                                        # samogłoska „a” – piła przez formanty
        f0 = 210 * (1 - 0.06 * t / beat)
        src = signal.sawtooth(2 * np.pi * np.cumsum(f0) / fs)
        x = np.zeros(n)
        for fc, bw, g in ((750, 130, 1.0), (1150, 150, 0.5), (2500, 220, 0.12)):
            x += g * signal.sosfilt(signal.butter(2, [fc - bw, fc + bw], "bandpass", fs=fs, output="sos"), src)
    elif kind == "stab":                                     # akord molowy, jasny atak
        x = sum(signal.sawtooth(2 * np.pi * f * t) * w for f, w in ((220, 1), (261.6, 0.8), (329.6, 0.7), (440, 0.5)))
        x = signal.sosfilt(signal.butter(2, 2500, fs=fs, output="sos"), x)
        env = np.minimum(1, t / 0.004) * np.exp(-t / (beat * 0.3))
    elif kind == "fresh":                                    # „ffff” (szum) przechodzące w „esh”
        noise = signal.sosfilt(signal.butter(2, [1500, 4000], "bandpass", fs=fs, output="sos"), rng.standard_normal(n))
        vow = signal.sosfilt(signal.butter(2, [500, 2200], "bandpass", fs=fs, output="sos"),
                             signal.sawtooth(2 * np.pi * 180 * t))
        cut = int(0.12 * fs)
        x = np.concatenate([noise[:cut] * 0.3, vow[cut:]])
    elif kind == "tone":                                     # miękki ton (440 Hz + oktawa) – nie świdruje w uszach
        x = np.sin(2 * np.pi * 440 * t) + 0.25 * np.sin(2 * np.pi * 880 * t)
        env = np.minimum(1, t / 0.003) * np.minimum(1, (beat - t) / 0.01)
    else:                                                    # kick
        f = 45 + 110 * np.exp(-t / 0.03)
        x = np.sin(2 * np.pi * np.cumsum(f) / fs)
        env = np.exp(-t / 0.18)
    x = x * env
    x /= np.abs(x).max() + 1e-9
    return (np.stack([x, x], axis=1) * 0.7).astype(np.float32)


def soften(y, fs):
    """Presety bez ostrej góry: dolnoprzepustowy ~3,8 kHz, łagodne nasycenie, szczyt −6 dBFS."""
    y = signal.sosfilt(signal.butter(2, 3800, fs=fs, output="sos"), y, axis=0)
    y = signal.sosfilt(signal.butter(1, 90, "highpass", fs=fs, output="sos"), y, axis=0)
    pk = np.abs(y).max()
    if pk > 0:
        y = np.tanh(1.3 * y / pk) / np.tanh(1.3)              # ciepło, bez twardych szczytów
    return (y * 0.5).astype(np.float32)


def render_preset(kind, style, bpm, beats, fs=48000):
    """Gotowy plik skreczu z presetu (źródło + styl) w tempie bitu. Zwraca ścieżkę WAV (trwały katalog)."""
    os.makedirs(OUT_DIR, exist_ok=True)
    name = f"scratch_{kind}_{style}_{bpm:.1f}bpm_{beats}b_v2.wav"
    out = os.path.join(OUT_DIR, name)
    if not os.path.exists(out):
        beat = 60.0 / bpm
        smp = synth_source(kind, fs, beat)
        y = soften(render_scratch(smp, fs, bpm, style, beats, seed=sum(map(ord, kind))), fs)
        from .common import atomic_write
        atomic_write(out, y, fs, "FLOAT")
        L.log(f"skrecz z presetu: {kind}, {style}, {bpm:.1f} BPM, {beats} uderzeń", "ok", tag="scratch")
    return out


# ------------------------------------------------------------------ siatka bitu: tempo + faza + „raz”
def _flux(m, fs, lo_hz, hi_hz, hop=512, n=2048):
    f, _t, z = signal.stft(m, fs, nperseg=n, noverlap=n - hop, boundary=None, padded=False)
    mag = np.log1p(np.abs(z[(f > lo_hz) & (f < hi_hz)]) * 100)
    fl = np.maximum(0, np.diff(mag, axis=1)).sum(axis=0)
    fl = np.maximum(fl - signal.medfilt(fl, 31), 0)
    return fl / (fl.std() + 1e-9), hop / fs


def detect_grid(path, genre="basic"):
    """(bpm, beat0, bar0): tempo, czas pierwszego uderzenia i pierwszego „raz” (najmocniejsza stopa) w sekundach.
    Dla RAP / HIP-HOP tempo sprowadzane do 80–110 BPM (połowa / podwojenie – typowa pomyłka o oktawę)."""
    import json
    os.makedirs(CACHE, exist_ok=True)
    key = hashlib.sha1(f"{path}|{os.path.getmtime(path)}|{genre}|grid-v2".encode()).hexdigest()[:16]
    cp = os.path.join(CACHE, key + ".json")
    if os.path.exists(cp):
        with open(cp) as f:
            d = json.load(f)
        return d["bpm"], d["beat0"], d["bar0"]
    bpm = detect_bpm(path)
    if genre == "rap":
        while bpm > 115:
            bpm /= 2
        while bpm < 75:
            bpm *= 2
    from .common import load
    x, fs = load(path)
    m = x.mean(axis=1)[: int(fs * 90)]
    fl, fr = _flux(m, fs, 30, 8000)
    low, _ = _flux(m, fs, 30, 160)                         # stopa – do znalezienia „raz”
    period = 60.0 / bpm / fr                                # w ramkach
    k = np.arange(int(len(fl) / period))
    best, bp = -1, 0.0
    for p in np.arange(0, period, 0.5):                     # faza: gdzie trafiają uderzenia
        idx = np.round(p + k * period).astype(int)
        idx = idx[idx < len(fl)]
        sc = fl[idx].sum()
        if sc > best:
            best, bp = sc, p
    beat_scores = []
    for j in range(4):                                      # który z 4 uderzeń to „raz” (najwięcej stopy)
        idx = np.round(bp + (j + 4 * np.arange(int(len(low) / (4 * period)))) * period).astype(int)
        idx = idx[idx < len(low)]
        beat_scores.append(low[idx].sum() if len(idx) else 0)
    beat0 = bp * fr + (2048 - 512) / fs                     # okno STFT: przyrost widma pojawia się o (n − hop) wcześniej
    bar0 = beat0 + int(np.argmax(beat_scores)) * 60.0 / bpm
    with open(cp, "w") as fh:
        json.dump(dict(bpm=round(bpm, 2), beat0=beat0, bar0=bar0), fh)
    L.log(f"{os.path.basename(path)}: siatka {bpm:.1f} BPM, uderzenie {beat0:.3f} s, „raz” {bar0:.3f} s", "ok",
          tag="scratch")
    return round(bpm, 2), beat0, bar0


def snap_to_grid(t, bpm, beat0, bar0, to="beat"):
    """Najbliższe uderzenie (albo początek taktu) od czasu t (s), nie wcześniej niż t − pół uderzenia."""
    step = 60.0 / bpm * (4 if to == "bar" else 1)
    ref = bar0 if to == "bar" else beat0
    n = round((t - ref) / step)
    return max(0.0, ref + n * step)


# ------------------------------------------------------------------ wzorce na siatce 16 kroków (1 takt = 16 szesnastek)
# zdarzenie: (krok startu, długość w krokach, ruch). Ruchy: patrz _move().
GENRES = {
    "basic": ["baby", "chirp", "transformer", "mix"],
    "rap": ["rap_boombap", "rap_chirps", "rap_transformer", "rap_tear", "rap_flare", "rap_scribble", "rap_drop"],
}
PATTERNS = {
    "rap_boombap": [(0, 2, "baby"), (2, 2, "baby"), (4, 4, "baby"), (8, 2, "baby"), (10, 2, "baby"), (12, 4, "baby")],
    "rap_chirps": [(0, 1, "chirp"), (2, 1, "chirp"), (3, 1, "chirp"), (6, 2, "chirp"), (8, 1, "chirp"),
                   (10, 1, "chirp"), (11, 1, "chirp"), (14, 2, "chirp")],
    "rap_transformer": [(0, 8, "transform"), (8, 8, "transform_back")],
    "rap_tear": [(0, 4, "tear"), (4, 4, "tear"), (8, 4, "tear"), (12, 2, "baby"), (14, 2, "baby")],
    "rap_flare": [(0, 4, "flare"), (4, 4, "flare"), (8, 4, "flare"), (12, 4, "flare")],
    "rap_scribble": [(0, 4, "fwd"), (4, 4, "back"), (8, 2, "baby"), (10, 2, "baby"), (12, 4, "scribble")],
    "rap_drop": [(0, 3, "fwd"), (13, 3, "back")],
}


def _move(kind, n, step_n):
    """Pozycja (0..1 zakresu płyty) i bramka crossfadera dla jednego ruchu o długości n próbek."""
    u = np.arange(n) / max(1, n)
    g = np.ones(n)
    if kind == "fwd":
        p = _smooth(u)
    elif kind == "back":
        p = 1 - _smooth(u)
    elif kind == "baby":
        p = np.where(u < 0.5, _smooth(u * 2), 1 - _smooth((u - 0.5) * 2))
    elif kind == "chirp":                                  # do przodu otwarty, powrót zamknięty
        p = np.where(u < 0.5, _smooth(u * 2), 1 - _smooth((u - 0.5) * 2))
        g = (u < 0.5).astype(float)
    elif kind in ("transform", "transform_back"):          # wolny ruch, crossfader klika szesnastkami / 2
        p = _smooth(u) if kind == "transform" else 1 - _smooth(u)
        g = ((np.arange(n) // max(1, step_n // 2)) % 2 == 0).astype(float)
    elif kind == "tear":                                   # do przodu, powrót w dwóch szarpnięciach
        p = np.where(u < 0.4, _smooth(u / 0.4),
                     np.where(u < 0.7, 1 - 0.5 * _smooth((u - 0.4) / 0.3), 0.5 - 0.5 * _smooth((u - 0.7) / 0.3)))
    elif kind == "flare":                                  # baby z jednym kliknięciem crossfadera w każdą stronę
        p = np.where(u < 0.5, _smooth(u * 2), 1 - _smooth((u - 0.5) * 2))
        g = 1 - (((u > 0.2) & (u < 0.3)) | ((u > 0.7) & (u < 0.8))).astype(float)
    elif kind == "scribble":                               # drobne, szybkie drgania (trzydziestki dwójki)
        p = 0.2 + 0.12 * np.sin(2 * np.pi * u * (n / max(1, step_n / 2)))
    else:
        p, g = np.zeros(n), np.zeros(n)
    return p, g


def render_pattern(sample, fs, bpm, pattern, beats, seed=7, max_range=None):
    """Wzorzec z PATTERNS zsynchronizowany z siatką: `beats` uderzeń (takty po 4), pierwsza próbka = „raz”."""
    beat = 60.0 / bpm
    step_n = int(round(beat * fs / 4))
    total = int(round(beats * beat * fs))
    L_s = min(len(sample) / fs, max_range if max_range else beat * 0.9)
    pos, gate = np.zeros(total), np.zeros(total)
    rng = np.random.default_rng(seed)
    bars = int(np.ceil(beats / 4))
    for b in range(bars):
        for st, ln, kind in PATTERNS[pattern]:
            a = b * 16 * step_n + st * step_n
            n = ln * step_n
            if a >= total:
                continue
            p, g = _move(kind, n, step_n)
            p = np.clip(p * (1 + 0.04 * rng.standard_normal()), 0, 1)
            e = min(total, a + n)
            pos[a:e], gate[a:e] = p[:e - a], g[:e - a]
    ramp = max(1, int(0.003 * fs))
    gate = np.convolve(gate, np.ones(ramp) / ramp, mode="same")
    return _play(sample, fs, pos * L_s, gate)


def _play(sample, fs, p, g):
    """Odczyt próbki według pozycji igły p (s) i bramki g – wspólne dla stylów i wzorców."""
    v = np.abs(np.gradient(p) * fs)
    vol = np.clip(v / 1.2, 0, 1) ** 0.6
    t = np.arange(len(sample))
    out = np.stack([np.interp(p * fs, t, sample[:, c]) for c in range(sample.shape[1])], axis=1)
    out *= (vol * g)[:, None]
    out = signal.sosfilt(signal.butter(2, [120, 9000], "bandpass", fs=fs, output="sos"), out, axis=0)
    peak, ref = np.abs(out).max(), np.abs(sample).max()
    if peak > 0:
        out *= min(1.5, ref / peak)
    return out.astype(np.float32)
