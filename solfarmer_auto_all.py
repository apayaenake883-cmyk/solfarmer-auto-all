#!/usr/bin/env python3
"""SolFarmer Auto All — auto klaim harian + auto withdraw SOLfarmer.io tanpa browser.

Jalankan langsung (tanpa flag):
  python3 solfarmer_auto_all.py

Mode cron/GitHub Actions (1 siklus lalu keluar):
  SOLFARMER_ONCE=1 python3 solfarmer_auto_all.py
Interval loop bisa diubah: SOLFARMER_INTERVAL=300 (detik, default 90).

Sesi (urutan prioritas):
  1. sf_access_token  (berlaku ±15 menit, gratis)
  2. sf_refresh_token → diperpanjang otomatis tiap <2 menit sisa umur (gratis)
  3. Auto-relogin (cadangan, 0.7 token solver per solve) — aktif kalau kamu isi
     password sekali saat pertama jalan (tersimpan di ~/.solfarmer_login.json, 600).

Auto Withdraw (endpoint ASLI, diverifikasi live 2026-10-28):
  Tiap siklus (90 detik) saldo dicek via GET /api/ledger/balance/{userId};
  kalau >= SOLFARMER_WD_AMOUNT (default 1.0 SOL, minimum site 0.1 SOL), script:
    1. POST /api/game/wallet/withdraw  {amount_lamports, destination_address}
    2. ambil kode konfirmasi dari email — urutan: env SOLFARMER_WD_CODE
       (boleh tempel link /withdraw/confirm?token=... atau kode 6 huruf),
       lalu IMAP auto-baca kalau SOLFARMER_IMAP_PASS diisi (Gmail app password),
       lalu prompt terminal (tunggu 60 detik; Enter = lewati).
    3. POST /api/game/wallet/withdraw/confirm  {code|token}
  State pending disimpan di ~/.solfarmer_wd_log.json (kode TIDAK disimpan),
  kedaluwarsa 30 menit, cooldown 300 detik + cap harian SOLFARMER_MAX_WD.
Env: SOLFARMER_WALLET_ADDR, SOLFARMER_WD_AMOUNT, SOLFARMER_WD_CODE, SOLFARMER_MAX_WD
     (cap withdraw/hari, default 50), SOLFARMER_WD_COOLDOWN_SEC, SOLFARMER_WD_MIN_BALANCE,
     SOLFARMER_IMAP_USER/HOST/PORT/PASS (opsional, auto-baca kode email),
     SOLFARMER_COOKIE, SOLFARMER_REFRESH, SOLFARMER_EMAIL, SOLFARMER_PASSWORD,
     SOLFARMER_APIKEY, SOLFARMER_MAX_SOLVE (cap solve/hari, default 20)

Sisa saldo minimum: 0.1 SOL (tidak withdraw sampai sisa >= 0.1 SOL)
Alamat wallet: env SOLFARMER_WALLET_ADDR atau yang tersimpan di ~/.solfarmer_wd_log.json.

Fitur TIDAK diotomasi (diverifikasi live 2026-10-28 — rutenya mati/berat):
  weekly quest (404), pvp season claim (404), notifikasi clear-all (yang jalan
  cuma /social/notifications/read-all), convert SOL->SSOL (justru mengurangi saldo
  yang bisa ditarik), set-bonus (Supabase edge function), start ekspedisi/minigame
  (interaksi manual).

Ctrl+C untuk berhenti.
"""

from __future__ import annotations

import base64
import datetime as dt
import getpass
import json
import os
import random
import re
import string
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    print("Butuh library requests:  pip install requests", file=sys.stderr)
    sys.exit(1)

BASE = "https://solfarmer.io"
GAME = f"{BASE}/api/game"
AUTH = f"{BASE}/api/auth"
SESSION_FILE = Path.home() / ".solfarmer_session.json"
INTERVAL_SEC = int(os.environ.get("SOLFARMER_INTERVAL") or "90")  # detik antar siklus
DELAY_SEC = 0.4
REFRESH_MARGIN_SEC = 120  # perpanjang token saat sisa umur < 2 menit (sama dgn situsnya)
REFRESH_RETRY_DELAY = (1.2, 2.4, 3.6)

# --- auto-relogin (cadangan saat refresh token putus) ---
SOLVER_BASE = "https://api.waryono.my.id"
SOLVER_SITEKEY = "0x4AAAAAAD6IBdRb6nTWIoWl"  # sitekey Turnstile solfarmer.io
SOLVER_DEFAULT_KEY = "8a2075541162f7a2e6bdbe94f35cdde5"  # apikey solver kamu (override: SOLFARMER_APIKEY)
SOLVER_TIMEOUT_SEC = 120
BROWSER_UA = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36"
)
LOGIN_FILE = Path.home() / ".solfarmer_login.json"
STATE_FILE = Path.home() / ".solfarmer_relogin_state.json"
RELOGIN_COOLDOWN_SEC = 300  # 5 menit antar solve
SOLVE_DAILY_CAP = int(os.environ.get("SOLFARMER_MAX_SOLVE", "20"))
NEGATIVE_CACHE_SEC = 1800  # 30 menit kalau solver error permanen
TRANSIENT_CACHE_SEC = 60  # 1 menit kalau solver lagi penuh/gangguan (tidak ditagih)
TRANSIENT_SOLVER_ERRORS = {
    "ERROR_SLOT_NOT_AVAILABLE",
    "ERROR_DATABASE_CONNECTION_FAILED",
    "ERROR_TOO_MANY_REQUESTS",
    "ERROR_SERVER_BUSY",
    "timeout",
}
CACHED_TOKEN_MAX_AGE_SEC = 240  # token hasil solve dipakai ulang maks 4 menit

# --- auto withdraw settings ---
WALLET_ADDR = os.environ.get("SOLFARMER_WALLET_ADDR") or ""
WD_AMOUNT = float(os.environ.get("SOLFARMER_WD_AMOUNT") or "1.0")  # withdraw kalau saldo >= ini
WD_CAP_PER_DAY = int(os.environ.get("SOLFARMER_MAX_WD", "50"))
WD_COOLDOWN = float(os.environ.get("SOLFARMER_WD_COOLDOWN_SEC") or "300")  # cooldown antar withdraw
WD_MIN_BALANCE = float(os.environ.get("SOLFARMER_WD_MIN_BALANCE") or "0.1")  # sisakan ini di akun
WD_LOG = Path.home() / ".solfarmer_wd_log.json"
WD_PENDING_EXPIRE_SEC = 1800  # kode email tidak dimasukkan 30 menit → permintaan batal
WD_SITE_MIN = 0.1  # minimum withdrawal solfarmer.io
# format kode situs: 6 karakter huruf besar + angka (tanpa 0/1/I/O); input user
# dilebarkan ke [A-Z0-9] — validasi final tetap di server.
WD_CODE_RE = re.compile(r"^[A-Z0-9]{6}$")
WD_CODE_STRICT = r"[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{6}"
WD_BASE58_RE = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")  # alamat Solana (base58)
IMAP_STATE = {"failed": False}

# Fitur aktif: SEMUA (auto all)
FEATURE_ORDER = [
    "settle",
    "streak",
    "freeBox",
    "quests",
    "milestones",
    "story",
    "campQuests",
    "expedition",
    "pools",
    "gifts",
    "visits",
    "notifications",
    "withdraw",
]

