"""Analizy i podglądy zakładki Album: widmo w pasmach 1/3 oktawy (na żywo w A/B i średnie),
porównanie z utworem referencyjnym, odsłuch „jak po kodeku” (AAC / Opus / Vorbis)
oraz cały album jako jeden plik z przerwami lub przenikaniem + arkusz CUE."""
import hashlib
import os
import subprocess

import numpy as np
import soundfile as sf

from . import dsp
from .. import log as L

CACHE = os.path.join(os.path.expanduser("~/.cache"), "linux-project-mastering-audio")

# środki pasm 1/3 oktawy 25 Hz – 20 kHz (ISO 266)
BANDS = np.array([25, 31.5, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630, 800, 1000, 1250, 1600,
                  2000, 2500, 3150, 4000, 5000, 6300, 8000, 10000, 12500, 16000, 20000], dtype=np.float64)
# trzy szerokie zakresy do opisu barwy słowami
RANGES = {"low": (20, 250), "mid": (250, 4000), "high": (4000, 20000)}


def _load_any(path):
    from .common import load
    try:
        return load(path)
    except Exception:  # noqa: BLE001  (MP3 / MP4 – przez ffmpeg)
        from .export import _decode
        return _decode(path), 44100


def _band_matrix(freqs):
    """Macierz (pasma × biny FFT): każdy bin należy do pasma 1/3 oktawy wokół środka."""
    lo, hi = BANDS * 2 ** (-1 / 6), BANDS * 2 ** (1 / 6)
    m = ((freqs[None, :] >= lo[:, None]) & (freqs[None, :] < hi[:, None])).astype(np.float32)
    return m


