#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audio_sync — Détecte l'offset et l'écart de vitesse entre deux fichiers audio/vidéo.

Principe : extraction mono basse fréquence → corrélation croisée sur le signal
commun (musique, bruitages) pour trouver le décalage. Teste également les ratios
de framerate connus (PAL/film/NTSC) pour corriger les problèmes de vitesse.

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
from scipy.io import wavfile

# ---- Paramètres d'analyse --------------------------------------------------
SR        = 8000    # Hz — suffisant pour la corrélation sur signal commun
N_PROBES  = 6       # fenêtres de test réparties sur la durée du doublage
PROBE_LEN = 30.0    # secondes par fenêtre
MARGIN    = 60.0    # secondes ignorées en début/fin (génériques, silences)
MIN_CONF  = 8.0     # PSR minimal pour juger une fenêtre fiable

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


# ---- Cœur de l'algorithme --------------------------------------------------

def _locate_probe(ref, probe, sr):
  """Où la `probe` s'aligne-t-elle le mieux dans `ref` ?
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
  conf = float((peak - side.mean()) / (side.std() + 1e-9))
  return idx / sr, conf


def _resample(x, up, down):
  if up == down:
    return x
  return sps.resample_poly(x, up, down).astype(np.float32)


def _constant_offset(ref, dub, sr, n_probes, probe_len, margin):
  """Décalage constant par sondage multi-fenêtres.
  Retourne (offset_secondes, fiable, probes).
  """
  dub_dur = len(dub) / sr
  last    = max(margin, dub_dur - margin - probe_len)
  starts  = np.linspace(margin, last, n_probes)
  probes  = []
  for t_p in starts:
    a = int(t_p * sr)
    b = a + int(probe_len * sr)
    if b > len(dub):
      continue
    pos_ref, conf = _locate_probe(ref, dub[a:b], sr)
    probes.append((float(t_p), float(pos_ref - t_p), float(conf)))
  good = [(t, o, c) for (t, o, c) in probes if c >= MIN_CONF]
  if len(good) >= 2:
    return float(np.median([o for _, o, _ in good])), True, probes
  best = max(probes, key=lambda x: x[2]) if probes else (0.0, 0.0, 0.0)
  return float(best[1]), False, probes


def _detect_tempo(ref, dub, sr, probe_len=60.0):
  """Teste les ratios de framerate connus, retourne le meilleur (up, down, ...)."""
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


def estimate_offset(ref, dub, sr=SR, n_probes=N_PROBES,
          probe_len=PROBE_LEN, margin=MARGIN, try_drift=True):
  """Analyse complète : éventuel écart de vitesse, puis décalage constant.

  Paramètres
  ----------
  ref, dub : np.ndarray float32 mono normalisé [-1, 1]

  Retourne un dict :
    offset        : décalage à appliquer (s). Positif = retarder le doublage.
    reliable      : True si la mesure est jugée fiable.
    drift         : True si un ratio de vitesse a été détecté.
    drift_label   : nom du ratio (ex. "PAL→NTSC (25/23.976)").
    stretch_up    : numérateur   du ratio d'étirement pour mkvmerge --sync.
    stretch_down  : dénominateur du ratio d'étirement pour mkvmerge --sync.
            stretch = up/down (>1 ralentit, <1 accélère).
    delay_ms      : offset converti en ms (entier, prêt pour mkvmerge --sync).
    probes        : liste de (t_doublage, offset, confiance).
  """
  up, down, label = 1, 1, "aucune (1:1)"
  if try_drift:
    u, d, best_c, run_c, lab = _detect_tempo(ref, dub, sr)
    if (u, d) != (1, 1) and best_c > MIN_CONF and best_c > 2.0 * run_c:
      up, down, label = u, d, lab

  work = _resample(dub, up, down) if (up, down) != (1, 1) else dub
  offset, reliable, probes = _constant_offset(
    ref, work, sr, n_probes, probe_len, margin)

  return {
    "offset":       offset,
    "delay_ms":     int(round(offset * 1000)),
    "reliable":     reliable,
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

def analyze(ref_path, dub_path, sr=SR, n_probes=N_PROBES,
      probe_len=PROBE_LEN, margin=MARGIN, try_drift=True):
  """Analyse complète : extrait les pistes audio et estime l'offset.

  Retourne le même dict qu'`estimate_offset`.
  Lève RuntimeError si l'extraction audio échoue.
  """
  ref = extract_mono(ref_path, sr)
  dub = extract_mono(dub_path, sr)
  return estimate_offset(ref, dub, sr=sr, n_probes=n_probes,
               probe_len=probe_len, margin=margin,
               try_drift=try_drift)


# ---- Formatage du flag mkvmerge --------------------------------------------

def mkvmerge_sync_flag(track_id, result):
  """Génère la valeur du flag --sync pour mkvmerge.

  Exemple de retour : "2:1200,3125/2997"
  Retourne None si aucune correction n'est nécessaire.
  """
  d  = result["delay_ms"]
  up = result["stretch_up"]
  dn = result["stretch_down"]
  if d == 0 and up == dn:
    return None
  if up != dn:
    return f"{track_id}:{d},{up}/{dn}"
  return f"{track_id}:{d}"