FEATURE_LABELS = {
    "settle": "Settle tambang (ambil SOL)",
    "streak": "Streak harian",
    "freeBox": "Peti gratis harian",
    "quests": "Quest utama",
    "milestones": "Milestone",
    "story": "Story reward",
    "campQuests": "Quest Camp",
    "expedition": "Klaim ekspedisi selesai",
    "pools": "Reward mining pool",
    "gifts": "Gift diterima",
    "visits": "Visit reward farm",
    "notifications": "Bersihkan notifikasi",
    "withdraw": "Auto withdraw SOL ke wallet",
}

DEFAULT_FEATURES = {k: True for k in FEATURE_ORDER}

BENIGN_RE = re.compile(
    r"already[_ -]?claimed|cooldown|not yet|nothing to claim|not available|"
    r"ineligible|not ready|no rewards|too early|already opened|already claimed|"
    r"daily limit|nothing to",
    re.I,
)

RESET = "\033[0m"
COLORS = {"ok": "\033[32m", "err": "\033[31m", "warn": "\033[33m", "info": "\033[36m"}


class ApiError(Exception):
    def __init__(self, status: int | None, body, message: str = ""):
        self.status = status
        self.body = body if isinstance(body, dict) else {}
        self.message = message or str(self.body.get("error") or self.body.get("message") or "")
        super().__init__(f"{status} {self.message}".strip())


def colorize(text: str, level: str, use_color: bool) -> str:
    if not use_color:
        return text
    return f"{COLORS.get(level, '')}{text}{RESET}"


class Logger:
    def __init__(self, use_color: bool):
        self.use_color = use_color

    def log(self, msg: str, level: str = "info") -> None:
        now = dt.datetime.now().strftime("%H:%M:%S")
        print(colorize(f"[{now}] {msg}", level, self.use_color), flush=True)


def parse_session(raw: str) -> tuple[str, str | None]:
    raw = (raw or "").strip().strip('"').strip("'")
    if not raw:
        return "", None
    looks_like_pairs = ";" in raw or bool(
        re.match(r"^\s*(sf_access_token|sf_refresh_token|access_token|refresh_token|token)\s*=", raw, re.I)
    )
    if looks_like_pairs:
        token = refresh = None
        for part in re.split(r"[;\n]", raw):
            part = part.strip()
            if not part or "=" not in part:
                continue
            k, v = part.split("=", 1)
            k, v = k.strip(), v.strip().strip('"')
            if k.lower() in ("sf_access_token", "access_token", "authorization", "token"):
                token = v
            elif k.lower() in ("sf_refresh_token", "refresh_token"):
                refresh = v
        if token:
            if token.lower().startswith("bearer "):
                token = token[7:].strip()
            return token, refresh
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    return raw, None


def load_saved_session() -> tuple[str | None, str | None]:
    try:
        data = json.loads(SESSION_FILE.read_text())
        return data.get("access_token"), data.get("refresh_token")
    except Exception:
        return None, None


def token_exp(token: str | None) -> float | None:
    """Epoch detik kedaluwarsa JWT, atau None kalau bukan JWT valid."""
    if not token or token.count(".") != 2:
        return None
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        exp = data.get("exp")
        return float(exp) if isinstance(exp, (int, float)) else None
    except Exception:
        return None


def save_session(token: str, refresh: str | None) -> None:
    SESSION_FILE.write_text(json.dumps({"access_token": token, "refresh_token": refresh}, indent=2))
    try:
        os.chmod(SESSION_FILE, 0o600)
    except OSError:
        pass


def _secure_write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def clear_saved_password() -> None:
    """Hapus password tersimpan (dipanggil saat server bilang password salah)."""
    try:
        data = json.loads(LOGIN_FILE.read_text())
    except Exception:
        return
    if not data.get("password"):
        return
    data["password"] = None
    _secure_write(LOGIN_FILE, data)


def load_login() -> dict:
    try:
        data = json.loads(LOGIN_FILE.read_text())
    except Exception:
        data = {}
    return {
        "email": data.get("email") or os.environ.get("SOLFARMER_EMAIL") or "dodykurniap4n@gmail.com",
        "password": data.get("password") or os.environ.get("SOLFARMER_PASSWORD"),
        "apikey": data.get("apikey") or os.environ.get("SOLFARMER_APIKEY") or SOLVER_DEFAULT_KEY,
    }


def load_state() -> dict:
    today = dt.date.today().isoformat()
    try:
        st = json.loads(STATE_FILE.read_text())
    except Exception:
        st = {}
    if st.get("day") != today:
        st = {"day": today, "solves_today": 0}
    st.setdefault("last_solve_at", 0)
    st.setdefault("cached_token", None)
    st.setdefault("cached_used_at", 0)
    st.setdefault("negative_until", 0)
    st.setdefault("transient_until", 0)
    st.setdefault("relogin_rejected", False)
    st.setdefault("tokens_spent", 0.0)
    st.setdefault("day", today)
    st.setdefault("solves_today", 0)
    return st


def save_state(st: dict) -> None:
    _secure_write(STATE_FILE, st)


def check_balance(apikey: str) -> float | None:
    try:
        res = requests.get(
            f"{SOLVER_BASE}/balance.php",
            params={"apikey": apikey, "json": 1},
            headers={"User-Agent": BROWSER_UA},
            timeout=20,
        )
        data = res.json()
        if data.get("status") != 1:
            return None
        return float(data.get("balance") or 0)
    except Exception:
        return None


def solve_turnstile(apikey: str, log: Logger) -> tuple[str | None, str | None]:
    """Solve Turnstile SolFarmer.

    Return (token, error_code):
      token buka None  → sukses
      error transient   → kode seperti ERROR_SLOT_NOT_AVAILABLE (retry aman, TIDAK ditagih)
      error permanen   → ERROR_ZERO_BALANCE / ERROR_CAPTCHA_UNSOLVABLE / dll
    """
    headers = {"User-Agent": BROWSER_UA, "Content-Type": "application/json"}
    payload = {
        "apikey": apikey,
        "methods": "turnstile",
        "domain": "solfarmer.io",
        "sitekey": SOLVER_SITEKEY,
        "json": 1,
    }
    task_id = None
    last_code = None
    for attempt in range(4):  # submit retry: error transient tidak ditagih
        if attempt:
            time.sleep(5)
        try:
            res = requests.post(
                f"{SOLVER_BASE}/in.php?json=1",
                json=payload,
                headers=headers,
                timeout=30,
            )
            data = res.json()
        except Exception as exc:
            last_code = f"network:{exc}"
            continue
        if data.get("status") == 1:
            task_id = data.get("request")
            break
        last_code = str(data.get("request") or "unknown")
        if last_code in TRANSIENT_SOLVER_ERRORS:
            continue
        break
    if not task_id:
        return None, last_code

    deadline = time.time() + SOLVER_TIMEOUT_SEC
    while time.time() < deadline:
        time.sleep(3)
        try:
            r = requests.get(
                f"{SOLVER_BASE}/res.php",
                params={"apikey": apikey, "id": task_id, "action": "get", "json": 1},
                headers={"User-Agent": BROWSER_UA},
                timeout=20,
            ).json()
        except Exception:
            continue
        if r.get("status") == 1:
            return r.get("request"), None
        code = str(r.get("request") or "")
        if code == "CAPCHA_NOT_READY":
            continue
        return None, code
    return None, "timeout"