def band_frames(x, fs, hop_s=0.1, win_s=0.4):
    """Widmo w pasmach 1/3 oktawy co `hop_s` (okno `win_s`) w dB względem pełnej skali, kanały uśrednione.
    Zwraca tablicę (ramki × pasma) float16 – mała nawet dla długich utworów."""
    m = np.asarray(x, dtype=np.float32)
    m = m.mean(axis=1) if m.ndim == 2 else m
    n = 1 << int(np.ceil(np.log2(win_s * fs)))
    hop = max(1, int(hop_s * fs))
    win = np.hanning(n).astype(np.float32)
    norm = (win.sum() / 2) ** 2
    freqs = np.fft.rfftfreq(n, 1 / fs)
    bm = _band_matrix(freqs)
    empty = bm.sum(axis=1) == 0                  # pasma powyżej Nyquista albo bez binów
    pad = np.concatenate([np.zeros(n // 2, np.float32), m, np.zeros(n, np.float32)])
    frames = max(1, int(np.ceil(len(m) / hop)))
    out = np.empty((frames, len(BANDS)), np.float32)
    step = 512                                     # w paczkach – mało pamięci
    for s in range(0, frames, step):
        idx = np.arange(s, min(frames, s + step))
        seg = np.lib.stride_tricks.sliding_window_view(pad, n)[idx * hop]
        p = np.abs(np.fft.rfft(seg * win, axis=1)) ** 2 / norm
        out[idx] = 10 * np.log10(p @ bm.T + 1e-12)
    out[:, empty] = -120.0
    return np.maximum(out, -120.0).astype(np.float16)


def average_spectrum(frames, floor_db=-70.0):
    """Średnie widmo z pominięciem cichych ramek (cisza między utworami nie zaniża wyniku)."""
    f = np.asarray(frames, dtype=np.float32)
    if not len(f):
        return np.full(len(BANDS), -120.0)
    total = 10 * np.log10((10 ** (f / 10)).sum(axis=1) + 1e-12)
    keep = total > max(floor_db, total.max() - 40)
    f = f[keep] if keep.any() else f
    return 10 * np.log10((10 ** (f / 10)).mean(axis=0) + 1e-12)


def tilt(spec_db):
    """Widmo wyrównane do średniej z pasm 200 Hz – 5 kHz: porównujemy barwę, nie głośność."""
    s = np.asarray(spec_db, dtype=np.float64)
    ref = (BANDS >= 200) & (BANDS <= 5000)
    return s - s[ref].mean()


def range_levels(spec_db):
    """Poziom (dB, po wyrównaniu) w zakresach low / mid / high."""
    s = tilt(spec_db)
    out = {}
    for k, (lo, hi) in RANGES.items():
        sel = (BANDS >= lo) & (BANDS < hi) & (s > -100)
        out[k] = float(10 * np.log10((10 ** (s[sel] / 10)).mean())) if sel.any() else 0.0
    return out


# ------------------------------------------------------------------ utwór referencyjny
def analyze_reference(path):
    """Pomiar utworu wzorcowego: LUFS, refren, LRA, true peak i średnie widmo."""
    from .common import loud_level
    x, fs = _load_any(path)
    r = dict(path=path, lufs=dsp.integrated_lufs(x, fs), loud=loud_level(x, fs), lra=dsp.loudness_range(x, fs),
             true_peak=dsp.true_peak_db(x), spectrum=average_spectrum(band_frames(x, fs, 0.25, 0.4)).tolist(),
             duration=len(x) / fs)
    L.log(f"referencja {os.path.basename(path)}: {r['lufs']:.1f} LUFS, LRA {r['lra']:.1f}, "
          f"szczyt {r['true_peak']:+.1f} dBTP", "ok", tag="ref")
    return r


def track_spectrum(path):
    x, fs = _load_any(path)
    return average_spectrum(band_frames(x, fs, 0.25, 0.4)).tolist()


def compare(ref, album_infos, album_spectra):
    """Porównanie albumu (średnia utworów) z referencją + podpowiedzi korekty.
    album_infos: lista NormInfo; album_spectra: lista widm (dB) tych utworów."""
    lufs = float(np.mean([i.lufs for i in album_infos]))
    lra = float(np.mean([i.lra for i in album_infos]))
    tp = float(max(i.true_peak for i in album_infos))
    p = np.mean([10 ** (np.asarray(s) / 10) for s in album_spectra], axis=0)
    spec = 10 * np.log10(p + 1e-12)
    ra, rr = range_levels(spec), range_levels(ref["spectrum"])
    tone = {k: ra[k] - rr[k] for k in RANGES}             # + = album ma tego więcej niż referencja
    hints = []
    if abs(lufs - ref["lufs"]) >= 1.0:
        hints.append(("level", ref["lufs"]))
    if tone["high"] <= -1.5:
        hints.append(("darker", -tone["high"]))
    elif tone["high"] >= 1.5:
        hints.append(("brighter", tone["high"]))
    if tone["low"] <= -1.5:
        hints.append(("less_bass", -tone["low"]))
    elif tone["low"] >= 1.5:
        hints.append(("more_bass", tone["low"]))
    if lra - ref["lra"] >= 2.0:
        hints.append(("more_dynamic", lra - ref["lra"]))
    elif ref["lra"] - lra >= 2.0:
        hints.append(("less_dynamic", ref["lra"] - lra))
    return dict(lufs=lufs, lra=lra, true_peak=tp, spectrum=spec.tolist(), tone=tone, hints=hints,
                master=suggest_master(tone, lra - ref["lra"]))


def suggest_master(tone, lra_diff):
    """Który styl masteringu najbardziej zbliży album do referencji (klucze z master.ORDER)."""
    if lra_diff >= 2.5:
        return "punch" if tone["low"] < 0 else "modern"
    if tone["high"] <= -1.5:
        return "modern"
    if tone["high"] >= 1.5 or tone["low"] <= -1.5:
        return "warm"
    if abs(tone["low"]) < 1.5 and abs(tone["high"]) < 1.5:
        return "gentle"
    return "gentle"


# ------------------------------------------------------------------ odsłuch po kodeku
CODECS = {
    "aac": dict(args=["-c:a", "aac", "-b:a", "256k"], ext="m4a", label="AAC 256 kbps"),
    "opus": dict(args=["-c:a", "libopus", "-b:a", "160k"], ext="opus", label="Opus 160 kbps"),
    "vorbis": dict(args=["-c:a", "libvorbis", "-b:a", "320k"], ext="ogg", label="Ogg Vorbis 320 kbps"),
    "mp3": dict(args=["-c:a", "libmp3lame", "-b:a", "128k"], ext="mp3", label="MP3 128 kbps"),
}
CODEC_ORDER = ["aac", "opus", "vorbis", "mp3"]


def codec_preview(path, codec):
    """Koduje plik B tak jak serwis streamingowy i dekoduje z powrotem do WAV (cache).
    Zwraca dict(path, true_peak po kodeku, lufs)."""
    from .export import FFMPEG
    c = CODECS[codec]
    key = hashlib.sha1(f"{path}|{os.path.getmtime(path)}|{codec}|codec-v1".encode()).hexdigest()[:16]
    d = os.path.join(CACHE, "codec")
    os.makedirs(d, exist_ok=True)
    enc, out = os.path.join(d, f"{key}.{c['ext']}"), os.path.join(d, f"{key}.wav")
    if not os.path.exists(out):
        subprocess.run([FFMPEG, "-v", "error", "-y", "-i", path, "-map", "0:a:0", "-ar", "48000"] + c["args"] + [enc],
                       check=True, capture_output=True)
        tmp = f"{out}.{os.getpid()}.tmp.wav"
        subprocess.run([FFMPEG, "-v", "error", "-y", "-i", enc, "-c:a", "pcm_f32le", tmp],
                       check=True, capture_output=True)
        os.replace(tmp, out)
        try:
            os.remove(enc)
        except OSError:
            pass
    y, fs = sf.read(out, dtype="float32", always_2d=True)
    r = dict(path=out, true_peak=dsp.true_peak_db(y), lufs=dsp.integrated_lufs(y, fs), label=c["label"])
    L.log(f"kodek {c['label']}: {os.path.basename(path)} -> szczyt po dekodowaniu {r['true_peak']:+.2f} dBTP",
          "warn" if r["true_peak"] > 0 else "info", tag="codec")
    return r


# ------------------------------------------------------------------ cały album w jednym pliku + CUE
def _frames(sec):
    """Pozycja w ramkach CD (75 na sekundę) -> MM:SS:FF."""
    f = int(round(sec * 75))
    return f"{f // 4500:02d}:{f // 75 % 60:02d}:{f % 75:02d}"


def cue_text(wav_name, tracks, meta):
    """tracks: lista dict(title, start, pregap_start) – czasy w sekundach od początku pliku."""
    q = lambda s: str(s).replace('"', "'")  # noqa: E731
    lines = []
    if meta.get("artist"):
        lines.append(f'PERFORMER "{q(meta["artist"])}"')
    if meta.get("album"):
        lines.append(f'TITLE "{q(meta["album"])}"')
    lines.append(f'FILE "{q(wav_name)}" WAVE')
    for n, t in enumerate(tracks, 1):
        lines.append(f"  TRACK {n:02d} AUDIO")
        lines.append(f'    TITLE "{q(t["title"])}"')
        if meta.get("artist"):
            lines.append(f'    PERFORMER "{q(meta["artist"])}"')
        if t.get("pregap_start") is not None and t["pregap_start"] < t["start"]:
            lines.append(f"    INDEX 00 {_frames(t['pregap_start'])}")
        lines.append(f"    INDEX 01 {_frames(t['start'])}")
    return "\n".join(lines) + "\n"


def album_image(paths, titles, out_path, gap_s, xfade_s, quality, meta, ceil):
    """Skleja znormalizowane utwory w jeden plik WAV: przerwy (cisza) albo przenikanie (equal-power)
    + arkusz CUE obok. quality: 'cd' (16 bit / 44,1 kHz z ditherem) lub 'hires' (24 bit)."""
    from .common import safety_limit, to_rate
    from .export import tpdf_16
    loaded = [_load_any(p) for p in paths]
    fs = 44100 if quality == "cd" else max(f for _x, f in loaded)
    xs = [to_rate(x, f, fs).astype(np.float32) for x, f in loaded]
    xs = [np.repeat(x, 2, axis=1) if x.shape[1] == 1 else x for x in xs]
    xf = int(max(0.0, xfade_s) * fs)
    gap = int(max(0.0, gap_s) * fs) if xf == 0 else 0
    starts, pregaps = [], []
    total = 0
    for i, x in enumerate(xs):
        if i == 0:
            starts.append(0)
            pregaps.append(None)
            total = len(x)
            continue
        o = min(xf, len(x) // 2, len(xs[i - 1]) // 2)
        pregaps.append(total if gap else None)
        s = total + gap - o
        starts.append(s)
        total = s + len(x)
    y = np.zeros((total, 2), np.float32)
    for i, x in enumerate(xs):
        s = starts[i]
        x = x.copy()
        if xf and i > 0:                      # wejście: sin, wyjście poprzedniego: cos (stała moc)
            o = min(xf, len(x) // 2, len(xs[i - 1]) // 2)
            if o:
                x[:o] *= np.sin(np.linspace(0, np.pi / 2, o, dtype=np.float32))[:, None]
        if xf and i < len(xs) - 1:
            o = min(xf, len(x) // 2, len(xs[i + 1]) // 2)
            if o:
                x[-o:] *= np.cos(np.linspace(0, np.pi / 2, o, dtype=np.float32))[:, None]
        y[s:s + len(x)] += x
    y = safety_limit(y, fs, ceil)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    if quality == "cd":
        sf.write(out_path, tpdf_16(y), fs, subtype="PCM_16")
    else:
        sf.write(out_path, y, fs, subtype="PCM_24")
    cue = os.path.splitext(out_path)[0] + ".cue"
    tracks = [dict(title=t, start=s / fs, pregap_start=(p / fs if p is not None else None))
              for t, s, p in zip(titles, starts, pregaps)]
    with open(cue, "w", encoding="utf-8") as f:
        f.write(cue_text(os.path.basename(out_path), tracks, meta))
    L.log(f"album w jednym pliku: {os.path.basename(out_path)} ({total / fs / 60:.1f} min, "
          f"{'przenikanie %.1f s' % xfade_s if xf else 'przerwy %.1f s' % gap_s}) + CUE", "ok", tag="album")
    return dict(path=out_path, cue=cue, seconds=total / fs, true_peak=dsp.true_peak_db(y))
