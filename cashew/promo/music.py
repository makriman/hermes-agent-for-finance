"""Cashew promo v2 soundtrack — upbeat product-demo track, composed from
scratch (numpy): 112 BPM four-on-the-floor, plucky arps, claps, sidechain pump,
impacts on scene cuts, resolving outro. License-clean by construction.
Output: promo/audio.wav (stereo)
"""
import numpy as np
import wave
from pathlib import Path

SR = 44100
DUR = 40.5
BPM = 112.0
BEAT = 60.0 / BPM            # 0.5357s
BAR = 4 * BEAT
N = int(SR * DUR)
t = np.arange(N) / SR

GROOVE_A = 2.4               # chat starts
GROOVE_B = 23.2              # asks section — lift
OUTRO = 35.8                 # ending card — drums stop, pad resolves

# C major, feel-good loop: C - G - Am - F  (root, third, fifth [, seventh])
CHORDS = [
    [130.81, 164.81, 196.00],            # C
    [98.00, 123.47, 146.83],             # G
    [110.00, 130.81, 164.81],            # Am
    [87.31, 110.00, 130.81, 174.61],     # F(add9-ish)
]


def place(buf, sig, at):
    i0 = int(at * SR)
    if i0 >= len(buf):
        return
    n = min(len(sig), len(buf) - i0)
    buf[i0:i0 + n] += sig[:n]


def pluck(freq, dur=0.22, amp=1.0, bright=1.0):
    n = int(dur * SR)
    tt = np.arange(n) / SR
    s = (np.sin(2 * np.pi * freq * tt)
         + 0.5 * bright * np.sin(2 * np.pi * freq * 2 * tt)
         + 0.22 * bright * np.sin(2 * np.pi * freq * 3 * tt))
    return s * np.exp(-tt * 16) * amp


def kick_hit():
    n = int(0.22 * SR)
    tt = np.arange(n) / SR
    f = 120 * np.exp(-tt * 22) + 45
    return np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-tt * 15)


def clap_hit(rng):
    n = int(0.16 * SR)
    tt = np.arange(n) / SR
    noise = rng.normal(0, 1, n)
    body = noise * np.exp(-tt * 30)
    for off in (0.012, 0.024):                       # classic multi-tap clap
        k = int(off * SR)
        body[k:] += noise[:-k] * np.exp(-tt[:-k] * 34) * 0.7
    return body


def hat_hit(rng, dur=0.045, amp=1.0):
    n = int(dur * SR)
    s = rng.normal(0, 1, n) * np.exp(-np.arange(n) / (0.006 * SR))
    return np.diff(s, prepend=0.0) * amp             # crude highpass


def impact(rng):
    n = int(1.0 * SR)
    tt = np.arange(n) / SR
    boom = np.sin(2 * np.pi * (70 * np.exp(-tt * 6) + 38) * tt) * np.exp(-tt * 4)
    air = rng.normal(0, 1, n) * np.exp(-tt * 8) * 0.4
    return boom + air


def riser(dur, rng):
    n = int(dur * SR)
    tt = np.arange(n) / SR
    amp_env = (tt / dur) ** 2.2
    noise = rng.normal(0, 1, n) * amp_env * 0.5
    sweep = np.sin(2 * np.pi * np.cumsum(180 + 900 * (tt / dur) ** 2) / SR) * amp_env * 0.35
    return noise + sweep


rng = np.random.default_rng(11)
drums = np.zeros(N)
music = np.zeros(N)

# ---- riser into the first drop ----------------------------------------------
place(drums, riser(GROOVE_A, rng) * 0.7, 0.0)
place(drums, impact(rng) * 0.9, GROOVE_A)
place(drums, impact(rng) * 0.9, GROOVE_B)
place(drums, impact(rng) * 0.8, OUTRO)

# ---- groove -------------------------------------------------------------------
kick_times = []
beat_t = GROOVE_A
while beat_t < OUTRO:
    kick_times.append(beat_t)
    place(drums, kick_hit() * 0.85, beat_t)
    if (round((beat_t - GROOVE_A) / BEAT) % 4) in (1, 3):      # clap on 2 & 4
        place(drums, clap_hit(rng) * 0.30, beat_t)
    beat_t += BEAT
h = GROOVE_A
while h < OUTRO:
    vel = 0.16 if (round((h - GROOVE_A) / (BEAT / 2)) % 2) else 0.06
    place(drums, hat_hit(rng, amp=vel), h)
    h += BEAT / 2