def email_login(
    email: str, password: str, apikey: str, captcha_token: str, log: Logger
) -> tuple[str | None, str | None, str]:
    """Login email+password+turnstile.

    Return (access, refresh, kind):
      kind="ok"        → sukses
      kind="network"   → gagal kirim (token captcha BELUM tentu terpakai, boleh dipakai ulang)
      kind="rejected"  → server menolak (token captcha sudah terpakai/invalid, harus solve baru)
    """
    try:
        res = requests.post(
            f"{AUTH}/auth/login",
            json={"email": email, "password": password, "captcha_token": captcha_token},
            headers={
                "User-Agent": BROWSER_UA,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Origin": BASE,
                "Referer": f"{BASE}/",
            },
            timeout=25,
        )
    except Exception as exc:
        log.log(f"Login gagal kirim: {exc}", "warn")
        return None, None, "network"
    if res.status_code != 200:
        try:
            body = res.json()
        except Exception:
            body = {"message": res.text[:160]}
        msg = str(body.get("message") or body.get("error") or "")
        log.log(f"Login ditolak ({res.status_code}): {msg}", "warn")
        if res.status_code == 401 and "password" in msg.lower():
            # password tersimpan salah — buang, biar bisa input ulang di run berikutnya
            clear_saved_password()
            try:
                st = load_state()
                st["relogin_rejected"] = True  # akun kemungkinan login via Google
                save_state(st)
            except Exception:
                pass
            log.log(
                "Password ditolak server — auto-relogin dimatikan permanen "
                "(kemungkinan akun pakai login Google). "
                "Cara aktifkan lagi: hapus ~/.solfarmer_relogin_state.json",
                "warn",
            )
            return None, None, "bad_credentials"
        return None, None, "rejected"
    try:
        data = res.json()
    except Exception:
        return None, None, "rejected"
    access = data.get("accessToken")
    if not access:
        log.log("Login balasan tanpa accessToken.", "warn")
        return None, None, "rejected"
    return access, data.get("refreshToken"), "ok"


class Client:
    def __init__(self, token: str, refresh: str | None, cookie_header: str | None, delay: float, log: Logger):
        self.token = token
        self.refresh_token = refresh
        self.cookie_header = cookie_header
        self.delay = delay
        self.log = log
        self.relogin_reason = ""
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json", "User-Agent": "SolFarmerAutoAll/1.0"})

    def _headers(self, method: str) -> dict:
        headers = {"Authorization": f"Bearer {self.token}"}
        if self.cookie_header:
            headers["Cookie"] = self.cookie_header
        if method != "GET":
            headers["idempotency-key"] = f"{int(time.time() * 1000)}-{random_id()}"
        return headers

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        if path.startswith("/api/"):
            return f"{BASE}{path}"
        return f"{GAME}{path}"

    def _apply_tokens(self, token: str, refresh: str | None) -> None:
        self.token = token
        if refresh:
            self.refresh_token = refresh
        save_session(self.token, self.refresh_token)

    def ensure_fresh(self) -> bool:
        """Perpanjang token proaktif sebelum kedaluwarsa (tanpa menunggu 401)."""
        exp = token_exp(self.token)
        if exp is None:
            return True
        if exp - time.time() > REFRESH_MARGIN_SEC:
            return True
        return self.try_refresh()

    def try_refresh(self) -> bool:
        if not self.refresh_token:
            # token lama mungkin sudah diganti proses lain — reload dari file
            saved_token, saved_refresh = load_saved_session()
            if saved_refresh and saved_refresh != self.refresh_token:
                self.refresh_token = saved_refresh
                if saved_token and token_exp(saved_token or "") and (token_exp(saved_token) or 0) > time.time():
                    self._apply_tokens(saved_token, saved_refresh)
                    return True
            if not self.refresh_token:
                return False
        last_ok = False
        for i, delay in enumerate(REFRESH_RETRY_DELAY):
            if i:
                time.sleep(delay)
            try:
                res = self.session.post(
                    f"{AUTH}/auth/refresh",
                    json={"refreshToken": self.refresh_token},
                    timeout=20,
                )
            except Exception:
                continue
            if res.status_code != 200:
                if res.status_code in (400, 401, 403):
                    # refresh token ditolak permanen — jangan retry
                    return False
                continue
            try:
                data = res.json()
            except Exception:
                return False
            access = data.get("accessToken")
            refresh = data.get("refreshToken") or self.refresh_token
            if not access:
                return False
            self._apply_tokens(access, refresh)
            last_ok = True
            break
        return last_ok

    def try_relogin(self) -> bool:
        """Auto-relogin (solve Turnstile + login email). Cadangan saat refresh putus.

        Layer 4: token masih valid / cache token / cap harian / cooldown dicek dulu
        supaya saldo solver tidak terkuras.
        """
        # 1. Token masih hidup? langsung lolos, 0 request solver.
        exp = token_exp(self.token)
        if exp is not None and exp - time.time() > REFRESH_MARGIN_SEC:
            self.relogin_reason = ""
            return True

        creds = load_login()
        email, password, apikey = creds.get("email"), creds.get("password"), creds.get("apikey")
        if not (email and password and apikey):
            self.relogin_reason = (
                "password ditolak server sebelumnya (akun kemungkinan pakai login Google) — "
                "aktifkan lagi dgn hapus ~/.solfarmer_relogin_state.json"
                if load_state().get("relogin_rejected")
                else ("password belum diisi (jalankan script di terminal buat input)" if not password else "apikey solver belum diisi")
            )
            return False

        st = load_state()
        now = time.time()

        # 2. Negative cache (solver error permanen tadi).
        if now < float(st.get("negative_until") or 0):
            self.relogin_reason = f"solver error permanen, coba lagi dalam {int(st['negative_until'] - now)}s"
            return False

        # 2b. Transient cache (solver lagi penuh/gangguan, tidak ditagih).
        if now < float(st.get("transient_until") or 0):
            self.relogin_reason = f"solver sibuk, coba lagi dalam {int(st['transient_until'] - now)}s"
            return False

        # 3. Cap harian.
        if int(st.get("solves_today") or 0) >= SOLVE_DAILY_CAP:
            self.relogin_reason = f"cap harian tercapai ({st['solves_today']}/{SOLVE_DAILY_CAP} solve)"
            return False

        # 4. Cooldown antar solve.
        if now - float(st.get("last_solve_at") or 0) < RELOGIN_COOLDOWN_SEC:
            sisa = int(RELOGIN_COOLDOWN_SEC - (now - float(st.get("last_solve_at") or 0)))
            self.relogin_reason = f"cooldown {sisa}s lagi (hemat token solver)"
            return False

        # 5. Cache token hasil solve yang belum sempat dipakai server (gratis).
        captcha_token = None
        cached = st.get("cached_token")
        cached_at = float(st.get("cached_used_at") or 0)
        if cached and 0 <= now - cached_at <= CACHED_TOKEN_MAX_AGE_SEC:
            captcha_token = cached

        # 6. Baru solve kalau benar-benar perlu (cek saldo dulu).
        if captcha_token is None:
            balance = check_balance(apikey)
            if balance is not None and balance < 1.0:
                self.log.log(
                    f"Saldo solver tinggal {balance} token (< 1) — auto-relogin dimatikan. "
                    f"Top-up {SOLVER_BASE} atau isi SOLFARMER_REFRESH.",
                    "warn",
                )
                st["negative_until"] = now + NEGATIVE_CACHE_SEC
                save_state(st)
                self.relogin_reason = f"saldo solver tinggal {balance} token"
                return False
            self.log.log("Rantai refresh putus — solve captcha (0.7 token)…", "info")
            captcha_token, err = solve_turnstile(apikey, self.log)
            st["last_solve_at"] = now  # cooldown jalan terus, mencegah spam submit
            if not captcha_token:
                if err in TRANSIENT_SOLVER_ERRORS:
                    self.log.log(
                        f"Solver sibuk ({err}) — coba lagi {TRANSIENT_CACHE_SEC}s (tidak ditagih).",
                        "warn",
                    )
                    st["transient_until"] = now + TRANSIENT_CACHE_SEC
                    self.relogin_reason = f"solver sibuk ({err})"
                else:
                    self.log.log(f"Solver gagal: {err}", "warn")
                    st["negative_until"] = now + NEGATIVE_CACHE_SEC
                    self.relogin_reason = f"solver gagal: {err}"
                save_state(st)
                return False
            st["solves_today"] = int(st.get("solves_today") or 0) + 1
            st["tokens_spent"] = round(float(st.get("tokens_spent") or 0) + 0.7, 2)
            st["cached_token"] = captcha_token
            st["cached_used_at"] = now
            save_state(st)
        else:
            self.log.log("Pakai token captcha tersimpan (gratis).", "info")

        # 7. Login.
        access, refresh, kind = email_login(email, password, apikey, captcha_token, self.log)
        st = load_state()
        if kind == "network":
            # token captcha belum tentu terpakai server — pertahankan buat retry
            self.relogin_reason = "gangguan jaringan saat login (token captcha dipertahankan)"
            return False
        # ok / rejected: token captcha sudah terpakai (atau sudah dipakai sukses)
        st["cached_token"] = None
        st["cached_used_at"] = 0
        save_state(st)
        if kind != "ok":
            self.relogin_reason = (
                "password ditolak server — auto-relogin dimatikan permanen (kemungkinan login Google)"
                if kind == "bad_credentials"
                else "server menolak login (token captcha dipakai)"
            )
            return False
        self.relogin_reason = ""
        self._apply_tokens(access, refresh)
        st["negative_until"] = 0
        st["transient_until"] = 0
        save_state(st)
        sisa = SOLVE_DAILY_CAP - int(st.get("solves_today") or 0)
        self.log.log(
            f"Login ulang sukses — refresh token baru tersimpan, "
            f"solver bekerja lagi nanti. Sisa cap hari ini: {sisa}",
            "ok",
        )
        return True

    def request(self, method: str, path: str, body=None) -> dict:
        url = self._url(path)
        payload = None if body is None else json.dumps(body)

        self.ensure_fresh()
        res = self.session.request(method, url, data=payload, headers=self._headers(method), timeout=25)

        if res.status_code == 401 and self.try_refresh():
            res = self.session.request(method, url, data=payload, headers=self._headers(method), timeout=25)

        if res.status_code == 401 and self.try_relogin():
            res = self.session.request(method, url, data=payload, headers=self._headers(method), timeout=25)

        if res.status_code == 204 or not res.content:
            data = {}
        else:
            try:
                data = res.json()
            except Exception:
                data = {"message": res.text[:200]}

        if res.status_code >= 400:
            raise ApiError(res.status_code, data)
        return data if isinstance(data, dict) else {}

    def get(self, path: str) -> dict:
        return self.request("GET", path)

    def post(self, path: str, body=None) -> dict:
        return self.request("POST", path, body if body is not None else {})

    def tick(self) -> None:
        time.sleep(self.delay)


