#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audio_sync — Détecte l'offset et l'écart de vitesse entre deux fichiers audio/vidéo.

Analyse l'intégralité des pistes audio par fenêtres glissantes chevauchantes
(PROBE_STEP < PROBE_LEN). Le FFT de la référence est pré-calculé une seule
fois ; seule la fenêtre courante du doublage est transformée à chaque pas, ce qui
garde le temps de traitement autour de 30-90 s par épisode de 24 min.

Produit directement les paramètres pour le flag --sync de mkvmerge :
  --sync TID:delay_ms,stretch_up/stretch_down

Dépendances : numpy, scipy, ffmpeg (pour l'extraction audio)
"""

import os
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from scipy import signal as sps
from scipy.fft import rfft, irfft, next_fast_len
from scipy.io import wavfile

# ---- Paramètres d'analyse --------------------------------------------------
SR         = 8000   # Hz — suffisant pour la corrélation sur signal commun
PROBE_LEN  = 30.0   # durée (s) de chaque fenêtre d'analyse
PROBE_STEP = 15.0   # pas (s) entre fenêtres — chevauchement 50 %
MARGIN     = 5.0    # secondes ignorées en tout début / toute fin
MIN_CONF   = 8.0    # PSR minimal pour retenir une fenêtre comme fiable

# Ratios PAL/film/NTSC exprimés en fractions exactes.
# up/down = facteur d'étirement à appliquer au doublage pour le recaler.
# Dans mkvmerge --sync : stretch = up/down (>1 = ralentit, <1 = accélère).
CANDIDATE_TEMPOS = [
  ("aucune (1:1)",           1,    1),
  ("PAL→NTSC (25/23.976)",  3125, 2997),
  ("NTSC→PAL (23.976/25)",  2997, 3125),
  ("PAL→film (25/24)",       25,   24),
  ("film→PAL (24/25)",       24,   25),
  ("film→NTSC (24/23.976)", 1000, 999),
  ("NTSC→film (23.976/24)", 999,  1000),
]


# ---- Détection de ratio de vitesse ----------------------------------------

def _locate_probe(ref, probe, sr):
  """Où `probe` s'aligne-t-elle le mieux dans `ref` ?
  Utilisé uniquement par _detect_tempo (appel ponctuel, pas de boucle).
  Retourne (position_en_secondes, confiance_PSR).
  """
  ref   = ref   - np.mean(ref)
  probe = probe - np.mean(probe)
  corr  = sps.correlate(ref, probe, mode="valid", method="fft")
  idx   = int(np.argmax(corr))
  peak  = float(corr[idx])
  guard = max(1, int(0.5 * sr))
  lo, hi = max(0, idx - guard), min(len(corr), idx + guard + 1)
  side  = np.concatenate([corr[:lo], corr[hi:]])
  if side.size == 0:
    return idx / sr, 0.0
  conf = float((peak - float(side.mean())) / (float(side.std()) + 1e-9))
  return idx / sr, conf


def _resample(x, up, down):
  if up == down:
    return x
  return sps.resample_poly(x, up, down).astype(np.float32)


def _detect_tempo(ref, dub, sr, probe_len=60.0):
  """Teste les ratios de framerate connus sur une fenêtre centrale du doublage."""
  dub_dur = len(dub) / sr
  a       = int(max(0.0, dub_dur / 2 - probe_len / 2) * sr)
  probe   = dub[a:a + int(probe_len * sr)]
  scores  = []
  for label, up, down in CANDIDATE_TEMPOS:
    _, conf = _locate_probe(ref, _resample(probe, up, down), sr)
    scores.append((conf, up, down, label))
  scores.sort(reverse=True, key=lambda s: s[0])
  c, up, down, label = scores[0]
  return up, down, c, scores[1][0], label


# ---- Scan complet par fenêtres glissantes ----------------------------------

def _scan_offsets(ref, dub, sr=SR, probe_len=PROBE_LEN,
                  step=PROBE_STEP, margin=MARGIN):
  """Scan complet de l'intégralité du doublage par fenêtres glissantes.

  Optimisation clé : le FFT de `ref` est pré-calculé une seule fois.
  Pour chaque fenêtre du doublage, seule une petite FFT (PROBE_LEN × sr
  échantillons) est calculée ; c'est elle qui drive le coût total.

  Retourne list[(t_dub_s, offset_s, confiance_PSR)] pour toutes les fenêtres.
  """
  probe_samples = int(probe_len * sr)
  valid_len     = len(ref) - probe_samples + 1
  if valid_len <= 0:
    return []

  # Pré-calcul du FFT de la référence — exécuté une seule fois
  n_fft   = next_fast_len(len(ref) + probe_samples - 1)
  ref_n   = ref.astype(np.float64)
  ref_n  -= float(ref_n.mean())
  ref_fft = rfft(ref_n, n=n_fft)
  guard   = max(1, int(0.5 * sr))

  dub_dur = len(dub) / sr
  t = margin
  probes = []
  while t + probe_len <= dub_dur - margin:
    a = int(t * sr)
    b = a + probe_samples
    if b > len(dub):
      break
    probe  = dub[a:b].astype(np.float64)
    probe -= float(probe.mean())

    # Corrélation croisée via FFT pré-calculé de ref
    # c_circ[k] = sum_n ref[n] * probe[(n-k) mod n_fft]
    # Partie "valid" (probe tient entièrement dans ref) : c_circ[0:valid_len]
    corr  = irfft(ref_fft * np.conj(rfft(probe, n=n_fft)), n=n_fft)[:valid_len]
    idx   = int(np.argmax(corr))
    peak  = float(corr[idx])
    lo    = max(0, idx - guard)
    hi    = min(valid_len, idx + guard + 1)
    side  = np.concatenate([corr[:lo], corr[hi:]])
    if side.size == 0:
      t += step
      continue
    conf  = float((peak - float(side.mean())) / (float(side.std()) + 1e-9))
    probes.append((float(t), float(idx / sr - t), float(conf)))
    t += step

  return probes


# ---- Analyse principale ----------------------------------------------------

def estimate_offset(ref, dub, sr=SR, probe_len=PROBE_LEN,
                    step=PROBE_STEP, margin=MARGIN, try_drift=True):
  """Analyse complète sur l'intégralité du signal.

  Paramètres
  ----------
  ref, dub : np.ndarray float32 mono normalisé [-1, 1]

  Retourne un dict :
    offset        : décalage à appliquer (s). Positif = retarder le doublage.
    delay_ms      : offset en ms (entier, prêt pour mkvmerge --sync).
    reliable      : True si au moins 2 fenêtres dépassent MIN_CONF.
    n_reliable    : nombre de fenêtres fiables.
    n_total       : nombre total de fenêtres analysées.
    drift         : True si un ratio de vitesse a été retenu.
    drift_label   : nom du ratio (ex. "PAL→NTSC (25/23.976)").
    stretch_up    : numérateur   du ratio d'étirement pour mkvmerge --sync.
    stretch_down  : dénominateur du ratio d'étirement (stretch = up/down).
    probes        : liste complète (t_dub, offset, confiance) de chaque fenêtre.
  """
  up, down, label = 1, 1, "aucune (1:1)"
  if try_drift:
    u, d, best_c, run_c, lab = _detect_tempo(ref, dub, sr)
    if (u, d) != (1, 1) and best_c > MIN_CONF and best_c > 2.0 * run_c:
      up, down, label = u, d, lab

  work   = _resample(dub, up, down) if (up, down) != (1, 1) else dub
  probes = _scan_offsets(ref, work, sr, probe_len, step, margin)

  good       = [(t, o, c) for (t, o, c) in probes if c >= MIN_CONF]
  n_reliable = len(good)

  if n_reliable >= 2:
    offset, reliable = float(np.median([o for _, o, _ in good])), True
  elif n_reliable == 1:
    offset, reliable = good[0][1], False
  elif probes:
    offset, reliable = max(probes, key=lambda x: x[2])[1], False
  else:
    offset, reliable = 0.0, False

  return {
    "offset":       offset,
    "delay_ms":     int(round(offset * 1000)),
    "reliable":     reliable,
    "n_reliable":   n_reliable,
    "n_total":      len(probes),
    "drift":        (up, down) != (1, 1),
    "drift_label":  label,
    "stretch_up":   up,
    "stretch_down": down,
    "probes":       probes,
  }


# ---- Extraction audio ------------------------------------------------------

def extract_mono(src, sr=SR):
  """Extrait l'audio de `src` en mono `sr` Hz (numpy float32 [-1, 1])."""
  fd, name = tempfile.mkstemp(suffix=".wav")
  tmp = Path(name)
  os.close(fd)
  try:
    proc = subprocess.run(
      ["ffmpeg", "-y", "-i", str(src), "-vn",
       "-ac", "1", "-ar", str(sr), "-f", "wav", str(tmp)],
      stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    if proc.returncode != 0:
      raise RuntimeError(proc.stderr.decode("utf-8", "ignore").strip()[:300])
    _, data = wavfile.read(tmp)
  finally:
    tmp.unlink(missing_ok=True)
  if data.ndim > 1:
    data = data.mean(axis=1)
  data = data.astype(np.float32)
  m = np.max(np.abs(data)) or 1.0
  return data / m


# ---- Point d'entrée principal ----------------------------------------------

def analyze(ref_path, dub_path, sr=SR, probe_len=PROBE_LEN,
            step=PROBE_STEP, margin=MARGIN, try_drift=True):
  """Extrait les pistes audio et estime l'offset (scan complet).
  Retourne le même dict qu'`estimate_offset`.
  Lève RuntimeError si l'extraction audio échoue.
  """
  ref = extract_mono(ref_path, sr)
  dub = extract_mono(dub_path, sr)
  return estimate_offset(ref, dub, sr=sr, probe_len=probe_len,
                         step=step, margin=margin, try_drift=try_drift)


# ---- Formatage du flag mkvmerge --------------------------------------------

def mkvmerge_sync_flag(track_id, result):
  """Génère la valeur du flag --sync pour mkvmerge.
  Exemple : "2:1200,3125/2997". Retourne None si aucune correction n'est nécessaire.
  """
  d  = result["delay_ms"]
  up = result["stretch_up"]
  dn = result["stretch_down"]
  if d == 0 and up == dn:
    return None
  if up != dn:
    return f"{track_id}:{d},{up}/{dn}"
  return f"{track_id}:{d}"
