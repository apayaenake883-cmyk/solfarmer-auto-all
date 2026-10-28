# SolFarmer Auto All — GitHub Actions

Bot auto-klaim + auto-withdraw [solfarmer.io](https://solfarmer.io) yang jalan
gratis selamanya di GitHub Actions (repo publik = menit build tanpa batas).
Jadwal: tiap ±2 menit satu siklus klaim. Tanpa VPS, tanpa kartu.

## Cara kerja
- Workflow `.github/workflows/solfarmer.yml` menjadwalkan `runner.sh` tiap 2 menit.
- `runner.sh` menulis state sesi dari **Secrets** ke HOME, menjalankan
  `solfarmer_auto_all.py` dengan `SOLFARMER_ONCE=1` (satu siklus lalu keluar),
  lalu menyimpan state (termasuk refresh token yang sudah berputar) ke
  **actions/cache** untuk run berikutnya.
- Job keep-alive commit `KEEPALIVE.md` tiap Senin supaya jadwal tidak dinonaktifkan
  GitHub setelah 60 hari sepi.

## Setup (sekali saja)
1. Buat repo publik ini di GitHub kamu (sudah ada kalau kamu memakai repo ini).
2. Buka **Settings → Secrets and variables → Actions → New repository secret**,
   tambahkan 4 secret (isi file dari HP, base64-encoded):
   | Secret | Isi |
   |---|---|
   | `SF_SESSION` | `base64 ~/.solfarmer_session.json` |
   | `SF_LOGIN` | `base64 ~/.solfarmer_login.json` |
   | `SF_RELOGIN_STATE` | `base64 ~/.solfarmer_relogin_state.json` |
   | `SF_WD_LOG` | `base64 ~/.solfarmer_wd_log.json` |
   Di Termux: `base64 -w0 ~/.solfarmer_session.json` lalu salin hasilnya ke secret.
3. Buka tab **Actions** → izinkan workflow (kalau ada banner) → selesai.
   Jalankan manual sekali dengan tombol **Run workflow** buat tes.

## Konfirmasi withdraw (kode email)
Saat saldo mencukupi (≥ 0.1 SOL), bot otomatis kirim permintaan wd — kode
konfirmasinya dikirim ke email kamu. Konfirmasi cuma butuh **1 langkah**:

1. Buka email solfarmer, salin kode 6 huruf (atau link konfirmasinya).
2. Buka tab **Actions** di repo ini → pilih workflow **SolFarmer Auto All** →
   tombol **Run workflow** → tempel kode di kolom `wd_code` → Run.

Bot lanjut konfirmasi + catat ke riwayat sendiri. Cron 2-menitan tidak terganggu —
pending wd menunggu sampai 30 menit sampai kamu sempat tempel kodenya.

Alternatif hands-off penuh: set secret `SOLFARMER_IMAP_PASS` (Gmail app password) +
opsional `SOLFARMER_IMAP_USER` — kode email dibaca otomatis tiap siklus, tanpa
langkah manual sama sekali.

## Catatan keamanan
- Repo ini PUBLIK dan kode TIDAK berisi kredensial apa pun — semua rahasia hidup di
  GitHub Secrets (hanya terlihat pemilik repo).
- Kalau suatu saat ganti HP: state cache bisa hilang → cukup jalankan manual lagi;
  kalau token mati total, ulangi langkah setup dengan file state baru dari HP.