def random_id() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=8))


def is_benign(exc: Exception) -> bool:
    if isinstance(exc, ApiError):
        if exc.status == 409:
            return True
        text = f"{exc.message} {json.dumps(exc.body, ensure_ascii=False)}".lower()
        if exc.status == 400 and re.search(r"already|cooldown|not yet|nothing|not available|ineligible|not ready", text):
            return True
        return bool(BENIGN_RE.search(text))
    return False


def load_wd_log() -> dict:
    try:
        data = json.loads(WD_LOG.read_text())
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    # wd_log lama (hasil prompt setup) bisa tanpa kunci withdraws
    if not isinstance(data.get("withdraws"), list):
        data["withdraws"] = []
    return data


def save_wd_log(data: dict) -> None:
    _secure_write(WD_LOG, data)


def wallet_address() -> str:
    """Alamat tujuan: env SOLFARMER_WALLET_ADDR, atau yang tersimpan di wd_log."""
    if WALLET_ADDR:
        return WALLET_ADDR.strip()
    try:
        return str(load_wd_log().get("address") or "").strip()
    except Exception:
        return ""


def can_withdraw() -> tuple[bool, str]:
    """Cek apakah boleh withdraw (alamat + cap harian + cooldown)."""
    addr = wallet_address()
    if not addr:
        return False, "alamat wallet belum diisi (SOLFARMER_WALLET_ADDR)"
    if not WD_BASE58_RE.fullmatch(addr):
        return False, "alamat wallet tidak valid (bukan base58 Solana)"
    data = load_wd_log()
    today = dt.date.today().isoformat()
    count = sum(1 for w in data.get("withdraws") if w.get("date") == today)
    if count >= WD_CAP_PER_DAY:
        return False, f"cap withdraw harian tercapai ({count}/{WD_CAP_PER_DAY})"
    last = max((w.get("timestamp") for w in data.get("withdraws") if w.get("timestamp")), default=0)
    if time.time() - last < WD_COOLDOWN:
        return False, f"cooldown {int(WD_COOLDOWN - (time.time() - last))}s"
    return True, ""


# =====================================================================
# AUTO WITHDRAW — endpoint ASLI (diverifikasi live 2026-10-28):
#   POST /api/game/wallet/withdraw         {amount_lamports, destination_address}
#   POST /api/game/wallet/withdraw/confirm {code "XXXXXX" | token}
# Server mengirim kode konfirmasi ke EMAIL — 1 withdraw = 1 kode.
# =====================================================================

def ledger_balance(client: Client, user_id) -> tuple[float, float]:
    """(SOL, SSOL) dari ledger. Rute lama /mining/balance sudah 404."""
    d = client.get(f"/api/ledger/balance/{user_id}")

    def lam(v) -> float:
        try:
            return int(float(v or 0)) / 1e9
        except Exception:
            return 0.0

    return lam(d.get("balance_lamports")), lam(d.get("ssol_lamports"))


def extract_wd_code(text: str) -> str | None:
    """Kode 6 huruf besar setelah kata 'code' di manapun di teks (format email resmi)."""
    for m in re.finditer(r"\bcode\b", text, re.I):
        window = text[m.end() : m.end() + 60]
        c = re.search(rf"\b({WD_CODE_STRICT})\b", window)  # case-sensitive: huruf besar saja
        if c:
            return c.group(1)
    return None


def parse_wd_proof(raw: str) -> tuple[str, str] | None:
    """Kenali kode/link konfirmasi email → ("code", "ABC123") atau ("token", ...)."""
    raw = (raw or "").strip().strip('"').strip("'")
    if not raw:
        return None
    m = re.search(r"[?&#](?:code|token)=([A-Za-z0-9._~+/-]+)", raw, re.I)
    if m:
        val = m.group(1)
        return ("code", val.upper()) if WD_CODE_RE.match(val.upper()) else ("token", val)
    code = extract_wd_code(raw)
    if code:
        return "code", code
    up = raw.upper()
    if WD_CODE_RE.fullmatch(up):  # user cuma nempel/tik kode polos
        return "code", up
    if len(raw) >= 16:
        m = re.search(rf"\b({WD_CODE_STRICT})\b", raw)  # kode nyelip di cuplikan teks
        if m:
            return "code", m.group(1)
        return "token", raw  # tempel token/link mentah
    return None