h = GROOVE_B                                                    # 16th hats in B
while h < OUTRO:
    place(drums, hat_hit(rng, dur=0.03, amp=0.05), h + BEAT / 4)
    h += BEAT / 2

# ---- musical layers -------------------------------------------------------------
bar_i = 0
bt = GROOVE_A
while bt < OUTRO:
    chord = CHORDS[bar_i % 4]
    root = chord[0]
    # offbeat bass (8ths on the "and")
    for k in range(4):
        place(music, pluck(root / 2, 0.30, 1.5, bright=0.4), bt + k * BEAT + BEAT / 2)
    # 16th-note arp up two octaves
    seq = [chord[0] * 2, chord[1] * 2, chord[2] * 2, chord[0] * 4,
           chord[1] * 2, chord[2] * 2, chord[0] * 4, chord[min(2, len(chord) - 1)] * 4]
    for k in range(16):
        f = seq[k % 8]
        vel = 0.34 if k % 4 == 0 else 0.22
        place(music, pluck(f, 0.16, vel), bt + k * BEAT / 4)
    # sparkle layer an octave up once section B starts
    if bt >= GROOVE_B - 0.01:
        for k in range(8):
            place(music, pluck(seq[k % 8] * 2, 0.12, 0.13), bt + k * BEAT / 2)
    # sustained stab each bar
    n_bar = int(BAR * SR)
    tt = np.arange(n_bar) / SR
    pad = sum(np.sin(2 * np.pi * f * tt) for f in chord) * np.exp(-tt * 1.6)
    place(music, pad * 0.10, bt)
    bt += BAR
    bar_i += 1

# ---- outro: Fmaj7 -> C resolve, long ring ---------------------------------------
n_out = N - int(OUTRO * SR)
tt = np.arange(n_out) / SR
outro_pad = np.zeros(n_out)
for f, amp in ((87.31, 1.0), (110.0, 0.8), (130.81, 0.8), (174.61, 0.6)):
    outro_pad += np.sin(2 * np.pi * f * tt) * amp
half = int(1.8 * SR)
res = np.zeros(n_out)
for f, amp in ((130.81, 1.0), (164.81, 0.8), (196.0, 0.7), (261.63, 0.5)):
    res[half:] += np.sin(2 * np.pi * f * tt[:-half] if half else tt) [: n_out - half] * amp
env = np.exp(-tt * 0.55)
place(music, (outro_pad * np.exp(-tt * 1.1) + res * 0.8) * env * 0.12, OUTRO)

# ---- sidechain pump: duck music after every kick ---------------------------------
pump = np.ones(N)
duck_n = int(0.30 * SR)
duck = 1 - 0.55 * np.exp(-np.arange(duck_n) / (0.09 * SR))
for kt in kick_times:
    i0 = int(kt * SR)
    n = min(duck_n, N - i0)
    pump[i0:i0 + n] = np.minimum(pump[i0:i0 + n], duck[:n])
music *= pump

# ---- message pops (kept from v1, retimed) ----------------------------------------
def blip(f0, f1, dur, amp):
    n = int(dur * SR)
    bt_ = np.arange(n) / SR
    f = np.linspace(f0, f1, n)
    return np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-bt_ * 24) * amp

for ts in (2.7, 8.9, 15.5):
    place(drums, blip(1250, 880, 0.14, 0.5), ts)
for ts in (4.1, 10.1, 17.0):
    place(drums, blip(660, 990, 0.16, 0.45), ts)
for i in range(8):                                   # ask-pill ticks
    place(drums, blip(1500, 1500, 0.04, 0.12), 23.55 + i * 0.32)

# ---- master -----------------------------------------------------------------------
mix = music * 0.9 + drums * 0.8
fade_in = int(0.4 * SR)
fade_out = int(3.2 * SR)
mix[:fade_in] *= np.linspace(0, 1, fade_in)
mix[-fade_out:] *= np.linspace(1, 0, fade_out)
mix = np.tanh(mix * 1.15)
mix = mix / np.max(np.abs(mix)) * 0.88

# subtle stereo width via 12ms haas on a quiet copy
delay = int(0.012 * SR)
left = mix
right = np.copy(mix)
right[delay:] = mix[:-delay] * 0.92 + mix[delay:] * 0.08
stereo = np.stack([left, right], axis=1)
pcm = (stereo * 32767).astype(np.int16)

out = Path(__file__).parent / "audio.wav"
with wave.open(str(out), "wb") as w:
    w.setnchannels(2)
    w.setsampwidth(2)
    w.setframerate(SR)
    w.writeframes(pcm.tobytes())
print("wrote", out, f"{DUR}s @ {BPM}bpm")
