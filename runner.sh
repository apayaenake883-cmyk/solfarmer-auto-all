#!/usr/bin/env bash
# Dipanggil GitHub Actions: seed state dari secrets/cache → 1 siklus → state balik ke cache.
set -euo pipefail

STATE_DIR="${STATE_DIR:-$PWD/sf-state}"
mkdir -p "$HOME" "$STATE_DIR"

# 1) Seed awal dari GitHub Secrets (base64). Dipakai saat pertama kali / cache hilang.
seed() { # $1 = nama env (base64), $2 = nama file tujuan
  if [ -n "${!1:-}" ]; then
    printf '%s' "${!1}" | base64 -d > "$HOME/$2"
    chmod 600 "$HOME/$2"
    echo "seed: $2 dari secret $1"
  fi
}
seed SF_SESSION       .solfarmer_session.json
seed SF_LOGIN         .solfarmer_login.json
seed SF_RELOGIN_STATE .solfarmer_relogin_state.json
seed SF_WD_LOG        .solfarmer_wd_log.json

# 2) Overlay state terbaru dari cache run sebelumnya (lebih baru dari secret —
#    refresh token sudah berputar di sana). Catatan: file diawali titik, jadi
#    daftar eksplisit — glob *.json tidak menangkap dotfile.
STATE_FILES=".solfarmer_session.json .solfarmer_login.json .solfarmer_relogin_state.json .solfarmer_wd_log.json"
for f in $STATE_FILES; do
  if [ -f "$STATE_DIR/$f" ]; then
    cp "$STATE_DIR/$f" "$HOME/"
    chmod 600 "$HOME/$f"
    echo "cache: $f dipulihkan"
  fi
done

# 3) Jalankan SATU siklus (SOLFARMER_ONCE=1 di-set oleh workflow).
#    Kalau python gagal, state tetap disimpan (token berputar tidak hilang).
rc=0
python3 solfarmer_auto_all.py || rc=$?

# 4) Simpan state balik — action/cache mengunggahnya untuk run berikutnya.
rm -rf "$STATE_DIR"
mkdir -p "$STATE_DIR"
for f in $STATE_FILES; do
  if [ -f "$HOME/$f" ]; then
    cp "$HOME/$f" "$STATE_DIR/"
  fi
done
echo "state tersimpan: $(ls -A "$STATE_DIR" | tr '\n' ' ')"
exit $rc