def imap_fetch_wd_proof(since: float, log: Logger) -> tuple[str, str] | None:
    """Auto-baca kode withdraw dari inbox (butuh SOLFARMER_IMAP_PASS = Gmail app password)."""
    if IMAP_STATE["failed"]:
        return None
    password = os.environ.get("SOLFARMER_IMAP_PASS")
    if not password:
        return None
    try:
        import email as email_lib
        import email.utils as email_utils
        import imaplib
        from email import policy
    except ImportError:
        return None
    user = os.environ.get("SOLFARMER_IMAP_USER") or load_login().get("email")
    host = os.environ.get("SOLFARMER_IMAP_HOST") or "imap.gmail.com"
    try:
        port = int(os.environ.get("SOLFARMER_IMAP_PORT") or "993")
    except ValueError:
        port = 993
    try:
        M = imaplib.IMAP4_SSL(host, port, timeout=30)
    except Exception as exc:
        IMAP_STATE["failed"] = True
        log.log(f"IMAP tidak bisa tersambung ({exc}) — auto-baca kode dimatikan.", "warn")
        return None
    try:
        M.login(user, password)
        M.select("INBOX")
        typ, data = M.search(None, "SINCE", dt.date.today().strftime("%d-%b-%Y"))
        nums = ((data or [b""])[0] or b"").split()[-12:]
        for num in reversed(nums):
            typ, part = M.fetch(num, "(BODY.PEEK[])")
            if not part or not part[0] or not isinstance(part[0], tuple):
                continue
            msg = email_lib.message_from_bytes(part[0][1], policy=policy.default)
            try:
                t = email_utils.parsedate_to_datetime(str(msg.get("Date") or "")).timestamp()
            except Exception:
                t = 0.0
            if t and t < since - 90:
                continue
            texts = [str(msg.get("From") or ""), str(msg.get("Subject") or "")]
            for p in msg.walk():
                if p.get_content_type() in ("text/plain", "text/html"):
                    try:
                        texts.append(str(p.get_content()))
                    except Exception:
                        pass
            blob = "\n".join(texts)
            hay = blob.lower()
            if "solfarmer" not in hay and "withdraw" not in hay:
                continue
            m = re.search(r"withdraw/confirm[^\s\"']*token=([A-Za-z0-9._~+/-]+)", blob, re.I)
            if m:
                return "token", m.group(1)
            plain = re.sub(r"<[^>]+>", " ", blob)
            code = extract_wd_code(plain)
            if code:
                return "code", code
        return None
    except Exception as exc:
        IMAP_STATE["failed"] = True
        log.log(f"IMAP gagal ({exc}) — auto-baca kode dimatikan, pakai prompt/env.", "warn")
        return None
    finally:
        try:
            M.logout()
        except Exception:
            pass


def prompt_wd_proof(pending: dict) -> tuple[str, str] | None:
    """Minta kode ke user di terminal — tunggu TERBATAS supaya siklus tidak macet."""
    if not sys.stdin.isatty():
        return None
    first = int(pending.get("attempts") or 0) == 0
    pending["attempts"] = int(pending.get("attempts") or 0) + 1
    timeout = 60 if first else 10
    print(
        f"🔐 Kode withdraw dari email (tunggu {timeout}s, Enter = lewati): ",
        end="",
        flush=True,
    )
    raw = ""
    pressed_enter = False
    try:
        import select as select_mod

        ready, _, _ = select_mod.select([sys.stdin], [], [], timeout)
        if ready:
            raw = sys.stdin.readline().strip()
            pressed_enter = True
    except Exception:
        try:
            raw = input().strip()
            pressed_enter = True
        except Exception:
            raw = ""
    print(flush=True)
    if pressed_enter and not raw:
        pending["skipped"] = True  # user sudah lewati — jangan ditanya tiap siklus
    return parse_wd_proof(raw) if raw else None


def obtain_wd_proof(pending: dict, log: Logger) -> tuple[str, str] | None:
    """Cari kode konfirmasi: env → IMAP → prompt terminal."""
    env = (os.environ.get("SOLFARMER_WD_CODE") or "").strip()
    if env:
        p = parse_wd_proof(env)
        if p:
            return p
        log.log("SOLFARMER_WD_CODE tidak dikenali (butuh kode 6 huruf atau link /withdraw/confirm).", "warn")
    if os.environ.get("SOLFARMER_IMAP_PASS"):
        since = float(pending.get("ts") or time.time()) - 120
        deadline = time.time() + 36
        while True:
            p = imap_fetch_wd_proof(since, log)
            if p or IMAP_STATE["failed"]:
                return p
            if time.time() >= deadline:
                break
            time.sleep(8)
    if pending.get("skipped"):
        return None
    return prompt_wd_proof(pending)


def confirm_pending(client: Client, wd: dict, pending: dict, log: Logger) -> dict:
    proof = obtain_wd_proof(pending, log)
    save_wd_log(wd)  # simpan attempts/skipped (kode TIDAK pernah disimpan)
    if not proof:
        return {
            "silent": True,
            "reason": "kode email belum dimasukkan (SOLFARMER_WD_CODE / prompt / IMAP)",
        }
    kind, val = proof
    r = client.post("/wallet/withdraw/confirm", {kind: val})
    msg = str(r.get("message") or "Withdraw dikonfirmasi")
    addr = str(pending.get("address") or "")
    short = f"{addr[:8]}...{addr[-6:]}" if len(addr) > 16 else addr
    amount = float(pending.get("amount") or 0)
    wd["withdraws"].append(
        {
            "address": addr,
            "amount": amount,
            "date": pending.get("date") or dt.date.today().isoformat(),
            "timestamp": time.time(),
            "status": "confirmed",
            "message": msg,
        }
    )
    wd.pop("pending", None)
    save_wd_log(wd)
    return {"msg": f"💸 Withdraw {amount:.6f} SOL → {short} — {msg}"}


def withdraw_solfarmer(client: Client, user_id, log: Logger) -> dict:
    """Auto withdraw SOLfarmer.io ke wallet eksternal (endpoint asli, lihat blok di atas)."""
    wd = load_wd_log()
    pending = wd.get("pending")
    now = time.time()

    # 1) Ada permintaan tertunda? selesaikan dulu (tidak kena cooldown).
    if pending:
        if now - float(pending.get("ts") or 0) > WD_PENDING_EXPIRE_SEC:
            wd["withdraws"].append(
                {
                    "address": str(pending.get("address") or ""),
                    "amount": float(pending.get("amount") or 0),
                    "date": dt.date.fromtimestamp(float(pending.get("ts") or now)).isoformat(),
                    "timestamp": float(pending.get("ts") or now),
                    "status": "expired",
                }
            )
            wd.pop("pending", None)
            save_wd_log(wd)
            log.log(
                "Kode withdraw tidak pernah dimasukkan 30 menit — permintaan batal "
                "(kode lama tetap bisa dipakai manual dari email).",
                "warn",
            )
            pending = None
        else:
            return confirm_pending(client, wd, pending, log)

    # 2) Baru kirim permintaan kalau lolos alamat + cap + cooldown.
    allowed, reason = can_withdraw()
    if not allowed:
        return {"silent": True, "reason": reason}
    if not user_id:
        return {"silent": True, "reason": "user id tidak diketahui"}
    try:
        sol, _ssol = ledger_balance(client, user_id)
    except ApiError as exc:
        if exc.status == 401:
            raise
        return {"silent": True, "reason": f"cek saldo gagal ({exc.status})"}
    except Exception as exc:
        return {"silent": True, "reason": f"cek saldo gagal ({exc})"}
    if sol < WD_AMOUNT:
        return {"silent": True, "reason": f"saldo {sol:.4f} < {WD_AMOUNT}"}

    amount = min(WD_AMOUNT, sol - WD_MIN_BALANCE)
    if amount < WD_SITE_MIN:
        return {
            "silent": True,
            "reason": f"sisa setelah ditahan {WD_MIN_BALANCE} SOL = {amount:.4f} < minimum {WD_SITE_MIN}",
        }

    addr = wallet_address()
    r = client.post(
        "/wallet/withdraw",
        {"amount_lamports": str(int(round(amount * 1e9))), "destination_address": addr},
    )
    pending = {
        "amount": amount,
        "address": addr,
        "ts": time.time(),
        "date": dt.date.today().isoformat(),
        "message": str(r.get("message") or ""),
        "attempts": 0,
    }
    wd["pending"] = pending
    save_wd_log(wd)
    log.log(f"📧 Permintaan withdraw {amount:.6f} SOL dikirim — kode konfirmasi ke email.", "info")
    if pending["message"]:
        log.log(f"   server: {pending['message']}", "info")
    return confirm_pending(client, wd, pending, log)


class Runner:
    def __init__(self, client: Client, features: dict[str, bool], log: Logger):
        self.client = client
        self.features = features
        self.log = log
        self.stats = {"ok": 0, "skip": 0, "fail": 0}
        self.user_id = None

    def safe_get(self, path: str, default=None):
        try:
            return self.client.get(path)
        except ApiError as exc:
            if exc.status == 401:
                raise
            return {} if default is None else default
        except Exception:
            return {} if default is None else default

    def step(self, name: str, fn) -> None:
        if not self.features.get(name):
            return
        label = FEATURE_LABELS.get(name, name)
        try:
            result = fn()
            if isinstance(result, dict) and result.get("silent"):
                self.stats["skip"] += result.get("count", 1)
            elif isinstance(result, dict) and result.get("msg"):
                self.stats["ok"] += 1
                self.log.log(result["msg"], "ok")
            else:
                self.stats["ok"] += 1
        except ApiError as exc:
            if exc.status == 401:
                self.stats["fail"] += 1
                self.log.log("Sesi habis (401) — hentikan siklus.", "warn")
                raise
            if is_benign(exc):
                self.stats["skip"] += 1
            else:
                self.stats["fail"] += 1
                self.log.log(f"{label}: gagal ({exc.status} {exc.message})", "err")
        except Exception as exc:
            if is_benign(exc):
                self.stats["skip"] += 1
            else:
                self.stats["fail"] += 1
                self.log.log(f"{label}: gagal ({exc})", "err")
        self.client.tick()

    def run(self) -> None:
        self.stats = {"ok": 0, "skip": 0, "fail": 0}
        self.log.log("Mulai siklus auto…", "info")

        try:
            user_id = None
            try:
                me = self.client.get(f"{AUTH}/auth/me")
                user = me.get("user") or {}
                user_id = user.get("id") or user.get("user_id") or me.get("id")
            except Exception:
                user_id = None
            self.user_id = user_id
            self.client.tick()

            state = None
            try:
                state = self.client.get("/state")
            except ApiError as exc:
                if exc.status == 401:
                    raise
                self.log.log(f"Gagal ambil state: {exc}", "err")
            except Exception as exc:
                self.log.log(f"Gagal ambil state: {exc}", "err")
            self.client.tick()

            self.step("settle", lambda: self.do_settle(user_id))
            self.step("streak", self.do_streak)
            self.step("freeBox", lambda: self.do_free_box(state))
            self.step("quests", lambda: self.do_quests(state))
            self.step("milestones", lambda: self.do_milestones(state))
            self.step("story", lambda: self.do_story(state))
            self.step("campQuests", self.do_camp_quests)
            self.step("expedition", self.do_expedition)
            self.step("pools", self.do_pools)
            self.step("gifts", self.do_gifts)
            self.step("visits", self.do_visits)
            self.step("notifications", self.do_notifications)

            # --- AUTO WITHDRAW ---
            self.step("withdraw", self.do_withdraw)
        except Exception as exc:
            if isinstance(exc, ApiError) and exc.status == 401:
                if self.client.try_relogin():
                    self.log.log("Sesi dipulihkan lewat auto-relogin.", "ok")
                elif not self.client.refresh_token:
                    self.log.log(
                        "Sesi habis — tidak ada sf_refresh_token, "
                        "tempel ulang dari Local Storage solfarmer.io.",
                        "warn",
                    )
                else:
                    self.log.log(
                        "Sesi habis — auto-relogin belum jalan: "
                        f"{self.client.relogin_reason or 'alasan tidak diketahui'}",
                        "warn",
                    )
            else:
                self.log.log(f"Siklus berhenti: {exc}", "err")
        finally:
            s = self.stats
            self.log.log(f"Siklus selesai — OK {s['ok']} · Lewat {s['skip']} · Gagal {s['fail']}", "info")

    def do_settle(self, user_id) -> dict:
        self.client.post("/mining/sync", {})
        self.client.tick()
        st = self.safe_get("/mining/state", None)
        hashrate = float((st or {}).get("hashrate") or 0)
        if hashrate <= 0 and not user_id:
            return {"silent": True}
        res = self.client.post("/mining/settle", {"userId": user_id})
        earned = float(((res.get("settlement") or {}).get("earnings_lamports")) or 0) / 1e9
        if earned > 0:
            return {"msg": f"⛏️ Settle: +{earned:.8f} SOL"}
        return {"silent": True}

    def do_streak(self) -> dict:
        s = self.client.get("/quests/streak")
        today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
        last = str(s.get("lastClaimedAt") or "")[:10] or None
        if last == today:
            return {"silent": True}
        r = self.client.post("/quests/streak/claim")
        sol = float(r.get("rewardLamports") or r.get("reward_lamports") or 0) / 1e9
        days = r.get("streakDays", "?")
        extra = f" (+{sol:.6f} SOL)" if sol > 0 else ""
        return {"msg": f"🔥 Streak hari ke-{days}{extra}"}

    def do_free_box(self, state) -> dict:
        if not state:
            return {"silent": True}
        free = next((c for c in (state.get("cooldowns") or []) if c and c.get("box_id") == "daily-free-box"), None)
        if free and free.get("last_opened_at"):
            try:
                last = dt.datetime.fromisoformat(str(free["last_opened_at"]).replace("Z", "+00:00"))
                if dt.datetime.now(dt.timezone.utc) - last < dt.timedelta(hours=24):
                    return {"silent": True}
            except Exception:
                pass
        r = self.client.post("/lucky-boxes/open", {"box_id": "daily-free-box"})
        if r.get("success") is False:
            return {"silent": True}
        if r.get("reward_sol") is not None:
            sol = float(r.get("reward_sol") or 0)
        else:
            sol = float(r.get("reward_lamports") or 0) / 1e9
        parts = []
        if sol > 0:
            parts.append(f"+{sol:.6f} SOL")
        if r.get("boost_won"):
            boost = f"boost {r.get('boost_percent') or ''}%"
            if r.get("boost_hours"):
                boost += f" {r['boost_hours']}h"
            parts.append(boost)
        detail = f": {' '.join(parts)}" if parts else ""
        return {"msg": f"🎁 Peti gratis dibuka{detail}"}

    def do_quests(self, state) -> dict:
        if not state:
            return {"silent": True}
        defs_res = self.safe_get("/quests")
        defs = defs_res.get("quests") or []
        prog = state.get("quests") or []
        by_id = {q.get("id"): q for q in prog if q}
        n = 0
        for d in defs:
            if not d or d.get("id") is None:
                continue
            p = by_id.get(d.get("id"))
            if not p or p.get("claimed"):
                continue
            target = float(d.get("target") or 0)
            progress = float(p.get("progress") or 0)
            if progress < target:
                continue
            if p.get("completed") is False:
                continue
            try:
                r = self.client.post("/quests/claim", {"questId": d["id"]})
            except ApiError as e1:
                if e1.status == 400:
                    r = self.client.post("/quests/claim", {"quest_id": d["id"]})
                else:
                    raise
            if r.get("success") is not False:
                n += 1
            self.client.tick()
        if n:
            return {"msg": f"🎯 {n} quest diklaim"}
        return {"silent": True}

    def do_milestones(self, state) -> dict:
        if not state:
            return {"silent": True}
        gs = state.get("gameState") or {}
        level = float(gs.get("level") or 0)
        claimed = set(gs.get("claimed_milestones") or [])
        res = self.safe_get("/progression/milestones")
        n = 0
        for m in res.get("milestones") or []:
            if not m or not m.get("milestone_id"):
                continue
            if float(m.get("level") or 0) > level:
                continue
            if m["milestone_id"] in claimed:
                continue
            try:
                r = self.client.post("/quests/milestones/claim", {"milestone_id": m["milestone_id"]})
                if r.get("success") is not False:
                    n += 1
                    claimed.add(m["milestone_id"])
            except Exception as exc:
                if not is_benign(exc):
                    raise
                claimed.add(m["milestone_id"])
            self.client.tick()
        if n:
            return {"msg": f"🏁 {n} milestone diklaim"}
        return {"silent": True}

    def do_story(self, state) -> dict:
        if not state:
            return {"silent": True}
        unclaimed = {
            e.get("story_event_id")
            for e in (state.get("storyProgress") or [])
            if e and e.get("story_event_id") and not e.get("claimed")
        }
        if not unclaimed:
            return {"silent": True}
        cat = self.safe_get("/quests/story/catalog")
        n = 0
        for ev in cat.get("events") or []:
            if not ev or ev.get("id") not in unclaimed:
                continue
            has_reward = (
                float(ev.get("reward_sol_lamports") or 0) > 0
                or float(ev.get("reward_xp") or 0) > 0
                or bool(ev.get("boost"))
            )
            if not has_reward:
                continue
            try:
                self.client.post("/quests/story/viewed", {"story_quest_id": ev["id"]})
            except ApiError as exc:
                if exc.status == 401:
                    raise
            except Exception:
                pass
            r = self.client.post("/quests/story/claim", {"story_quest_id": ev["id"]})
            if r.get("success") is not False:
                n += 1
            self.client.tick()
        if n:
            return {"msg": f"📖 {n} story reward diklaim"}
        return {"silent": True}

    def do_camp_quests(self) -> dict:
        res = self.safe_get("/farmer/quests/daily")
        n = 0
        for q in res.get("quests") or []:
            if not q or q.get("claimed"):
                continue
            if float(q.get("progress") or 0) < float(q.get("target_count") or 0):
                continue
            r = self.client.post("/farmer/quests/claim", {"quest_id": q.get("id")})
            if r.get("success") is not False:
                n += 1
            self.client.tick()
        if n:
            return {"msg": f"🏕️ {n} quest Camp diklaim"}
        return {"silent": True}

    def do_expedition(self) -> dict:
        run = self.safe_get("/farmer/expedition/active", None)
        active = run if run and run.get("id") else (run or {}).get("run")
        if not active or not active.get("id"):
            return {"silent": True}
        ends_raw = active.get("ends_at")
        if not ends_raw:
            return {"silent": True}
        try:
            ends = dt.datetime.fromisoformat(str(ends_raw).replace("Z", "+00:00"))
        except Exception:
            return {"silent": True}
        if dt.datetime.now(dt.timezone.utc) < ends:
            return {"silent": True}
        r = self.client.post("/farmer/expedition/claim", {"run_id": active["id"]})
        if r.get("success") is False:
            return {"silent": True}
        return {"msg": "🧭 Ekspedisi selesai diklaim"}

    def do_pools(self) -> dict:
        un = self.safe_get("/pools/rewards/unclaimed")
        total = float(un.get("total") or 0)
        if total <= 0:
            return {"silent": True}
        # GET /pools kadang 500 di server (bug kolom SQL) — fallback ambil pool_id
        # dari daftar reward yang belum diklaim.
        pool_id = None
        pools_res = self.safe_get("/pools")
        pools = pools_res.get("pools") or []
        mine = next((p for p in pools if p and p.get("is_member")), None) or next(
            (p for p in pools if p and p.get("id")), None
        )
        if mine:
            pool_id = mine.get("id")
        if not pool_id:
            r0 = next((x for x in (un.get("rewards") or []) if x), None) or {}
            pool = r0.get("pool") if isinstance(r0.get("pool"), dict) else {}
            pool_id = r0.get("pool_id") or r0.get("poolId") or pool.get("id")
        if not pool_id:
            return {"silent": True}
        r = self.client.post("/pools/claim", {"pool_id": pool_id})
        if r.get("success") is False:
            return {"silent": True}
        return {"msg": f"💧 Pool reward diklaim ({total / 1e9:.6f} SSOL)"}

    def do_gifts(self) -> dict:
        res = self.safe_get("/social/gifts")
        pending = [g for g in (res.get("received") or []) if g and not g.get("claimed")][:10]
        if not pending:
            return {"silent": True}
        n = 0
        for g in pending:
            r = self.client.post("/social/gifts/claim", {"gift_id": g.get("id")})
            if r.get("success") is not False:
                n += 1
            self.client.tick()
        if n:
            return {"msg": f"🎁 {n} gift diklaim"}
        return {"silent": True}

    def do_visits(self) -> dict:
        res = self.safe_get("/social/farms/visits?role=owner")
        eligible = [v for v in (res.get("visits") or []) if v and v.get("liked") and not v.get("reward_claimed")]
        if not eligible:
            return {"silent": True}
        r = self.client.post("/social/farms/claim-visit-reward", {"visit_id": eligible[0].get("id")})
        if r.get("success") is False:
            return {"silent": True}
        sol = float(r.get("reward_lamports") or 0) / 1e9
        extra = f" (+{sol:.6f} SSOL)" if sol > 0 else ""
        return {"msg": f"🤝 Visit reward diklaim{extra}"}

    def do_notifications(self) -> dict:
        # Live 2026-10-28: cuma /social/notifications/read-all yang jalan,
        # rute *clear-all lain 404 — tetap dicoba untuk jaga-jaga kalau server berubah.
        for path in (
            "/social/notifications/read-all",
            "/notifications/clear-all",
            "/social/notifications/clear-all",
            "/quests/notifications/clear-all",
        ):
            try:
                self.client.post(path, {})
                return {"silent": True}
            except ApiError as exc:
                if exc.status == 401:
                    raise
                continue
            except Exception:
                continue
        return {"silent": True}

    # --- AUTO WITHDRAW ---
    def do_withdraw(self) -> dict:
        return withdraw_solfarmer(self.client, self.user_id, self.log)


def resolve_credentials(log: Logger) -> tuple[str, str | None, str | None]:
    token = refresh = raw = None

    saved_token, saved_refresh = load_saved_session()
    if saved_token:
        token, refresh = saved_token, saved_refresh

    if not token:
        env = os.environ.get("SOLFARMER_COOKIE") or os.environ.get("SOLFARMER_TOKEN")
        if env:
            token, refresh = parse_session(env)
            raw = env

    if not refresh:
        env_refresh = os.environ.get("SOLFARMER_REFRESH")
        if env_refresh:
            refresh = env_refresh.strip().strip('"').strip("'")

    if not token and not sys.stdin.isatty():
        piped = sys.stdin.read().strip()
        if piped:
            token, refresh = parse_session(piped)
            raw = piped

    if not token and sys.stdin.isatty():
        print("Tempel cookie/token sesi (sf_access_token + sf_refresh_token dari localStorage):")
        raw = input("> ").strip()
        token, refresh = parse_session(raw)
        if not token:
            print("Cookie/token kosong.", file=sys.stderr)
            sys.exit(2)

    if not token:
        print(
            "Sesi belum ada. Tempel cookie lewat stdin, set env SOLFARMER_COOKIE, "
            "atau jalankan sekali di terminal buat prompt.\n"
            "Cara ambil: DevTools → Application → Local Storage → solfarmer.io → sf_access_token",
            file=sys.stderr,
        )
        sys.exit(2)

    if not refresh and sys.stdin.isatty():
        print(
            "sf_refresh_token belum ada — tanpa ini sesi tidak bisa diperpanjang otomatis.\n"
            "Ambil juga dari DevTools → Application → Local Storage → solfarmer.io → sf_refresh_token\n"
            "(Enter untuk lewati, tapi nanti kena login ulang saat token habis ±15 menit):"
        )
        extra = input("> ").strip()
        if extra:
            _, parsed_refresh = parse_session(extra)
            refresh = parsed_refresh or extra.strip('"').strip("'") or None

    return token, refresh, raw


def setup_login_creds(log: Logger) -> None:
    """Prompt sekali untuk kredensial auto-relogin (password tidak lewat chat)."""
    try:
        if load_state().get("relogin_rejected"):
            return  # sudah pernah ditolak server — jangan ganggu tiap run
    except Exception:
        pass
    creds = load_login()
    if creds.get("password") and creds.get("apikey"):
        return
    if not sys.stdin.isatty():
        return
    print(
        "Auto-relogin (cadangan kalau refresh token putus) butuh kredensial sekali.\n"
        "Aman: disimpan di ~/.solfarmer_login.json (chmod 600).\n"
        "Tekan Enter untuk lewati (fitur mati, sesi hanya pakai refresh token)."
    )
    email = creds.get("email")
    password = creds.get("password")
    if not password:
        password = getpass.getpass("Password akun SolFarmer (Enter=lewati): ").strip() or None
    if password:
        _secure_write(LOGIN_FILE, {"email": email, "password": password, "apikey": creds.get("apikey")})
        log.log("Kredensial auto-relogin tersimpan (apikey solver default aktif).", "ok")
    else:
        log.log("Auto-relogin tidak diaktifkan (tanpa password).", "warn")


def setup_wd_addr(log: Logger) -> None:
    """Prompt sekali untuk alamat wallet auto-withdraw (tidak menimpa log withdraw)."""
    if wallet_address():
        return  # sudah ada (env atau tersimpan) — jangan timpa riwayat
    if not sys.stdin.isatty():
        return
    print(
        "Auto-withdraw butuh alamat wallet Solana sekali.\n"
        "Aman: disimpan di ~/.solfarmer_wd_log.json (chmod 600).\n"
        "Cek ulang karakternya — 1 typo = dana hilang permanen.\n"
        "Tekan Enter untuk lewati (fitur mati, saldo tetap diaccumulasi)."
    )
    addr = getpass.getpass("Alamat wallet Solana (public key, Enter=lewati): ").strip() or None
    if addr and not WD_BASE58_RE.fullmatch(addr):
        log.log("Alamat tidak valid (harus base58 Solana, 32-44 karakter) — tidak disimpan.", "warn")
        addr = None
    if addr:
        data = load_wd_log()
        data.update({"configured": True, "address": addr})
        save_wd_log(data)
        log.log("Alamat wallet auto-withdraw tersimpan.", "ok")
    else:
        log.log("Auto-withdraw tidak diaktifkan (tanpa alamat wallet).", "warn")


def main() -> int:
    use_color = sys.stdout.isatty()
    log = Logger(use_color)

    token, refresh, raw_cookie = resolve_credentials(log)
    setup_login_creds(log)
    setup_wd_addr(log)
    if refresh:
        save_session(token, refresh)
    elif not token_exp(token) or (token_exp(token) or 0) < time.time() + REFRESH_MARGIN_SEC:
        log.log(
            "Token sudah mau habis dan tidak ada refresh token — "
            "perpanjangan otomatis tidak bisa jalan.",
            "warn",
        )

    cookie_header = None
    if raw_cookie and "sf_access_token" in raw_cookie:
        cookie_header = raw_cookie

    features = dict(DEFAULT_FEATURES)
    active = [FEATURE_LABELS[k] for k in FEATURE_ORDER if features[k]]
    log.log(f"Fitur aktif ({len(active)}): {', '.join(active)}", "info")

    try:
        st = load_state()
        creds = load_login()
        if creds.get("apikey"):
            bal = check_balance(creds["apikey"])
            if bal is not None:
                log.log(
                    f"Saldo solver: {bal} token • terpakai hari ini: {st.get('tokens_spent', 0)}",
                    "info",
                )
    except Exception:
        pass

    client = Client(token, refresh, cookie_header, DELAY_SEC, log)
    runner = Runner(client, features, log)

    try:
        me = client.get(f"{AUTH}/auth/me")
        user = me.get("user") or {}
        name = user.get("username") or user.get("email") or user.get("id") or "?"
        save_session(client.token, client.refresh_token)
        log.log(f"Terkoneksi sebagai: {name}", "ok")
    except ApiError as exc:
        if exc.status == 401:
            if client.try_relogin():
                try:
                    me = client.get(f"{AUTH}/auth/me")
                    user = me.get("user") or {}
                    name = user.get("username") or user.get("email") or user.get("id") or "?"
                    log.log(f"Terkoneksi sebagai: {name} (auto-relogin)", "ok")
                except ApiError as exc2:
                    log.log(f"Gagal cek sesi setelah relogin: {exc2.status} {exc2.message}", "err")
                    return 1
            elif not client.refresh_token:
                log.log(
                    f"Token kedaluwarsa, auto-relogin tidak aktif. "
                    f"Jalankan ulang untuk isi password, atau tempel "
                    f"sf_access_token + sf_refresh_token dari browser "
                    f"(DevTools → Local Storage → solfarmer.io).",
                    "err",
                )
                return 1
            else:
                log.log(
                    "Refresh token ditolak dan auto-relogin belum jalan: "
                    + (client.relogin_reason or "alasan tidak diketahui"),
                    "err",
                )
                return 1
        else:
            log.log(f"Gagal cek sesi: {exc.status} {exc.message}", "warn")

    # Info saldo + kesiapan auto-withdraw sekali di awal (me ada di kedua jalur connect)
    if features.get("withdraw"):
        try:
            uid = (me.get("user") or {}).get("id") or me.get("id")
            bal = ledger_balance(client, uid)[0]
            addr = wallet_address()
            shown = f"{addr[:8]}...{addr[-6:]}" if len(addr) > 16 else (addr or "BELUM DIISI")
            log.log(f"💰 Saldo: {bal:.6f} SOL • Auto-wd ≥ {WD_AMOUNT} SOL → {shown}", "info")
        except Exception:
            pass

    # Mode sekali-jalan (dipakai GitHub Actions / cron): 1 siklus lalu exit.
    if os.environ.get("SOLFARMER_ONCE", "").strip().lower() in ("1", "true", "yes"):
        runner.run()
        return 0

    log.log(f"Mode loop — tiap {INTERVAL_SEC}s (Ctrl+C untuk berhenti)", "info")
    try:
        while True:
            runner.run()
            time.sleep(INTERVAL_SEC)
    except KeyboardInterrupt:
        log.log("Dihentikan oleh user.", "warn")
        return 0


if __name__ == "__main__":
    sys.exit(main())
