# -*- coding: utf-8 -*-

# ==========================
# Imports
# ==========================
import asyncio
import os
import random
import re
import ssl
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, unquote, urlparse, parse_qs, urlencode, urlunparse

import httpx
from bs4 import BeautifulSoup
from PIL import Image

from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QScrollArea, QSplitter,
    QLabel, QLineEdit, QSpinBox, QDoubleSpinBox, QCheckBox, QPushButton,
    QTextEdit, QFileDialog, QFrame, QProgressBar, QTreeWidget,
    QTreeWidgetItem, QTabWidget,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QObject
from PyQt6.QtGui import QColor, QIcon, QTextCharFormat, QTextCursor, QFont
from SplashScreenPython.splash_video_webP import SplashScreen

# httpx kann "Content-Encoding: br" (Brotli) nur dekodieren wenn das optionale
# 'brotli'/'brotlicffi'-Paket installiert ist. Fehlt es, wird eine Brotli-
# Antwort NICHT mit einem Fehler quittiert, sondern httpx liefert bei r.text
# stillschweigend unbrauchbaren/kaputten Text (Cloudflare-Seiten senden fast
# immer 'br' wenn der Client es im Accept-Encoding-Header anbietet). Das führt
# dazu dass eine eigentlich erfolgreich geladene Seite wie leer/fehlerhaft
# aussieht (z.B. "Keine Kategorien gefunden" obwohl die Seite existiert).
# Daher: 'br' nur anbieten wenn es auch dekodiert werden kann.
try:
    import brotli  # noqa: F401
    _BROTLI_AVAILABLE = True
except ImportError:
    try:
        import brotlicffi  # noqa: F401
        _BROTLI_AVAILABLE = True
    except ImportError:
        _BROTLI_AVAILABLE = False

# ==========================
# Datenstrukturen
# ==========================

NAMEandVERSION = "Gallery Downloader \nFlareSolverr \nVersion 0.46\n"

# Sentinel: Seite geladen, aber permanent kein Bild vorhanden → kein Retry
_SKIP_PERMANENT = object()

# Dateinamen / Pfad-Fragmente die auf ein Platzhalter-Bild hinweisen
# (case-insensitive, wird gegen den vollen src-Pfad geprüft)
_PLACEHOLDER_SRCS = (
    "thumb_nopic",
    "nopic",
    "no_pic",
    "no-pic",
    "nophoto",
    "no_photo",
    "no-photo",
    "missing",
    "placeholder",
    "not_found",
    "notfound",
    "image_not_available",
    "unavailable",
    "default_image",
    "blank",
    "spacer",
)


# ==========================
# Adaptiver Rate-Limiter (AIMD)
# ==========================

class RateLimiter:
    """
    Adaptiver Rate-Limiter nach AIMD-Prinzip (wie TCP Congestion Control):
      - Jede erfolgreiche Anfrage → Delay × _DECREASE (langsam beschleunigen)
      - Jeder ConnectError/Timeout → Delay × _INCREASE (sofort abbremsen)
      - Delay bleibt zwischen _MIN und _MAX

    Alle Requests des Programms teilen eine Instanz pro Host → globale
    Drosselung. Bei concurrency=1 ist der Delay de facto sequentiell.
    """
    _DECREASE = 0.90    # nach Erfolg: Delay um 10% reduzieren
    _INCREASE = 2.00    # nach Fehler:  Delay verdoppeln
    _MIN      = 0.2     # Untergrenze in Sekunden
    _MAX      = 30.0    # Obergrenze in Sekunden
    _JITTER   = 0.15    # ± zufällige Abweichung (verhindert Request-Synchronisierung)

    def __init__(self, initial: float = 1.0) -> None:
        self._delay = max(self._MIN, min(self._MAX, initial))
        self._lock  = None   # wird beim ersten await initialisiert

    @property
    def current_delay(self) -> float:
        return round(self._delay, 2)

    async def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def wait(self) -> None:
        """Vor jedem Request aufrufen."""
        jitter = random.uniform(-self._JITTER, self._JITTER) * self._delay
        await asyncio.sleep(max(0.0, self._delay + jitter))

    async def success(self) -> None:
        """Nach jedem erfolgreichen Request aufrufen."""
        lock = await self._get_lock()
        async with lock:
            self._delay = max(self._MIN, self._delay * self._DECREASE)

    async def failure(self) -> None:
        """Nach jedem ConnectError oder Timeout aufrufen."""
        lock = await self._get_lock()
        async with lock:
            self._delay = min(self._MAX, self._delay * self._INCREASE)

    def __repr__(self) -> str:
        return f"RateLimiter(delay={self.current_delay}s)"


@dataclass
class AlbumInfo:
    """Repräsentiert ein einzelnes Album in der Gallery-Struktur."""
    name:        str                    # Album-Name
    thumb_url:   str                    # thumbnails.php?album=X URL
    dest_folder: Path                   # Zielpfad auf Disk
    path_parts:  list[str] = field(default_factory=list)  # [Kat, SubKat, SubSubKat, ...]
    status:      str = "new"            # "new" | "skip" | "resume"
    image_count: int = 0


# ==========================
# Worker-Signals
# ==========================

class WorkerSignals(QObject):
    info          = pyqtSignal(str)
    error         = pyqtSignal(str)
    abort         = pyqtSignal(str)   # Programm-Abbruch wegen zu vieler Fehler

    # URL-Auflösung: init(total), tick(resolved, total)
    resolve_init  = pyqtSignal(int)
    resolve_tick  = pyqtSignal(int, int)

    # Downloads
    dl_start      = pyqtSignal(str, int)
    dl_progress   = pyqtSignal(str, int)
    dl_done       = pyqtSignal(str)
    dl_error      = pyqtSignal(str, str)

    # Gallery-Modus: Struktur
    gallery_album = pyqtSignal(object)    # AlbumInfo – neues Album entdeckt
    album_active  = pyqtSignal(str)       # aktuell verarbeitetes Album (dest_folder als str)
    album_done    = pyqtSignal(str)       # Album abgeschlossen (dest_folder als str)


# ==========================
# Browser-Profile
# ==========================
# Pro Profil alle zusammengehörigen Header damit ein konsistenter
# Browser-Fingerprint entsteht. Cloudflare und ähnliche Bot-Filter prüfen
# z.B. ob die sec-ch-ua Headers zum User-Agent passen — Mismatch = Bot.
_BROWSER_PROFILES = [
    {
        "name": "Chrome 124 Windows",
        "headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124.0.0.0 Safari/537.36",
            "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "sec-ch-ua-mobile":   "?0",
            "sec-ch-ua-platform": '"Windows"',
        },
    },
    {
        "name": "Chrome 124 macOS",
        "headers": {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124.0.0.0 Safari/537.36",
            "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "sec-ch-ua-mobile":   "?0",
            "sec-ch-ua-platform": '"macOS"',
        },
    },
    {
        "name": "Firefox 125 Windows",
        "headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) "
                          "Gecko/20100101 Firefox/125.0",
            # Firefox sendet keine sec-ch-ua Headers
        },
    },
    {
        "name": "Safari 17 macOS",
        "headers": {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) "
                          "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                          "Version/17.4.1 Safari/605.1.15",
            # Safari sendet keine sec-ch-ua Headers
        },
    },
]

# Aktives Browser-Profil – wird einmal pro Session gewählt damit alle
# Requests konsistente Headers haben (Cloudflare verfolgt UA-Wechsel)
_active_profile: dict | None = None


def _get_profile() -> dict:
    global _active_profile
    if _active_profile is None:
        _active_profile = random.choice(_BROWSER_PROFILES)
    return _active_profile


_COMPLETE_MARKER = ".complete"


def make_headers(base_url: str, referer: str | None = None) -> dict:
    """
    Browser-realistische Headers.

    referer:
      - None (Default)  → kein Referer-Header (wie beim Direkt-Aufruf in Adressleiste)
      - str             → expliziter Referer (z.B. die vorherige Seite)
    """
    profile = _get_profile()
    is_chrome = "Chrome" in profile["headers"]["User-Agent"] and "Firefox" not in profile["headers"]["User-Agent"]
    is_firefox = "Firefox" in profile["headers"]["User-Agent"]

    # Accept-Header passend zum Browser
    if is_firefox:
        accept = "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
    else:
        # Chrome / Safari
        accept = ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                  "image/avif,image/webp,image/apng,*/*;q=0.8,"
                  "application/signed-exchange;v=b3;q=0.7")

    headers: dict = {
        **profile["headers"],
        "Accept": accept,
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br" if _BROTLI_AVAILABLE else "gzip, deflate",
        "Upgrade-Insecure-Requests": "1",
    }

    # Sec-Fetch-Headers (Chrome/Edge/aktuelles Firefox)
    if is_chrome or is_firefox:
        # Bei Referer → Navigation von einer Seite, sonst direkt
        headers["Sec-Fetch-Dest"] = "document"
        headers["Sec-Fetch-Mode"] = "navigate"
        headers["Sec-Fetch-Site"] = "same-origin" if referer else "none"
        headers["Sec-Fetch-User"] = "?1"

    if referer:
        headers["Referer"] = referer

    # Wichtig: KEIN Cache-Control auf "no-cache" – das senden Browser nur
    # bei Hard-Reload (Ctrl+Shift+R), nie bei normaler Navigation.
    # KEIN explizites Connection: keep-alive – ist seit HTTP/1.1 Default und
    # wird von echten Browsern bei HTTPS gar nicht mehr gesendet (HTTP/2).

    return headers


# ==========================
# FlareSolverr Adapter
# ==========================
# FlareSolverr ist eine API (kein Proxy!) die Cloudflare-Challenges
# durch automatisiertes Browser-Driving löst. Wir senden POST-Anfragen
# an http://localhost:8191 (Standard-Port) mit JSON-Body.
#
# Strategie:
#   - HTML-Seiten (Index, Albums, Thumbnails, Display-Pages) gehen über
#     FlareSolverr – das löst Cloudflare-Challenges und liefert das HTML.
#   - FlareSolverr gibt uns Cookies und User-Agent zurück.
#   - Bild-Downloads gehen DIREKT über httpx mit den gespeicherten Cookies
#     und User-Agent – das ist 100x schneller als jeden Download durch
#     einen Browser zu routen.

class FlareSolverrError(Exception):
    pass


class FlareSolverrClient:
    """
    Adapter der das FlareSolverr-Protokoll kapselt und nach außen wie
    httpx.AsyncClient aussieht (für .get()/.stream() der HTML-Seiten).

    Bild-Downloads laufen NICHT durch diese Klasse – dafür hält sie
    self.session_cookies + self.session_ua bereit, die der Download-Client
    direkt verwenden kann.
    """

    def __init__(
        self,
        endpoint: str,
        target_domain: str,
        max_timeout_ms: int = 60000,
    ) -> None:
        self.endpoint        = endpoint.rstrip("/")
        self.target_domain   = target_domain
        self.max_timeout     = max_timeout_ms
        self.session_id: str | None = None
        # Wird nach erstem erfolgreichen Request gefüllt – dient als
        # "Identität" für direkte Bild-Downloads
        self.session_cookies: dict[str, str] = {}
        self.session_ua: str = ""
        # Eigener httpx-Client zum Reden mit FlareSolverr selbst
        self._http = httpx.AsyncClient(timeout=max_timeout_ms / 1000 + 30)

    async def __aenter__(self) -> "FlareSolverrClient":
        # FlareSolverr-Session erstellen (hält Browser-Tab am Leben über
        # mehrere Requests → spart Cloudflare-Challenge-Zeit)
        try:
            r = await self._http.post(
                self.endpoint,
                json={"cmd": "sessions.create"},
                timeout=30,
            )
            data = r.json()
            if data.get("status") == "ok":
                self.session_id = data.get("session")
        except Exception:
            # Wenn Sessions nicht unterstützt werden, weiter ohne
            self.session_id = None
        return self

    async def __aexit__(self, *_exc) -> None:
        if self.session_id:
            try:
                await self._http.post(
                    self.endpoint,
                    json={"cmd": "sessions.destroy", "session": self.session_id},
                    timeout=10,
                )
            except Exception:
                pass
        await self._http.aclose()

    async def get(self, url: str, **_kwargs) -> "FlareSolverrResponse":
        """
        Holt eine Seite über FlareSolverr.
        Zusätzliche kwargs (timeout, headers, follow_redirects) werden ignoriert
        weil FlareSolverr seine eigenen Defaults benutzt.
        """
        payload = {
            "cmd":        "request.get",
            "url":        url,
            "maxTimeout": self.max_timeout,
        }
        if self.session_id:
            payload["session"] = self.session_id

        try:
            r = await self._http.post(self.endpoint, json=payload)
            data = r.json()
        except Exception as e:
            raise FlareSolverrError(f"FlareSolverr-API nicht erreichbar: {e}")

        if data.get("status") != "ok":
            raise FlareSolverrError(
                f"FlareSolverr-Fehler: {data.get('message', 'unbekannt')}"
            )

        sol = data.get("solution", {})
        # Cookies und User-Agent für direkte Bild-Downloads merken
        cookies = sol.get("cookies", [])
        for c in cookies:
            self.session_cookies[c["name"]] = c["value"]
        if sol.get("userAgent"):
            self.session_ua = sol["userAgent"]

        return FlareSolverrResponse(
            url=sol.get("url", url),
            status_code=sol.get("status", 200),
            text=sol.get("response", ""),
            headers=sol.get("headers", {}) or {},
        )

    async def head(self, url: str, **_kwargs) -> "FlareSolverrResponse":
        """
        FlareSolverr kann kein HEAD – wir würden einen kompletten Browser
        starten was viel zu teuer ist. Stattdessen geben wir eine
        synthetische "OK"-Antwort zurück; die echte Validierung passiert
        beim tatsächlichen Bild-Download über den direkten httpx-Client.
        """
        return FlareSolverrResponse(
            url=url,
            status_code=200,
            text="",
            headers={"content-type": "image/jpeg", "content-length": "999999"},
        )

    def stream(self, *args, **kwargs):
        """
        Streaming wird nicht über FlareSolverr unterstützt – sollte nie
        aufgerufen werden, da Downloads direkt über den dl_client laufen.
        """
        raise NotImplementedError(
            "FlareSolverrClient unterstützt kein streaming. "
            "Downloads müssen über den direkten httpx-Client laufen."
        )

    async def aclose(self) -> None:
        # Für Kompatibilität mit httpx-API
        pass


class FlareSolverrResponse:
    """Mimics httpx.Response für die Felder die unser Code benutzt."""
    def __init__(self, url: str, status_code: int, text: str, headers: dict) -> None:
        self.url          = url
        self.status_code  = status_code
        self.text         = text
        self.headers      = headers
        self.reason_phrase = "OK" if status_code == 200 else "FlareSolverr Status"

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    def raise_for_status(self) -> None:
        if not self.is_success:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=None,
                response=None,
            )


# ==========================
# Hybrid HTTP Client
# ==========================
# Strategie "Weg 3":
#   1. Erste Anfrage geht DIREKT (schnell)
#   2. Wenn die Antwort Cloudflare-typisch ist (403/503 + Cloudflare-Marker)
#      → einmaliger FlareSolverr-Aufruf um cf_clearance + UA zu holen
#   3. Diese Cookies in den internen Client übertragen
#   4. Alle weiteren Anfragen wieder DIREKT – schnell wie immer
#   5. Wenn später ein Request wieder blockiert wird → Session erneuern
#
# Damit zahlt man den FlareSolverr-Preis (~15s) nur 1-2x pro Galerie statt
# bei jeder einzelnen URL.

# Erkennungsmuster für Cloudflare-Challenges in der Antwort
_CF_BLOCK_MARKERS = (
    "challenge-platform",
    "cf-mitigated",
    "cf_chl_opt",
    "checking your browser",
    "just a moment",
    "ddos protection by cloudflare",
    "cf-browser-verification",
    "ray id",
    "__cf_chl_jschl_tk__",
    "_cf_chl_opt",
)

# Marker die NUR auf der eigentlichen Interstitial-Seite vorkommen, nicht in
# Fußzeilen o.ä. normaler Seiten. Werden auch bei HTTP 200 geprüft, denn
# Cloudflares "Managed Challenge" liefert die Challenge-Seite oft mit
# Status 200 statt 403/503 aus (dann liefert die Seite z.B. keine Alben-
# oder Kategorie-Links, obwohl der Request formal erfolgreich war).
# WICHTIG: "challenge-platform" bewusst NICHT hier – Cloudflare bindet das
# zugehörige Skript (/cdn-cgi/challenge-platform/...) als passive Bot-
# Erkennung auch auf ganz normalen, erfolgreich geladenen Seiten ein
# (false positive, real beobachtet auf kate-beckinsale.com).
_CF_CHALLENGE_STRONG_MARKERS = (
    "cf_chl_opt",
    "checking your browser",
    "just a moment",
    "__cf_chl_jschl_tk__",
    "_cf_chl_opt",
    "cf-turnstile",
    "enable javascript and cookies to continue",
    "verify you are human",
)


def _looks_like_cf_challenge(status: int, body: str, response_headers: dict | None = None) -> bool:
    """
    True wenn die Antwort eine Cloudflare-Challenge oder ein Cloudflare-Block ist.
    Konservative Heuristik: braucht entweder einen expliziten Cloudflare-Header
    ODER einen typischen Cloudflare-Marker im Body. Reine kurze 403/503
    werden NICHT mehr automatisch als CF erkannt – das gab false positives
    nach erfolgreicher Session-Erneuerung.

    Cloudflares "Managed Challenge" liefert die Interstitial-Seite häufig mit
    HTTP 200 statt 403/503 aus – dann sieht die Antwort für den Aufrufer wie
    ein normaler Seitenaufruf aus, enthält aber keinen echten Seiteninhalt
    (z.B. keine Kategorien/Alben). Das wird unabhängig vom Status-Code anhand
    eindeutiger Interstitial-Marker erkannt.
    """
    body_lower = (body or "").lower()
    if any(m in body_lower for m in _CF_CHALLENGE_STRONG_MARKERS):
        return True

    if status not in (403, 503, 429):
        return False

    # Header-Hinweise (sehr verlässlich)
    if response_headers:
        hdrs = {k.lower(): str(v).lower() for k, v in response_headers.items()}
        if "cf-mitigated" in hdrs:
            return True
        if "cf-ray" in hdrs and status != 200:
            return True
        server = hdrs.get("server", "")
        if "cloudflare" in server and status in (403, 503):
            # Cloudflare-Server + Block-Status → wahrscheinlich CF-Block
            # Aber nur wenn auch Body-Marker oder gar kein Body
            body_lower = (body or "").lower()
            if not body or any(m in body_lower for m in _CF_BLOCK_MARKERS):
                return True

    # Body-Hinweise: nur sehr eindeutige Cloudflare-Texte
    body_lower = (body or "").lower()
    if any(m in body_lower for m in _CF_BLOCK_MARKERS):
        return True

    return False


class HybridClient:
    """
    Hybrid HTTP-Client: macht alle Anfragen direkt mit httpx (schnell).
    Bei erkannter Cloudflare-Blockade einmaliger FlareSolverr-Call um
    cf_clearance Cookies zu holen, danach wieder direkt.

    Bei force_via_fs=True (Vollmodus): ALLE HTML-Anfragen laufen über
    FlareSolverr. Langsam aber zuverlässig bei Sites mit aggressivem
    Cloudflare-Schutz (TLS-Fingerprint Detection, Bot-Score, etc).

    Nach außen wie httpx.AsyncClient nutzbar: .get(), .head(), .stream()
    """

    def __init__(
        self,
        direct_client: httpx.AsyncClient,
        flaresolverr: "FlareSolverrClient | None",
        sig: "WorkerSignals",
        headers: dict,
        force_via_fs: bool = False,
    ) -> None:
        self.direct       = direct_client
        self.fs           = flaresolverr
        self.sig          = sig
        self.headers      = headers
        self.force_via_fs = force_via_fs
        self._fs_calls    = 0
        self._fs_lock     = asyncio.Lock()

    async def get(self, url: str, **kwargs):
        """
        GET-Anfrage. Bei force_via_fs=True: alles über FlareSolverr.
        Sonst: direkt mit Fallback auf FlareSolverr bei Cloudflare-Block.
        """
        kwargs.setdefault("follow_redirects", True)
        if "headers" not in kwargs:
            kwargs["headers"] = self.headers

        # Vollmodus: alles direkt über FlareSolverr
        if self.force_via_fs and self.fs:
            self._fs_calls += 1
            try:
                return await self.fs.get(url)
            except FlareSolverrError as e:
                self.sig.error.emit(
                    f"   ✗ FlareSolverr-Fehler bei {url}: {e}"
                )
                # Fallback auf direkt – besser als gar nichts
                return await self.direct.get(url, **kwargs)

        # Hybrid: Versuch 1 direkt
        try:
            r = await self.direct.get(url, **kwargs)
        except httpx.RequestError:
            raise

        if not self.fs:
            return r
        if not _looks_like_cf_challenge(r.status_code, r.text, dict(r.headers)):
            return r

        # Cloudflare-Block → FlareSolverr
        self.sig.info.emit(
            f"   🛡 Cloudflare-Block erkannt (HTTP {r.status_code}) – "
            f"FlareSolverr wird gestartet …"
        )
        async with self._fs_lock:
            fs_response = await self._refresh_via_flaresolverr(url)
            if fs_response is None:
                return r

        return fs_response

    async def head(self, url: str, **kwargs):
        """HEAD bleibt direkt – FlareSolverr unterstützt kein HEAD."""
        kwargs.setdefault("follow_redirects", True)
        if "headers" not in kwargs:
            kwargs["headers"] = self.headers
        return await self.direct.head(url, **kwargs)

    def stream(self, method: str, url: str, **kwargs):
        """Streaming bleibt direkt – wird nur für Bild-Downloads benutzt."""
        kwargs.setdefault("follow_redirects", True)
        if "headers" not in kwargs:
            kwargs["headers"] = self.headers
        return self.direct.stream(method, url, **kwargs)

    async def _refresh_via_flaresolverr(self, url: str):
        """
        Ruft FlareSolverr für die URL auf. Bei Erfolg:
          - Cookies und User-Agent werden in self.direct übernommen
          - Gibt die FlareSolverrResponse zurück (mit dem HTML)
        Bei Fehlschlag: None.
        """
        if not self.fs:
            return None

        self._fs_calls += 1
        try:
            fs_response = await self.fs.get(url)
        except FlareSolverrError as e:
            self.sig.error.emit(f"   ✗ FlareSolverr-Fehler: {e}")
            return None
        except Exception as e:
            self.sig.error.emit(f"   ✗ FlareSolverr unerwarteter Fehler: {e}")
            return None

        # Cookies übertragen
        for name, value in self.fs.session_cookies.items():
            self.direct.cookies.set(name, value)
        if self.fs.session_ua:
            self.headers["User-Agent"] = self.fs.session_ua

        self.sig.info.emit(
            f"   ✓ Cloudflare-Session erneuert "
            f"({len(self.fs.session_cookies)} Cookies, "
            f"FS-Calls bisher: {self._fs_calls})"
        )
        return fs_response

    async def aclose(self) -> None:
        pass   # Wird vom Aufrufer per direct.aclose() erledigt


# ==========================
# Utilities
# ==========================

def verify_image(img_path: Path) -> bool:
    """Vollständige Bildverifizierung via PIL – nur nach Download verwenden."""
    try:
        with Image.open(img_path) as img:
            img.verify()
        return True
    except Exception:
        return False


def file_looks_complete(img_path: Path, min_bytes: int = 1024) -> bool:
    """Schnelle Größen-Prüfung – nur intern für den ersten Grobfilter."""
    try:
        return img_path.stat().st_size >= min_bytes
    except OSError:
        return False


def file_is_valid(img_path: Path) -> bool:
    """
    Schneller Skip-Check: Datei muss existieren und mindestens 1 KB groß sein.
    Keine PIL-Verifikation — die wäre bei tausenden Skips zu langsam und
    blockiert das Event-Loop. Beschädigte Partial-Downloads werden bereits
    vom Download-Worker selbst im except-Block gelöscht, daher tauchen sie
    hier normalerweise gar nicht auf.
    """
    return file_looks_complete(img_path)


def safe_dirname(name: str) -> str:
    """Bereinigt einen String für die Verwendung als Ordnername.
    Kürzt NICHT – volle Länge wird beibehalten. Kürzung erfolgt
    nur wenn nötig über fit_path().
    """
    name = unquote(name).strip()
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = re.sub(r'\s+', " ", name)
    name = name.rstrip(". ")   # trailing dots/spaces sind auf Windows ungültig
    return name or "unnamed"


# Windows MAX_PATH ohne \\?\ Prefix
_WIN_MAX_PATH = 259
# Mindestlänge eines gekürzten Komponentennamens
_MIN_COMPONENT_LEN = 8


def fit_path(p: Path) -> Path:
    """Kürzt den letzten Pfad-Bestandteil nur wenn der Gesamtpfad das
    Windows MAX_PATH-Limit überschreiten würde.
    Auf anderen OS wird p unverändert zurückgegeben.
    """
    if sys.platform != "win32":
        return p
    full = os.path.abspath(str(p))
    if len(full) <= _WIN_MAX_PATH:
        return p

    parent = os.path.abspath(str(p.parent))
    stem   = p.stem
    suffix = p.suffix

    max_name_len = _WIN_MAX_PATH - len(parent) - 1 - len(suffix)
    max_name_len = max(max_name_len, _MIN_COMPONENT_LEN)

    short_stem = stem[:max_name_len].rstrip(". ")
    return Path(parent) / (short_stem + suffix)


def long_path(p: Path) -> Path:
    """Gibt einen Pfad mit \\\\?\\ Prefix zurück um das Windows MAX_PATH-Limit zu umgehen."""
    if sys.platform != "win32":
        return p
    s = os.path.abspath(str(p))
    if not s.startswith("\\\\?\\"):
        s = "\\\\?\\" + s
    return Path(s)


def build_page_url(base_url: str, page: int) -> str:
    parsed = urlparse(base_url)
    params = parse_qs(parsed.query, keep_blank_values=True)
    params["page"] = [str(page)]
    new_query = urlencode({k: v[0] for k, v in params.items()})
    return urlunparse(parsed._replace(query=new_query))


def looks_like_full_image(src: str) -> bool:
    if not src:
        return False
    lower = src.lower()
    if not lower.endswith((".jpg", ".jpeg", ".png", ".webp")):
        return False

    # Pfad-Segmente auf bekannte UI/Icon-Verzeichnisse prüfen
    path_lower = lower.split("?")[0]
    _ICON_DIRS = ("images/icons/", "images/icons\\", "/icons/", "themes/", "css/")
    if any(d in path_lower for d in _ICON_DIRS):
        return False

    # Nur Dateiname prüfen, nicht den gesamten Pfad
    filename = path_lower.split("/")[-1]
    stem = filename.rsplit(".", 1)[0]   # Dateiname ohne Endung

    # ── EXAKTE Stem-Treffer (Datei heißt genau so) ─────────────────────
    # Diese Navigations-Icons existieren als start.png, end.png usw.
    # WICHTIG: nur ganzer Stem zählt – sonst werden Dateien wie
    # "attends_and_performs.jpg" fälschlich geblockt (enthält "end").
    _EXACT_STEMS = {
        "start", "prev", "next", "end",
        "tab_left", "tab_right", "tab_first", "tab_last",
        "spacer", "blank", "pixel", "dot", "arrow",
        "logo", "banner", "button", "icon", "sprite",
        "close", "search", "menu", "nav",
        "header", "footer", "placeholder", "missing", "avatar",
    }
    if stem in _EXACT_STEMS:
        return False

    # ── PRÄFIXE die das ganze Bild als Variante markieren ─────────────
    # Coppermine speichert Vorschauen als "thumb_X.jpg" und mittlere
    # Versionen als "normal_X.jpg" – diese sollen geblockt werden.
    _BLOCKED_PREFIXES = ("thumb_", "normal_", "intermediate_", "small_", "tn_")
    if any(stem.startswith(p) for p in _BLOCKED_PREFIXES):
        return False

    # ── SUBSTRING-Treffer NUR für eindeutige Begriffe die in echten
    # Galerie-Dateinamen nie vorkommen würden ──────────────────────────
    _BLOCKED_SUBSTRINGS = (
        "placeholder", "watermark", "no_pic", "nopic",
    )
    if any(x in stem for x in _BLOCKED_SUBSTRINGS):
        return False

    return True


def is_album_complete(folder: Path) -> bool:
    return (long_path(folder) / _COMPLETE_MARKER).exists()


def mark_album_complete(folder: Path) -> None:
    (long_path(folder) / _COMPLETE_MARKER).touch()


# ==========================
# Network helpers
# ==========================

# HTTP-Statuscodes die einen temporären Server-Fehler signalisieren und einen Retry rechtfertigen
_RETRY_STATUS_CODES: set[int] = {429, 500, 502, 503, 504, 508}

# Verzögerungen pro Statuscode in Sekunden (Basis, wird mit Versuch multipliziert)
_RETRY_DELAYS: dict[int, float] = {
    429: 10.0,   # Too Many Requests – länger warten
    508: 15.0,   # Loop Detected – Server erholt sich langsam
}
_RETRY_DEFAULT_DELAY = 5.0

# Coppermine DB-Überlastungs-Fehlermeldungen die im HTML-Body erscheinen
_DB_ERROR_PHRASES = ("max_user_connections", "Unable to connect to database", "MySQLi said")


async def fetch_text(
    client: httpx.AsyncClient,
    url: str,
    headers: dict,
    sig: WorkerSignals,
    _retries: int = 5,
    rate_limiter: "RateLimiter | None" = None,
) -> str | None:
    """Lädt eine Seite als Text.
    Retried bei temporären Server-Fehlern (508, 429, 5xx) und DB-Überlastung.
    Passt den RateLimiter nach Erfolg/Fehler an.
    """
    for attempt in range(1, _retries + 1):
        if rate_limiter:
            await rate_limiter.wait()
        try:
            r = await client.get(
                url,
                headers=headers,
                timeout=30,
                follow_redirects=True,
            )

            # Cloudflare-Challenge/Block – wird VOR der generischen Status-Code-
            # Behandlung geprüft, denn Cloudflare liefert das oft als HTTP 200
            # (Managed Challenge – sieht wie ein normaler Seitenaufruf aus,
            # enthält aber keinen echten Inhalt) ODER als HTTP 403/503 mit
            # 'cf-mitigated: challenge' (z.B. Turnstile-Schutz auf bestimmten
            # Pfaden wie thumbnails.php). Ohne FlareSolverr kann das hier nicht
            # automatisch gelöst werden – erneutes Anfragen ohne JS-Ausführung
            # bringt nichts, daher sofort abbrechen statt die Retry-Versuche
            # zu verbrauchen (die HybridClient-Instanz hat bereits VOR dieser
            # Stelle einen FlareSolverr-Fallback versucht, falls konfiguriert –
            # kommt die Antwort hier als Challenge an, ist der Fallback
            # fehlgeschlagen oder nicht konfiguriert).
            if _looks_like_cf_challenge(r.status_code, r.text, dict(r.headers)):
                if rate_limiter:
                    await rate_limiter.failure()
                sig.error.emit(
                    f"  🛡 Cloudflare-Challenge/Block erkannt (HTTP {r.status_code}, "
                    f"kein echter Seiteninhalt) → {url.split('?')[0]}\n"
                    f"     Tipp: FlareSolverr muss laufen und erreichbar sein "
                    f"(FlareSolverr-API in den Einstellungen), um diese Seite zu "
                    f"laden. Bei sehr vielen Alben ggf. auch den Rate-Limit-Delay "
                    f"erhöhen, wenn die Seite bereits mit FlareSolverr geladen "
                    f"werden konnte."
                )
                return None

            # Temporäre Server-Fehler → warten und retry
            if r.status_code in _RETRY_STATUS_CODES:
                delay = _RETRY_DELAYS.get(r.status_code, _RETRY_DEFAULT_DELAY) * attempt
                if rate_limiter:
                    await rate_limiter.failure()
                sig.info.emit(
                    f"  HTTP {r.status_code} – warte {delay:.0f}s, "
                    f"Versuch {attempt}/{_retries} … ({url.split('?')[0]})"
                )
                await asyncio.sleep(delay)
                continue

            if not r.is_success:
                if rate_limiter:
                    await rate_limiter.failure()
                sig.error.emit(
                    f"[fetch] HTTP {r.status_code} {r.reason_phrase} → {url}"
                )
                return None

            text = r.text

            # Coppermine DB-Fehler im HTML-Body
            if any(p in text for p in _DB_ERROR_PHRASES):
                wait = 5 * attempt
                if rate_limiter:
                    await rate_limiter.failure()
                sig.info.emit(f"  ⚠ DB-Überlastung – warte {wait}s … (Versuch {attempt}/{_retries})")
                await asyncio.sleep(wait)
                continue

            if rate_limiter:
                await rate_limiter.success()
            return text

        except ssl.SSLError as e:
            sig.error.emit(
                f"[fetch] SSL-Fehler bei {url}: {e} "
                f"→ Tipp: 'SSL verifizieren' deaktivieren"
            )
            return None  # SSL-Fehler werden nicht retried
        except httpx.ConnectError as e:
            if rate_limiter:
                await rate_limiter.failure()
            err_msg = str(e).strip() or "ConnectError (keine Details)"
            if attempt < _retries:
                wait = 5 * attempt
                sig.info.emit(
                    f"  Verbindungsfehler – warte {wait}s "
                    f"(Versuch {attempt}/{_retries}): {err_msg}"
                    + (f"  [Rate-Delay jetzt {rate_limiter.current_delay}s]" if rate_limiter else "")
                )
                await asyncio.sleep(wait)
            else:
                sig.error.emit(f"[fetch] Verbindung fehlgeschlagen {url}: {err_msg}")
                return None
        except httpx.TimeoutException:
            if rate_limiter:
                await rate_limiter.failure()
            sig.info.emit(f"  Timeout (Versuch {attempt}/{_retries}): {url}")
            if attempt < _retries:
                await asyncio.sleep(3 * attempt)
            else:
                sig.error.emit(f"[fetch] Timeout nach {_retries} Versuchen: {url}")
                return None
        except Exception as e:
            err_msg = str(e) or type(e).__name__
            if attempt < _retries:
                await asyncio.sleep(3 * attempt)
            else:
                sig.error.emit(f"[fetch] {url}: {err_msg}")
    return None


async def check_img_candidate(
    src: str,
    client: httpx.AsyncClient,
    headers: dict,
    base_url: str = "",
) -> str | None:
    if not src:
        return None
    full_url = urljoin(base_url, src.strip()) if base_url else src.strip()
    if not looks_like_full_image(full_url):
        return None
    try:
        r = await client.head(full_url, headers=headers, timeout=10, follow_redirects=True)
        if r.status_code == 405:
            r = await client.get(full_url, headers=headers, timeout=10, follow_redirects=True)
        if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
            if int(r.headers.get("content-length", 0) or 0) < 512:
                return None
            return full_url
    except Exception:
        pass
    return None


# ==========================
# Pagination
# ==========================

async def count_pages(
    base_url: str,
    client: httpx.AsyncClient,
    headers: dict,
    sig: WorkerSignals,
    rate_limiter: "RateLimiter | None" = None,
) -> int:
    html = await fetch_text(client, base_url, headers, sig, rate_limiter=rate_limiter)
    if not html:
        return 1
    soup = BeautifulSoup(html, "html.parser")
    pages = []
    for a in soup.find_all("a", href=True):
        m = re.search(r"[?&]page=(\d+)", str(a["href"]))
        if m:
            try:
                pages.append(int(m.group(1)))
            except ValueError:
                pass
    result = max(pages) if pages else 1
    sig.info.emit(f"Seiten: {result}")
    return result


# ==========================
# Display-Page Collector
# ==========================

async def collect_display_pages(
    thumbnail_page_url: str,
    client: httpx.AsyncClient,
    headers: dict,
    seen: set[str],
    sig: WorkerSignals,
    rate_limiter: "RateLimiter | None" = None,
) -> list[str]:
    html = await fetch_text(client, thumbnail_page_url, headers, sig, rate_limiter=rate_limiter)
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    found = []
    for td in soup.find_all("td", class_=lambda c: c and "thumbnails" in c):
        a = td.find("a", href=True)
        if a:
            full = urljoin(thumbnail_page_url, a["href"].strip())
            if full not in seen:
                seen.add(full)
                found.append(full)
    for a in soup.select("a[href*='displayimage.php?pid=']"):
        full = urljoin(thumbnail_page_url, a["href"])
        if full not in seen:
            seen.add(full)
            found.append(full)
    return found


# ==========================
# Gallery Crawler
# ==========================

# Alben mit diesen IDs werden immer übersprungen (Sonder-Alben von Coppermine)
_SKIP_ALBUM_IDS: set[str] = {"lastup", "lastcom", "topn", "toprated", "search", "newest"}

# Kategorie-Namen die Coppermine als Navigations-Links rendert und die
# keine echten Kategorien sind – werden übersprungen (case-insensitive)
_SKIP_CAT_NAMES: set[str] = {
    "album list", "albumlist", "home", "start", "main", "index",
    "all albums", "alle alben", "categories", "kategorien",
    "captures", "screencaptures", "screencaps", "screen captures",
    "screeencaps",
}


async def crawl_full_gallery(
    index_url: str,
    dest_root: Path,
    client: httpx.AsyncClient,
    headers: dict,
    sig: WorkerSignals,
    rate_limiter: "RateLimiter | None" = None,
) -> list[AlbumInfo]:
    """
    Rekursiver Gallery-Crawler:
      - Hauptseite (index.php): NUR index.php?cat= Links verfolgen,
        thumbnails.php?album= auf der Hauptseite werden IGNORIERT.
      - Kategorie-Seiten: rekursiv alle index.php?cat= und thumbnails.php?album=
        verarbeiten, beliebig tief verschachtelt.
      - Für jede Kategorie-Ebene wird ein gleichnamiger Ordner angelegt.
    """
    albums:      list[AlbumInfo] = []
    seen_albums: set[str]        = set()   # bereits verarbeitete album-IDs
    seen_cats:   set[str]        = set()   # bereits besuchte Kategorie-URLs

    def _get_album_id(href: str, base: str) -> str | None:
        """Extrahiert album=X aus einem href, gibt None zurück wenn ungültig."""
        full    = urljoin(base, href)
        params  = parse_qs(urlparse(full).query)
        aid     = params.get("album", [""])[0]
        if not aid or aid in _SKIP_ALBUM_IDS:
            return None
        return aid

    def _album_status(folder: Path) -> str:
        lp = long_path(folder)
        if is_album_complete(folder):
            return "skip"
        if lp.exists() and any(lp.iterdir()):
            return "resume"
        return "new"

    def _extract_cat_links(soup: BeautifulSoup, base_url: str) -> list[tuple[str, str]]:
        """
        Extrahiert Kategorie-Links aus allen bekannten Coppermine-Theme-Strukturen:
          - span.catlink > a       (klassisches Coppermine)
          - div.catlink > a        (modernes Theme, z.B. elizabetholsen.org)
          - div.cat-title > a      (custom Theme, z.B. elizabeth-olsen.com)
          - Fallback: alle a[href*='index.php?cat='] die keiner der obigen Strukturen angehören

        Fallback-Kette für den Namen:
          1. a.get_text()              – direkter Linktext
          2. img[alt] im <a>          – wenn Bild statt Text verwendet wird
          3. Text im Container außerhalb des <a>
          4. title-Attribut des <a>
        """
        results:    list[tuple[str, str]] = []
        seen_hrefs: set[str]              = set()

        def _extract_name(container, a_tag) -> str:
            name = a_tag.get_text(strip=True)
            if not name:
                img = a_tag.find("img")
                name = img.get("alt", "").strip() if img else ""
            if not name:
                name = container.get_text(strip=True).replace(
                    a_tag.get_text(strip=True), ""
                ).strip()
            if not name:
                name = a_tag.get("title", "").strip()
            return safe_dirname(name or "category")

        # Wörter die, wenn sie im Kategorienamen vorkommen, zum Überspringen führen
        _SKIP_CAT_CONTAINS: set[str] = {"captures", "screencap"}

        def _add(full_url: str, name: str) -> None:
            if full_url in seen_hrefs:
                return
            lower = name.lower()
            if lower in _SKIP_CAT_NAMES:
                return
            if any(word in lower for word in _SKIP_CAT_CONTAINS):
                return
            seen_hrefs.add(full_url)
            results.append((full_url, name))

        # ── Bekannte Container-Klassen ──────────────────────────────────
        _CAT_CONTAINER_CLASSES = ("catlink", "cat-title", "cat_title", "category-title")

        for cls in _CAT_CONTAINER_CLASSES:
            for container in soup.find_all(
                lambda tag, c=cls: tag.name in ("span", "div", "td", "li")
                and c in tag.get("class", [])
            ):
                a = container.find("a", href=True)
                if not a:
                    continue
                href = str(a["href"])
                if "index.php" not in href or "cat=" not in href:
                    continue
                full = urljoin(base_url, href)
                _add(full, _extract_name(container, a))

        # ── Fallback: alle index.php?cat= Links die noch nicht erfasst wurden ──
        # Nur wenn die bekannten Strukturen gar nichts geliefert haben
        if not results:
            for a in soup.find_all("a", href=True):
                href = str(a["href"])
                if "index.php" not in href or "cat=" not in href:
                    continue
                full = urljoin(base_url, href)
                if full in seen_hrefs:
                    continue
                name = safe_dirname(
                    a.get_text(strip=True) or a.get("title", "") or "category"
                )
                _add(full, name)

        return results

    async def crawl_category(cat_url: str, path_parts: list[str]) -> None:
        """
        Verarbeitet eine Kategorie-Seite inkl. aller Folgeseiten (Paginierung).
        path_parts = Ordner-Pfad relativ zu dest_root, z.B. ["Musik", "Rock", "2023"]
        """
        if cat_url in seen_cats:
            return
        seen_cats.add(cat_url)

        html = await fetch_text(client, cat_url, headers, sig, rate_limiter=rate_limiter)
        if not html:
            return
        soup = BeautifulSoup(html, "html.parser")

        # Eigene cat= ID ermitteln um Paginierung von echten Subkategorien zu trennen
        own_cat_id = parse_qs(urlparse(cat_url).query).get("cat", [""])[0]

        # ── 1. Links klassifizieren: Paginierung vs. echte Unterkategorien ──
        sub_cats:   list[tuple[str, str]] = []
        next_pages: list[str]             = []

        for full, name in _extract_cat_links(soup, cat_url):
            if full in seen_cats:
                continue
            params   = parse_qs(urlparse(full).query)
            link_cat = params.get("cat", [""])[0]
            has_page = "page" in params

            if link_cat == own_cat_id and has_page:
                next_pages.append(full)
            elif link_cat != own_cat_id:
                sub_cats.append((full, name))

        # Paginierungs-Links die nicht in span.catlink stehen (plain nav-links)
        for a in soup.find_all("a", href=True):
            href = str(a["href"])
            if "index.php" not in href or "cat=" not in href:
                continue
            full = urljoin(cat_url, href)
            if full in seen_cats:
                continue
            params   = parse_qs(urlparse(full).query)
            link_cat = params.get("cat", [""])[0]
            has_page = "page" in params
            if link_cat == own_cat_id and has_page and full not in next_pages:
                next_pages.append(full)

        # ── 2. Alben auf DIESER Seite sammeln ───────────────────────────────
        for td in soup.find_all("td", class_=lambda c: c and "tableh2" in c.split()):
            span = td.find("span", class_=lambda c: c and "alblink" in c.split())
            if not span:
                continue
            a = span.find("a", href=True)
            if not a:
                continue

            href = str(a["href"])
            if "thumbnails.php" not in href or "album=" not in href:
                continue

            album_id = _get_album_id(href, cat_url)
            if not album_id or album_id in seen_albums:
                continue
            seen_albums.add(album_id)

            title_text = a.get_text(strip=True)
            if not title_text:
                sig.info.emit(f"  SKIP (kein Titel): album={album_id}")
                continue

            album_name = safe_dirname(title_text)
            folder     = fit_path(dest_root.joinpath(*path_parts, album_name))
            status     = _album_status(folder)

            info = AlbumInfo(
                name=album_name,
                thumb_url=urljoin(cat_url, f"thumbnails.php?album={album_id}"),
                dest_folder=folder,
                path_parts=list(path_parts),
                status=status,
            )
            albums.append(info)
            sig.gallery_album.emit(info)
            breadcrumb = " / ".join(path_parts + [album_name])
            sig.info.emit(f"[{status.upper()}] {breadcrumb}")

        # ── 3. Folgeseiten der gleichen Kategorie (kein neuer Ordner) ───────
        for page_url in next_pages:
            await crawl_category(page_url, path_parts)  # gleiche path_parts!

        # ── 4. Echte Unterkategorien rekursiv absteigen (neuer Ordner) ──────
        for sub_url, sub_name in sub_cats:
            await crawl_category(sub_url, path_parts + [sub_name])

    # ── Hauptseite: NUR Kategorien verfolgen, keine Alben ───────────────
    html = await fetch_text(client, index_url, headers, sig, rate_limiter=rate_limiter)
    if not html:
        return albums

    soup = BeautifulSoup(html, "html.parser")
    top_cats: list[tuple[str, str]] = []

    for full, name in _extract_cat_links(soup, index_url):
        if full == index_url:
            continue
        if full not in seen_cats:
            top_cats.append((full, name))

    if not top_cats:
        sig.error.emit("Keine Kategorien (index.php?cat=) auf der Hauptseite gefunden!")
        return albums

    sig.info.emit(f"{len(top_cats)} Top-Kategorien gefunden, starte rekursiven Crawl ...")
    for cat_url, cat_name in top_cats:
        await crawl_category(cat_url, [cat_name])

    sig.info.emit(f"Gallery gecrawlt: {len(albums)} Alben in {len(seen_cats)} Kategorien.")
    return albums


# ==========================
# Fullres Extractor
# ==========================

async def _get_img_from_candidate_page(
    page_url: str,
    client: httpx.AsyncClient,
    headers: dict,
) -> str | None:
    try:
        r = await client.get(page_url, headers=headers, timeout=30)
        r.raise_for_status()
    except Exception:
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    for img_tag in soup.find_all("img"):
        src = img_tag.get("src") or img_tag.get("data-src")
        if src:
            full_url = urljoin(page_url, src.strip())
            if looks_like_full_image(full_url):
                return full_url
    img_srcset = soup.find("img", srcset=True)
    if img_srcset:
        for part in img_srcset["srcset"].split(","):
            src = part.strip().split()[0]
            full_url = urljoin(page_url, src)
            if looks_like_full_image(full_url):
                return full_url
    meta_og = soup.find("meta", property="og:image")
    if meta_og and meta_og.get("content"):
        full_url = urljoin(page_url, meta_og["content"].strip())
        if looks_like_full_image(full_url):
            return full_url
    any_img = soup.find("img", src=re.compile(r"/?albums/"))
    if any_img:
        src = any_img.get("src", "").strip()
        full_url = urljoin(page_url, src)
        if looks_like_full_image(full_url):
            return full_url
    return None


async def extract_fullres_from_displaypage(
    display_url: str,
    client: httpx.AsyncClient,
    headers: dict,
    rate_limiter: "RateLimiter | None" = None,
) -> str | object | None:
    """
    Gibt zurück:
      - str             → aufgelöste Full-Res URL
      - _SKIP_PERMANENT → Seite geladen und Platzhalter-Bild EXPLIZIT erkannt
      - None            → Netzwerk-/Serverfehler ODER kein Bild gefunden → Retry
    """
    if rate_limiter:
        await rate_limiter.wait()
    try:
        r = await client.get(display_url, headers=headers, timeout=30)
        r.raise_for_status()
        html = r.text
        if rate_limiter:
            await rate_limiter.success()
    except httpx.ConnectError:
        if rate_limiter:
            await rate_limiter.failure()
        return None
    except httpx.TimeoutException:
        if rate_limiter:
            await rate_limiter.failure()
        return None
    except Exception:
        return None   # Netzfehler → Retry

    soup = BeautifulSoup(html, "html.parser")

    # ── Platzhalter-Erkennung (nur explizite Platzhalter → kein Retry) ──
    # Prüfe NUR die primären Bild-Container, nicht alle Tags.
    # Entscheidend: der src muss EXPLIZIT ein bekanntes Platzhalter-Muster enthalten.
    def _is_placeholder(src: str) -> bool:
        if not src:
            return False
        return any(p in src.lower() for p in _PLACEHOLDER_SRCS)

    for selector_fn in [
        lambda s: s.find("img", id="fullsize_image"),
        lambda s: s.find("img", class_=lambda c: c and "image" in c.split()),
        lambda s: s.find("td", class_="display_media") and
                  s.find("td", class_="display_media").find("img"),
    ]:
        tag = selector_fn(soup)
        if tag and hasattr(tag, "get"):
            src = tag.get("src") or tag.get("data-src") or ""
            if _is_placeholder(src):
                return _SKIP_PERMANENT
            # Wenn das primäre Bild-Tag eine echte albums/-URL hat,
            # direkt zurückgeben ohne weiteren HEAD-Check
            if src and "albums/" in src.lower() and looks_like_full_image(src):
                return urljoin(display_url, src.strip())

    # ── Normale Auflösungs-Kaskade ─────────────────────────────────────
    for a in soup.find_all("a", onclick=True):
        onclick = a.get("onclick", "")
        m = re.search(r"(?:MM_openBrWindow|window\.open)\(\s*['\"]([^'\"]*fullsize=1[^'\"]*)['\"]", onclick)
        if m:
            img = await _get_img_from_candidate_page(urljoin(display_url, m.group(1)), client, headers)
            if img:
                return img
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if any(x in href for x in ("fullsize=1", "view=popup", "size=original")):
            img = await _get_img_from_candidate_page(urljoin(display_url, href), client, headers)
            if img:
                return img
    for selector_fn in [
        lambda s: s.find("img", id="fullsize_image"),
        lambda s: s.find("img", class_=lambda c: c and "image" in c.split()),
    ]:
        tag = selector_fn(soup)
        if tag:
            src = tag.get("src") or tag.get("data-src")
            candidate = await check_img_candidate(src, client, headers, display_url)
            if candidate:
                return candidate
    img_srcset = soup.find("img", srcset=True)
    if img_srcset:
        for part in img_srcset.get("srcset", "").split(","):
            candidate = await check_img_candidate(part.strip().split()[0], client, headers, display_url)
            if candidate:
                return candidate
    meta_og = soup.find("meta", property="og:image")
    if meta_og and meta_og.get("content"):
        candidate = await check_img_candidate(meta_og["content"], client, headers, display_url)
        if candidate:
            return candidate
    # albums/-Bilder: direkt vertrauen wenn looks_like_full_image bestanden,
    # keinen HEAD-Check mehr machen (spart Requests, behebt Fehldiagnosen)
    any_img = soup.find("img", src=re.compile(r"/?albums/"))
    if any_img:
        src = (any_img.get("src") or any_img.get("data-src", "")).strip()
        if src and looks_like_full_image(src):
            return urljoin(display_url, src)
    for tag in soup.find_all("img"):
        src = tag.get("src") or tag.get("data-src")
        if src and "albums/" in src.lower() and looks_like_full_image(src):
            return urljoin(display_url, src.strip())

    # ── Erweiterter Fallback für Themes ohne <img>-Tag ─────────────────
    # Einige Coppermine-Themes binden das Bild nur als <a href="albums/...">
    # ein oder verstecken die URL in data-Attributen oder JavaScript.
    #
    # 1. Suche in <a href>-Anchoren nach Bild-URLs:
    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        if "albums/" in href.lower() and looks_like_full_image(href):
            return urljoin(display_url, href)

    # 2. Suche in beliebigen Attributen aller Tags nach albums/-Pfaden:
    for tag in soup.find_all(True):
        for attr_val in tag.attrs.values():
            if not isinstance(attr_val, str):
                continue
            if "albums/" in attr_val.lower() and looks_like_full_image(attr_val):
                return urljoin(display_url, attr_val.strip())

    # 3. Regex über rohen HTML-Text (fängt JS-eingebettete URLs, z.B.
    #    "fullsize_image_link":"albums/...jpg" oder background-image URLs):
    m = re.search(
        r'(?:["\'(]|^|\s)((?:https?://[^"\'\s<>()]+/)?albums/[^"\'\s<>()]+\.(?:jpe?g|png|webp))',
        html, re.IGNORECASE,
    )
    if m:
        candidate_path = m.group(1)
        if looks_like_full_image(candidate_path):
            return urljoin(display_url, candidate_path)

    # 4. Coppermine-spezifischer Fallback:
    #    Wenn nur "normal_X" / "thumb_X" / "intermediate_X" Varianten auf
    #    der Seite sind, gibt es das Original im selben Verzeichnis ohne
    #    diesen Präfix. URL konstruieren und per HEAD-Anfrage validieren.
    #
    #    Beispiel: 'albums/userpics/10001/normal_kaya-1~1.jpg'
    #         →   'albums/userpics/10001/kaya-1~1.jpg'
    _PREFIX_VARIANTS = ("normal_", "thumb_", "intermediate_", "small_", "tn_")
    derived_candidates: list[str] = []
    seen_derived: set[str] = set()
    for tag in soup.find_all("img"):
        src = (tag.get("src") or tag.get("data-src") or "").strip()
        if not src or "albums/" not in src.lower():
            continue
        if "/" not in src:
            continue
        directory, _, fname = src.rpartition("/")
        fname_lower = fname.lower()
        for prefix in _PREFIX_VARIANTS:
            if fname_lower.startswith(prefix):
                # Präfix entfernen → mutmaßliche Original-Datei
                original_path = directory + "/" + fname[len(prefix):]
                full_url = urljoin(display_url, original_path)
                if full_url not in seen_derived:
                    seen_derived.add(full_url)
                    derived_candidates.append(full_url)
                break

    for candidate_url in derived_candidates:
        verified = await check_img_candidate(candidate_url, client, headers, display_url)
        if verified:
            return verified

    # Kein Bild gefunden → None (Retry), nicht _SKIP_PERMANENT
    # (_SKIP_PERMANENT nur bei explizitem Platzhalter oben)
    return None


# ==========================
# Retry-Konstanten
# ==========================

# Maximale Versuche pro URL/Datei bevor aufgegeben wird
_MAX_ATTEMPTS     = 20
# Basis-Wartezeit: Versuch N → N×15s, maximal 300s
_RETRY_BASE_WAIT  = 15.0
_RETRY_MAX_WAIT   = 300.0
# Programm-Abbruch nach N aufeinanderfolgenden endgültigen Aufgaben (max-wait erreicht)
_MAX_CONSEC_FAILS = 5


def _calc_wait(attempt: int) -> float:
    return min(_RETRY_BASE_WAIT * attempt, _RETRY_MAX_WAIT)


# ==========================
# Queue-Worker: URL-Auflösung
# ==========================

async def _resolve_worker(
    worker_id: int,
    queue: asyncio.Queue,
    filelist: list,
    fileset: set,
    resolved_count: list,      # [int] mutable
    expected: int,
    client: httpx.AsyncClient,
    headers: dict,
    sig: WorkerSignals,
    consec_fails: list[int],
    abort_event: asyncio.Event,
    lock: asyncio.Lock,
    rate_limiter: "RateLimiter | None" = None,
) -> None:
    """
    Auflösungs-Worker mit optionalem RateLimiter.
    """
    while not abort_event.is_set():
        try:
            display_url, attempt = queue.get_nowait()
        except asyncio.QueueEmpty:
            break

        short = display_url.replace("https://", "").replace("http://", "")

        try:
            full = await extract_fullres_from_displaypage(
                display_url, client, headers, rate_limiter=rate_limiter
            )
        except Exception:
            full = None

        # ── Permanenter Skip: Seite geladen, kein Bild vorhanden ──────────
        if full is _SKIP_PERMANENT:
            sig.info.emit(f"  ↷ Kein Bild auf Seite (übersprungen): {short}")
            async with lock:
                resolved_count[0] += 1
                sig.resolve_tick.emit(resolved_count[0], expected)
            queue.task_done()
            continue

        # ── Erfolgreich aufgelöst ──────────────────────────────────────────
        if full:
            consec_fails[0] = 0
            async with lock:
                resolved_count[0] += 1
                if full not in fileset:
                    fileset.add(full)
                    filelist.append(full)
                sig.resolve_tick.emit(resolved_count[0], expected)
            queue.task_done()
            continue

        # Fehlgeschlagen
        wait    = _calc_wait(attempt)
        is_max  = (wait >= _RETRY_MAX_WAIT)
        is_last = (attempt >= _MAX_ATTEMPTS)

        sig.error.emit(
            f"  ⚠ Auflösung fehlgeschlagen – {short}"
            f"  [Versuch {attempt}/{_MAX_ATTEMPTS}, warte {wait:.0f}s]"
        )

        if is_last:
            # URL endgültig aufgeben
            if is_max:
                consec_fails[0] += 1
                if consec_fails[0] >= _MAX_CONSEC_FAILS:
                    abort_event.set()
                    sig.abort.emit(
                        f"🛑 Abbruch: {_MAX_CONSEC_FAILS} URLs hintereinander"
                        f" nach {_MAX_ATTEMPTS} Versuchen nicht auflösbar."
                    )
                    queue.task_done()
                    return
            sig.error.emit(f"  ✗ Aufgegeben: {short}")
            async with lock:
                resolved_count[0] += 1
                sig.resolve_tick.emit(resolved_count[0], expected)
            queue.task_done()
            continue

        # Wartezeit komplett innerhalb dieses Workers → kein anderer Worker
        # läuft währenddessen für dieselbe URL
        await asyncio.sleep(wait)

        # Nächster Versuch zurück in die Queue
        await queue.put((display_url, attempt + 1))
        queue.task_done()


# ==========================
# Queue-Worker: Download
# ==========================

async def _download_worker(
    worker_id: int,
    queue: asyncio.Queue,
    ok_count: list,            # [int] mutable
    client: httpx.AsyncClient,
    headers: dict,
    dest_folder: Path,
    sig: WorkerSignals,
    consec_fails: list[int],
    abort_event: asyncio.Event,
    lock: asyncio.Lock,
    prefix: str,
    use_index: bool,
    dl_delay: float,
    rate_limiter: "RateLimiter | None" = None,
) -> None:
    """
    Download-Worker mit optionalem RateLimiter.
    """
    while not abort_event.is_set():
        try:
            img_url, idx, attempt = queue.get_nowait()
        except asyncio.QueueEmpty:
            break

        url_clean = img_url.strip().replace(" ", "%20")
        original_name = unquote(url_clean.split("/")[-1].split("?")[0])
        ext = Path(original_name).suffix.lower() or ".jpg"
        filename = (
            f"{prefix.rstrip('_')}_{idx:03d}{ext}" if use_index
            else original_name or f"img{ext}"
        )
        target = long_path(fit_path(dest_folder / filename))

        # Skip-Check: Datei vollständig und PIL-verifizierbar vorhanden?
        if file_is_valid(target):
            sig.info.emit(f"SKIP: {filename}")
            consec_fails[0] = 0
            async with lock:
                ok_count[0] += 1
            queue.task_done()
            continue

        last_err = ""
        success  = False

        try:
            http_ok       = False
            http_attempts = 0
            while not http_ok and not abort_event.is_set():
                http_attempts += 1
                if rate_limiter:
                    await rate_limiter.wait()
                try:
                    async with client.stream(
                        "GET", url_clean, headers=headers,
                        timeout=60, follow_redirects=True,
                    ) as r:
                        if r.status_code in _RETRY_STATUS_CODES:
                            code_wait = _RETRY_DELAYS.get(r.status_code, _RETRY_DEFAULT_DELAY)
                            if http_attempts >= 10:
                                last_err = f"HTTP {r.status_code} nach {http_attempts} Versuchen"
                                http_ok = True
                                raise RuntimeError(last_err)
                            if rate_limiter:
                                await rate_limiter.failure()
                            sig.error.emit(
                                f"  ↯ HTTP {r.status_code} [{filename}]  warte {code_wait:.0f}s"
                            )
                            await asyncio.sleep(code_wait)
                            continue

                        r.raise_for_status()
                        total = int(r.headers.get("content-length", 0) or 0)
                        sig.dl_start.emit(filename, total)

                        # Progress-Drossel: nur alle 100ms oder bei jeder
                        # 1%-Stufe ein Signal feuern — verhindert dass die GUI
                        # bei vielen parallelen Downloads von tausenden Updates/s
                        # geflutet wird und blockiert.
                        last_emit_ts  = 0.0
                        last_emit_val = 0
                        threshold_pct = max(total // 100, 65536)  # mind. 64 KB

                        with open(target, "wb") as f:
                            downloaded = 0
                            async for chunk in r.aiter_bytes(65536):
                                f.write(chunk)
                                downloaded += len(chunk)
                                now = time.monotonic()
                                if (downloaded - last_emit_val) >= threshold_pct \
                                        or (now - last_emit_ts) >= 0.1:
                                    sig.dl_progress.emit(filename, downloaded)
                                    last_emit_ts  = now
                                    last_emit_val = downloaded
                        # Final state nach Abschluss
                        sig.dl_progress.emit(filename, downloaded)
                        http_ok = True
                        if rate_limiter:
                            await rate_limiter.success()

                except ssl.SSLError as e:
                    sig.dl_error.emit(filename, f"SSL-Fehler (nicht retried): {e}")
                    queue.task_done()
                    return

                except httpx.ConnectError as e:
                    cause = e.__cause__ or e.__context__
                    if isinstance(cause, ssl.SSLError):
                        sig.dl_error.emit(filename, f"SSL-Fehler (nicht retried): {cause}")
                        queue.task_done()
                        return
                    if rate_limiter:
                        await rate_limiter.failure()
                    raise  # normaler ConnectError → outer except

            if abort_event.is_set():
                queue.task_done()
                return

            if not verify_image(target):
                try:
                    target.unlink(missing_ok=True)
                except OSError:
                    pass
                last_err = "Bildverifizierung fehlgeschlagen"
                raise RuntimeError(last_err)

            sig.dl_done.emit(filename)
            consec_fails[0] = 0
            async with lock:
                ok_count[0] += 1
            if dl_delay > 0:
                await asyncio.sleep(dl_delay)
            success = True

        except Exception as e:
            last_err = str(e).strip() or type(e).__name__
            # Teilweise heruntergeladene Datei entfernen damit kein kaputtes
            # File zurückbleibt das beim nächsten Versuch als SKIP erkannt wird
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass

        if success:
            queue.task_done()
            continue

        # Download fehlgeschlagen
        wait    = _calc_wait(attempt)
        is_max  = (wait >= _RETRY_MAX_WAIT)
        is_last = (attempt >= _MAX_ATTEMPTS)

        sig.error.emit(
            f"  ↯ Download fehlgeschlagen [{filename}]  {last_err}"
            f"  [Versuch {attempt}/{_MAX_ATTEMPTS}, warte {wait:.0f}s]"
        )
        sig.dl_error.emit(filename, f"Versuch {attempt}/{_MAX_ATTEMPTS}: {last_err}")

        if is_last:
            if is_max:
                consec_fails[0] += 1
                if consec_fails[0] >= _MAX_CONSEC_FAILS:
                    abort_event.set()
                    sig.abort.emit(
                        f"🛑 Abbruch: {_MAX_CONSEC_FAILS} Dateien hintereinander"
                        f" nach {_MAX_ATTEMPTS} Versuchen nicht herunterladbar."
                    )
                    queue.task_done()
                    return
            sig.dl_error.emit(filename, f"✗ Aufgegeben nach {_MAX_ATTEMPTS} Versuchen")
            queue.task_done()
            continue

        # Wartezeit innerhalb des Workers, danach nächster Versuch in Queue
        await asyncio.sleep(wait)
        await queue.put((img_url, idx, attempt + 1))
        queue.task_done()


# ==========================
# Album-Download
# ==========================

async def download_album(
    thumb_url: str,
    dest_folder: Path,
    parallel: int,
    client: httpx.AsyncClient,
    dl_client: httpx.AsyncClient,
    headers: dict,
    sig: WorkerSignals,
    abort_event: asyncio.Event,
    consec_fails: list[int],
    prefix: str = "",
    use_index: bool = False,
    resolve_concurrency: int = 2,
    dl_delay: float = 1.0,
    rate_limiter: "RateLimiter | None" = None,
) -> bool:
    """
    Löst alle Bilder eines Albums auf und lädt sie herunter.
    """
    # ── Display-Pages sammeln ──────────────────────────────────────────
    pages = await count_pages(thumb_url, client, headers, sig, rate_limiter=rate_limiter)
    seen: set[str] = set()
    displaypage_list: list[str] = []
    # Thumbnail-Seiten sequentiell laden wenn rate_limiter aktiv (aggressiver Server)
    if rate_limiter:
        for p in range(1, pages + 1):
            batch = await collect_display_pages(
                build_page_url(thumb_url, p), client, headers, seen, sig,
                rate_limiter=rate_limiter,
            )
            displaypage_list.extend(batch)
    else:
        for batch in await asyncio.gather(*[
            collect_display_pages(build_page_url(thumb_url, p), client, headers, seen, sig)
            for p in range(1, pages + 1)
        ]):
            displaypage_list.extend(batch)

    if not displaypage_list:
        sig.info.emit(f"  SKIP (leer): {dest_folder.name}")
        return False

    expected = len(displaypage_list)
    sig.info.emit(f"  {expected} Bilder in Album")
    sig.resolve_init.emit(expected)

    # ── URL-Auflösung via Queue-Worker ────────────────────────────────
    resolve_queue: asyncio.Queue = asyncio.Queue()
    for url in displaypage_list:
        await resolve_queue.put((url, 1))

    filelist:       list[str] = []
    fileset:        set[str]  = set()
    resolved_count: list[int] = [0]
    lock = asyncio.Lock()

    workers = [
        asyncio.create_task(_resolve_worker(
            i, resolve_queue, filelist, fileset,
            resolved_count, expected,
            client, headers, sig, consec_fails, abort_event, lock,
            rate_limiter=rate_limiter,
        ))
        for i in range(resolve_concurrency)
    ]

    await asyncio.gather(*workers)

    if abort_event.is_set():
        return False

    found     = len(filelist)
    not_found = expected - found
    sig.info.emit(
        f"  {found} Full-Res URLs aufgelöst"
        + (f"  ({not_found} nicht gefunden)" if not_found else "")
    )

    if not filelist:
        sig.info.emit(f"  SKIP (keine URLs): {dest_folder.name}")
        return False

    # ── Download via Queue-Worker ─────────────────────────────────────
    long_path(dest_folder).mkdir(parents=True, exist_ok=True)

    dl_queue: asyncio.Queue = asyncio.Queue()
    for i, url in enumerate(filelist, 1):
        await dl_queue.put((url, i, 1))  # (img_url, idx, attempt=1)

    ok_count: list[int] = [0]
    dl_lock = asyncio.Lock()

    dl_workers = [
        asyncio.create_task(_download_worker(
            i, dl_queue, ok_count,
            dl_client, headers, dest_folder,
            sig, consec_fails, abort_event, dl_lock,
            prefix, use_index, dl_delay,
            rate_limiter=rate_limiter,
        ))
        for i in range(parallel)
    ]

    await asyncio.gather(*dl_workers)

    if abort_event.is_set():
        return False

    complete = (found > 0 and ok_count[0] == found)
    if not complete:
        if found == 0:
            sig.info.emit(f"  ⚠ Keine Dateien aufgelöst – kein .complete gesetzt")
        else:
            sig.info.emit(
                f"  ⚠ Unvollständig: {ok_count[0]}/{found} Dateien OK"
                f" – kein .complete gesetzt"
            )
    return complete


# ==========================
# Async Main
# ==========================

async def main(
    base_url: str,
    dest: Path,
    prefix: str,
    parallel: int,
    ssl: bool,
    dry_run: bool,
    sig: WorkerSignals,
    resolve_concurrency: int = 2,
    dl_delay: float = 1.0,
    rate_limit_delay: float = 0.0,
    proxy_url: str = "",
    flaresolverr_url: str = "",
    fs_hybrid: bool = False,
    abort_event: asyncio.Event | None = None,
) -> None:
    headers         = make_headers(base_url)
    parsed          = urlparse(base_url)
    is_full_gallery = "index.php" in parsed.path
    if abort_event is None:
        abort_event = asyncio.Event()
    consec_fails    = [0]

    rl = RateLimiter(initial=rate_limit_delay) if rate_limit_delay > 0 else None
    if rl:
        sig.info.emit(f"⏱ Rate-Limiter aktiv: Start-Delay {rl.current_delay}s (adaptiv)")

    proxy = proxy_url.strip() or None
    if proxy:
        sig.info.emit(f"🔀 Proxy aktiv: {proxy}")

    fs_url = flaresolverr_url.strip() or None
    fs_client: FlareSolverrClient | None = None

    try:
        import h2  # noqa: F401
        http2_supported = True
    except ImportError:
        http2_supported = False
        sig.info.emit(
            "ℹ Hinweis: 'h2' nicht installiert – HTTP/1.1 wird verwendet. "
            "Für browser-ähnliches HTTP/2: pip install httpx[http2]"
        )

    if not _BROTLI_AVAILABLE:
        sig.info.emit(
            "ℹ Hinweis: 'brotli' nicht installiert – Seiten werden ohne "
            "Brotli-Kompression angefragt (etwas mehr Datenvolumen, aber "
            "sichere Dekodierung). Für Brotli: pip install brotli"
        )

    profile = _get_profile()
    sig.info.emit(f"🌐 Browser-Profil: {profile['name']}")

    base_kwargs = dict(verify=ssl, http2=http2_supported, follow_redirects=True)
    if proxy:
        base_kwargs["proxy"] = proxy

    # Gemeinsamer Cookie-Jar zwischen Crawl- und Download-Client damit
    # Cloudflare-Cookies die der HybridClient holt automatisch auch für
    # Bild-Downloads gelten.
    shared_cookies = httpx.Cookies()

    client_kwargs    = {**base_kwargs, "timeout": 30, "cookies": shared_cookies}
    dl_client_kwargs = {**base_kwargs, "timeout": 60, "cookies": shared_cookies}

    # FlareSolverr initialisieren falls konfiguriert
    if fs_url:
        sig.info.emit(f"🛡 FlareSolverr API: {fs_url}")
        if fs_hybrid:
            sig.info.emit(
                "   Hybrid-Modus: erste Anfrage direkt, FlareSolverr nur "
                "bei Cloudflare-Block"
            )
        else:
            sig.info.emit(
                "   Vollmodus: alle HTML-Anfragen über FlareSolverr (langsam aber zuverlässig)"
            )
        fs_client = FlareSolverrClient(
            endpoint=fs_url,
            target_domain=urlparse(base_url).netloc,
        )

    async with httpx.AsyncClient(**client_kwargs) as direct_client:
        async with httpx.AsyncClient(**dl_client_kwargs) as dl_client:

            if fs_client:
                await fs_client.__aenter__()
                # HybridClient mit Modus-Konfiguration:
                # force_via_fs=True (Default) → alle Anfragen über FlareSolverr
                # force_via_fs=False           → erst direkt, FS nur bei Block
                client = HybridClient(
                    direct_client, fs_client, sig, headers,
                    force_via_fs=not fs_hybrid,
                )
            else:
                client = direct_client

            try:
                if is_full_gallery:
                    sig.info.emit("Gallery-Modus erkannt (index.php)")
                    albums = await crawl_full_gallery(
                        base_url, dest, client, headers, sig, rate_limiter=rl
                    )

                    if dry_run:
                        sig.info.emit("Dry-Run – kein Download.")
                        return

                    to_process = [a for a in albums if a.status != "skip"]
                    sig.info.emit(
                        f"{len(albums)} Alben: {len(to_process)} neu/resume,"
                        f" {len(albums) - len(to_process)} übersprungen"
                    )

                    for album in to_process:
                        if abort_event.is_set():
                            break

                        sig.album_active.emit(str(album.dest_folder))
                        breadcrumb = " / ".join(album.path_parts + [album.name])
                        sig.info.emit(f"▶ {breadcrumb} [{album.status}]")
                        if rl:
                            sig.info.emit(f"  ⏱ Rate-Delay aktuell: {rl.current_delay}s")

                        ok = await download_album(
                            thumb_url=album.thumb_url,
                            dest_folder=album.dest_folder,
                            parallel=parallel,
                            client=client,
                            dl_client=dl_client,
                            headers=headers,
                            sig=sig,
                            abort_event=abort_event,
                            consec_fails=consec_fails,
                            use_index=False,
                            resolve_concurrency=resolve_concurrency,
                            dl_delay=dl_delay,
                            rate_limiter=rl,
                        )

                        if abort_event.is_set():
                            break

                        if ok:
                            mark_album_complete(album.dest_folder)
                            sig.album_done.emit(str(album.dest_folder))
                            sig.info.emit(f"✓ {album.name}")
                        else:
                            sig.error.emit(f"⚠ Unvollständig: {album.name}")

                else:
                    sig.info.emit("Album-Modus (thumbnails.php)")
                    dest.mkdir(parents=True, exist_ok=True)
                    if dry_run:
                        sig.info.emit("Dry-Run – kein Download.")
                        return
                    await download_album(
                        thumb_url=base_url,
                        dest_folder=dest,
                        parallel=parallel,
                        client=client,
                        dl_client=dl_client,
                        headers=headers,
                        sig=sig,
                        abort_event=abort_event,
                        consec_fails=consec_fails,
                        prefix=prefix,
                        use_index=True,
                        resolve_concurrency=resolve_concurrency,
                        dl_delay=dl_delay,
                        rate_limiter=rl,
                    )
            finally:
                if fs_client:
                    try:
                        await fs_client.__aexit__(None, None, None)
                    except Exception:
                        pass

    if not abort_event.is_set():
        sig.info.emit("✅ Fertig.")


def _apply_fs_session(*args, **kwargs) -> None:
    """Veraltet seit v0.44 – HybridClient verwaltet Cookies automatisch
    über den gemeinsamen Cookie-Jar. Hier nur für Kompatibilität."""
    pass


# ==========================
# QThread Worker
# ==========================

class DownloadThread(QThread):
    def __init__(self, base_url, dest, prefix, parallel, ssl, dry_run, sig,
                 resolve_concurrency: int = 2, dl_delay: float = 1.0,
                 rate_limit_delay: float = 0.0, proxy_url: str = "",
                 flaresolverr_url: str = "", fs_hybrid: bool = False):
        super().__init__()
        self.base_url            = base_url
        self.dest                = dest
        self.prefix              = prefix
        self.parallel            = parallel
        self.ssl                 = ssl
        self.dry_run             = dry_run
        self.sig                 = sig
        self.resolve_concurrency = resolve_concurrency
        self.dl_delay            = dl_delay
        self.rate_limit_delay    = rate_limit_delay
        self.proxy_url           = proxy_url
        self.flaresolverr_url    = flaresolverr_url
        self.fs_hybrid           = fs_hybrid
        # Wird beim Start gesetzt – ermöglicht stop() aus dem GUI-Thread
        self._abort_event: asyncio.Event | None = None
        self._loop:        asyncio.AbstractEventLoop | None = None

    def stop(self) -> None:
        """Setzt abort_event und wartet sauber auf Thread-Ende."""
        if self._abort_event and self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._abort_event.set)
        # Blockiert den GUI-Thread maximal 10s, dann hart beenden
        if not self.wait(10_000):
            self.terminate()
            self.wait()

    def run(self) -> None:
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._abort_event = asyncio.Event()
            self._loop.run_until_complete(main(
                base_url=self.base_url,
                dest=self.dest,
                prefix=self.prefix,
                parallel=self.parallel,
                ssl=self.ssl,
                dry_run=self.dry_run,
                sig=self.sig,
                resolve_concurrency=self.resolve_concurrency,
                dl_delay=self.dl_delay,
                rate_limit_delay=self.rate_limit_delay,
                proxy_url=self.proxy_url,
                flaresolverr_url=self.flaresolverr_url,
                fs_hybrid=self.fs_hybrid,
                abort_event=self._abort_event,
            ))
        except Exception as e:
            self.sig.error.emit(f"Kritischer Fehler: {e}")
        finally:
            try:
                self._loop.close()
            except Exception:
                pass


# ==========================
# GUI Widgets
# ==========================

# ==========================
# Datei-Download-Zeile
# ==========================

class FileProgressRow(QWidget):
    def __init__(self, filename: str, total: int, parent=None) -> None:
        super().__init__(parent)
        self.total = total
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(8)
        self.name_lbl = QLabel(filename)
        self.name_lbl.setFixedWidth(260)
        self.name_lbl.setStyleSheet("color:#c0c0c0; font-size:11px;")
        self.name_lbl.setToolTip(filename)
        self.bar = QProgressBar()
        self.bar.setRange(0, total if total > 0 else 0)
        self.bar.setValue(0)
        self.bar.setFixedHeight(14)
        self.bar.setTextVisible(total > 0)
        self.bar.setFormat("%p%")
        self.bar.setStyleSheet("""
            QProgressBar { background:#2a2a2a; border:1px solid #444; border-radius:3px; color:#aaa; }
            QProgressBar::chunk { background:#3a7adf; border-radius:2px; }
        """)
        self.size_lbl = QLabel("0 KB")
        self.size_lbl.setFixedWidth(70)
        self.size_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.size_lbl.setStyleSheet("color:#888; font-size:10px;")
        layout.addWidget(self.name_lbl)
        layout.addWidget(self.bar)
        layout.addWidget(self.size_lbl)

    def update_progress(self, downloaded: int) -> None:
        if self.total > 0:
            self.bar.setValue(downloaded)
        # else: bei unbekannter Größe (total=0) nur Bytes anzeigen, keine Bar-Updates
        kb = downloaded / 1024
        self.size_lbl.setText(f"{kb:.0f} KB" if kb < 1024 else f"{kb/1024:.1f} MB")

    def _set_chunk_color(self, color: str) -> None:
        self.bar.setRange(0, 100)
        self.bar.setValue(100)
        self.bar.setStyleSheet(f"""
            QProgressBar {{ background:#2a2a2a; border:1px solid #444; border-radius:3px; color:#aaa; }}
            QProgressBar::chunk {{ background:{color}; border-radius:2px; }}
        """)

    def mark_done(self)  -> None: self._set_chunk_color("#3abf6a")
    def mark_error(self) -> None: self._set_chunk_color("#bf3a3a")


def _make_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet("color:#aaaaaa; font-size:11px;")
    return lbl


def _sep() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.Shape.HLine)
    f.setStyleSheet("color:#333;")
    return f


# ==========================
# Kategorie-Baum Widget
# ==========================

class GalleryTreeWidget(QTreeWidget):
    """Zeigt Kategorien / Unterkategorien / Alben als Baum – beliebig tief."""

    _STATUS_COLORS = {
        "new":    "#5a8aff",
        "resume": "#f0c040",
        "skip":   "#555555",
        "active": "#ffffff",
        "done":   "#3abf6a",
    }

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setAnimated(True)
        self.setStyleSheet("""
            QTreeWidget {
                background:#141414; border:1px solid #333;
                font-family:Segoe UI; font-size:11px; color:#c0c0c0;
            }
            QTreeWidget::item:selected { background:#2a3a5a; }
            QTreeWidget::item { padding:2px 0; }
        """)
        # Knoten-Cache: tuple(path_parts) → QTreeWidgetItem (für Kategorie-Knoten)
        self._node_cache:   dict[tuple, QTreeWidgetItem] = {}
        # album dest_folder str → QTreeWidgetItem
        self._album_items:  dict[str, QTreeWidgetItem]   = {}
        # aktuell aktives Item (für Hintergrund-Reset)
        self._active_item:  QTreeWidgetItem | None       = None

    def _get_or_create_node(self, path_parts: list[str]) -> QTreeWidgetItem:
        """
        Gibt den QTreeWidgetItem für einen Kategorie-Pfad zurück,
        legt ihn und alle Eltern-Knoten bei Bedarf an.
        """
        key = tuple(path_parts)
        if key in self._node_cache:
            return self._node_cache[key]

        if len(path_parts) == 1:
            # Top-Level-Kategorie → Kind des Tree-Widgets
            item = QTreeWidgetItem(self, [f"📁 {path_parts[0]}"])
            item.setForeground(0, QColor("#8ab4f8"))
            font = item.font(0)
            font.setBold(True)
            item.setFont(0, font)
        else:
            # Unterkategorie → Kind des Eltern-Knotens
            parent_item = self._get_or_create_node(path_parts[:-1])
            item = QTreeWidgetItem(parent_item, [f"📂 {path_parts[-1]}"])
            item.setForeground(0, QColor("#a0c8f0"))

        self.expandItem(item)
        self._node_cache[key] = item
        return item

    def add_album(self, info: "AlbumInfo") -> None:
        # Eltern-Knoten anhand des vollen Kategorie-Pfads holen/erstellen
        if info.path_parts:
            parent = self._get_or_create_node(info.path_parts)
        else:
            parent = self.invisibleRootItem()

        color = self._STATUS_COLORS.get(info.status, "#c0c0c0")
        icon  = {"new": "🆕", "resume": "▶", "skip": "⏭"}.get(info.status, "•")
        album_item = QTreeWidgetItem(parent, [f"{icon} {info.name}"])
        album_item.setForeground(0, QColor(color))
        album_item.setToolTip(0, str(info.dest_folder))
        self._album_items[str(info.dest_folder)] = album_item

    def set_active(self, folder_str: str) -> None:
        # Vorherigen aktiven Eintrag: Hintergrund zurücksetzen, Text-Prefix ⏳ entfernen
        if self._active_item is not None:
            prev = self._active_item
            raw = prev.text(0).lstrip("⏳ ")
            # Status-Farbe wiederherstellen anhand des verbleibenden Prefixes
            if raw.startswith("✓"):
                prev.setForeground(0, QColor(self._STATUS_COLORS["done"]))
            elif raw.startswith("🆕"):
                prev.setForeground(0, QColor(self._STATUS_COLORS["new"]))
            elif raw.startswith("▶"):
                prev.setForeground(0, QColor(self._STATUS_COLORS["resume"]))
            elif raw.startswith("⏭"):
                prev.setForeground(0, QColor(self._STATUS_COLORS["skip"]))
            prev.setBackground(0, QColor(0, 0, 0, 0))
            self._active_item = None

        item = self._album_items.get(folder_str)
        if item:
            item.setText(0, f"⏳ {item.text(0).lstrip('🆕▶⏭⏳✓ ')}")
            item.setForeground(0, QColor(self._STATUS_COLORS["active"]))
            item.setBackground(0, QColor("#1a3a5a"))
            self._active_item = item
            self.scrollToItem(item)

    def set_done(self, folder_str: str) -> None:
        item = self._album_items.get(folder_str)
        if item:
            item.setText(0, f"✓ {item.text(0).lstrip('🆕▶⏭⏳✓ ')}")
            item.setForeground(0, QColor(self._STATUS_COLORS["done"]))
            item.setBackground(0, QColor(0, 0, 0, 0))
            if self._active_item is item:
                self._active_item = None

    def clear_tree(self) -> None:
        self.clear()
        self._node_cache.clear()
        self._album_items.clear()
        self._active_item = None


# ==========================
# Main Window
# ==========================

class MainWindow(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(NAMEandVERSION)
        self.resize(900, 820)
        self.setStyleSheet("""
            QWidget { background:#1e1e1e; color:#e0e0e0; font-family:Segoe UI; font-size:12px; }
            QLineEdit, QSpinBox {
                background:#2a2a2a; border:1px solid #444; border-radius:4px;
                padding:4px 6px; color:#e0e0e0;
            }
            QLineEdit:focus, QSpinBox:focus { border-color:#5a8aff; }
            QPushButton {
                background:#3a3a3a; border:1px solid #555; border-radius:4px;
                padding:6px 14px; color:#e0e0e0;
            }
            QPushButton:hover    { background:#4a4a4a; }
            QPushButton:disabled { background:#252525; color:#555; }
            QPushButton#start_btn {
                background:#2a5aaf; border-color:#3a6abf; font-weight:bold; font-size:13px;
            }
            QPushButton#start_btn:hover    { background:#3a6abf; }
            QPushButton#start_btn:disabled { background:#1a3060; color:#446; }
            QPushButton#stop_btn {
                background:#7a2a2a; border-color:#9a3a3a; font-weight:bold; font-size:13px;
            }
            QPushButton#stop_btn:hover    { background:#9a3a3a; }
            QPushButton#stop_btn:disabled { background:#2a1a1a; color:#554; }
            QCheckBox { spacing:6px; }
            QCheckBox::indicator {
                width:15px; height:15px; border:1px solid #555;
                border-radius:3px; background:#2a2a2a;
            }
            QCheckBox::indicator:checked { background:#5a8aff; border-color:#5a8aff; }
            QTextEdit {
                background:#111; border:1px solid #333; border-radius:4px;
                font-family:Consolas,monospace; font-size:11px; color:#e0e0e0;
            }
            QScrollArea { border:none; }
            QSplitter::handle { background:#333; }
            QSpinBox::up-button, QSpinBox::down-button { width:18px; }
        """)

        self._worker: DownloadThread | None = None
        self._sig = WorkerSignals()
        self._dl_rows: dict[str, FileProgressRow] = {}

        self._build_ui()
        self._connect_signals()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        icon_path = (
            Path(sys._MEIPASS) / "assets/fotostapel.ico"  # type: ignore[attr-defined]
            if getattr(sys, "frozen", False)
            else Path("assets/fotostapel.ico")
        )
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(splitter)

        # ===================== LINKE SPALTE mit Tabs =====================
        left_widget = QWidget()
        left_outer = QVBoxLayout(left_widget)
        left_outer.setContentsMargins(0, 0, 0, 0)
        left_outer.setSpacing(0)

        self._tabs = QTabWidget()
        self._tabs.setStyleSheet("""
            QTabWidget::pane {
                border: none;
            }
            QTabBar::tab {
                background: #2a2a2a; color: #888; border: 1px solid #333;
                padding: 6px 18px; border-bottom: none; border-radius: 4px 4px 0 0;
                margin-right: 2px;
            }
            QTabBar::tab:selected { background: #1e1e1e; color: #e0e0e0; border-color: #555; }
            QTabBar::tab:hover    { background: #333; color: #ccc; }
        """)
        left_outer.addWidget(self._tabs)

        # ── Tab 1: Download ──────────────────────────────────────────────
        tab1 = QWidget()
        left = QVBoxLayout(tab1)
        left.setSpacing(8)
        left.setContentsMargins(16, 16, 8, 16)

        left.addWidget(_make_label("Gallery URL  (thumbnails.php?album=X  oder  index.php)"))
        self.url_input = QLineEdit("https://example.com/gallery/index.php")
        left.addWidget(self.url_input)

        left.addWidget(_make_label("Zielordner"))
        dest_row = QHBoxLayout()
        self.dest_input = QLineEdit(r"C:\Downloads\Gallery")
        self.dest_btn = QPushButton("📂")
        self.dest_btn.setFixedWidth(36)
        self.dest_btn.clicked.connect(self._pick_folder)
        dest_row.addWidget(self.dest_input)
        dest_row.addWidget(self.dest_btn)
        left.addLayout(dest_row)

        pp_row = QHBoxLayout()
        pp_row.setSpacing(12)

        prefix_col = QVBoxLayout()
        prefix_col.addWidget(_make_label("Datei-Prefix (nur Album-Modus)"))
        self.prefix_input = QLineEdit("img_")
        prefix_col.addWidget(self.prefix_input)

        parallel_col = QVBoxLayout()
        parallel_col.addWidget(_make_label("Parallele Downloads"))
        self.parallel_spin = QSpinBox()
        self.parallel_spin.setRange(1, 20)
        self.parallel_spin.setValue(3)
        self.parallel_spin.setToolTip("Bei schwachen Servern auf 1-2 reduzieren")
        parallel_col.addWidget(self.parallel_spin)

        resolve_col = QVBoxLayout()
        resolve_col.addWidget(_make_label("Parallele URL-Auflösung"))
        self.resolve_spin = QSpinBox()
        self.resolve_spin.setRange(1, 10)
        self.resolve_spin.setValue(1)
        self.resolve_spin.setToolTip("Bei DB-Fehlern auf 1 reduzieren")
        resolve_col.addWidget(self.resolve_spin)

        delay_col = QVBoxLayout()
        delay_col.addWidget(_make_label("Download-Delay (s)"))
        self.delay_spin = QSpinBox()
        self.delay_spin.setRange(0, 10)
        self.delay_spin.setValue(1)
        self.delay_spin.setToolTip("Pause zwischen Downloads – erhöhen bei ConnectErrors")
        delay_col.addWidget(self.delay_spin)

        rl_col = QVBoxLayout()
        rl_col.addWidget(_make_label("Rate-Limit Start (s)"))
        self.rl_spin = QDoubleSpinBox()
        self.rl_spin.setRange(0.0, 10.0)
        self.rl_spin.setSingleStep(0.5)
        self.rl_spin.setValue(0.0)
        self.rl_spin.setDecimals(1)
        self.rl_spin.setToolTip(
            "0 = deaktiviert\n"
            "> 0 = adaptiver Rate-Limiter (AIMD)\n"
            "Start-Delay in Sekunden zwischen Requests.\n"
            "Erhöht sich automatisch bei Verbindungsfehlern,\n"
            "verringert sich bei Erfolg."
        )
        rl_col.addWidget(self.rl_spin)

        pp_row.addLayout(prefix_col)
        pp_row.addLayout(parallel_col)
        pp_row.addLayout(resolve_col)
        pp_row.addLayout(delay_col)
        pp_row.addLayout(rl_col)
        left.addLayout(pp_row)

        left.addWidget(_sep())

        check_row = QHBoxLayout()
        check_row.setSpacing(24)
        self.ssl_check    = QCheckBox("SSL verifizieren")
        self.ssl_check.setChecked(True)
        self.dryrun_check = QCheckBox("Dry-Run")
        self.dryrun_check.setToolTip("Nur crawlen, nichts herunterladen")
        check_row.addWidget(self.ssl_check)
        check_row.addWidget(self.dryrun_check)
        check_row.addStretch()
        left.addLayout(check_row)

        left.addWidget(_sep())

        btn_row = QHBoxLayout()
        self.start_btn = QPushButton("▶  Start")
        self.start_btn.setObjectName("start_btn")
        self.start_btn.setFixedHeight(40)
        self.stop_btn = QPushButton("⏹  Stopp")
        self.stop_btn.setObjectName("stop_btn")
        self.stop_btn.setFixedHeight(40)
        self.stop_btn.setEnabled(False)
        self.info_btn = QPushButton("ℹ")
        self.info_btn.setFixedSize(40, 40)
        self.info_btn.setFont(QFont("Segoe UI", 14))
        self.info_btn.setToolTip("Info / About")
        self.info_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.info_btn.clicked.connect(self._show_splash)
        self.start_btn.clicked.connect(self._start)
        self.stop_btn.clicked.connect(self._stop)
        btn_row.addWidget(self.start_btn)
        btn_row.addWidget(self.stop_btn)
        btn_row.addWidget(self.info_btn)
        left.addLayout(btn_row)

        # Resolve-Bar
        resolve_header = QHBoxLayout()
        resolve_header.addWidget(_make_label("URL-Auflösung"))
        self.resolve_count_lbl = QLabel("")
        self.resolve_count_lbl.setStyleSheet("color:#888; font-size:11px;")
        resolve_header.addStretch()
        resolve_header.addWidget(self.resolve_count_lbl)
        left.addLayout(resolve_header)

        self.resolve_bar = QProgressBar()
        self.resolve_bar.setRange(0, 1)
        self.resolve_bar.setValue(0)
        self.resolve_bar.setFixedHeight(30)
        self.resolve_bar.setTextVisible(True)
        self.resolve_bar.setFormat("%v / %m  (%p%)")
        self.resolve_bar.setStyleSheet("""
            QProgressBar {
                background:#2a2a2a; border:1px solid #444; border-radius:5px;
                color:white; font-size:13px; font-weight:bold; text-align:center;
            }
            QProgressBar::chunk { background:#3a9a5c; border-radius:4px; }
        """)
        left.addWidget(self.resolve_bar)

        # Download-Rows
        left.addWidget(_make_label("Downloads"))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFixedHeight(180)
        self._dl_container = QWidget()
        self._dl_layout = QVBoxLayout(self._dl_container)
        self._dl_layout.setSpacing(2)
        self._dl_layout.setContentsMargins(4, 4, 4, 4)
        self._dl_layout.addStretch()
        scroll.setWidget(self._dl_container)
        left.addWidget(scroll)
        self._dl_scroll = scroll

        # Log
        left.addWidget(_make_label("Log"))
        self.output = QTextEdit()
        self.output.setReadOnly(True)
        self.output.setMinimumHeight(120)
        left.addWidget(self.output)

        # ── Tab 2: Proxy ─────────────────────────────────────────────────
        tab2 = QWidget()
        prx = QVBoxLayout(tab2)
        prx.setSpacing(12)
        prx.setContentsMargins(16, 20, 16, 16)

        prx.addWidget(_make_label("HTTP-Proxy  (leer = kein Proxy)"))
        self.proxy_input = QLineEdit()
        self.proxy_input.setPlaceholderText("http://benutzer:passwort@host:port  oder  http://host:port")
        self.proxy_input.setToolTip(
            "Alle ausgehenden Verbindungen werden über diesen Proxy geleitet.\n"
            "Unterstützte Formate:\n"
            "  http://host:port\n"
            "  http://user:pass@host:port\n"
            "  socks5://host:port\n"
            "Leer lassen für direkte Verbindung."
        )
        prx.addWidget(self.proxy_input)

        # Schnellauswahl-Buttons für gängige Locals
        quick_row = QHBoxLayout()
        quick_row.setSpacing(8)
        quick_lbl = QLabel("Schnellauswahl:")
        quick_lbl.setStyleSheet("color:#888; font-size:11px;")
        quick_row.addWidget(quick_lbl)
        for label, value in [
            ("Tor", "socks5://127.0.0.1:9050"),
            ("Burp / mitmproxy", "http://127.0.0.1:8080"),
            ("Squid", "http://127.0.0.1:3128"),
        ]:
            btn = QPushButton(label)
            btn.setFixedHeight(26)
            btn.setStyleSheet("font-size:11px; padding:2px 8px;")
            btn.clicked.connect(lambda checked, v=value: self.proxy_input.setText(v))
            quick_row.addWidget(btn)
        quick_row.addStretch()
        prx.addLayout(quick_row)

        prx.addWidget(_sep())

        # ── FlareSolverr (Cloudflare-Umgehung) ───────────────────────────
        prx.addWidget(_make_label("FlareSolverr API  (leer = nicht verwenden)"))
        self.flaresolverr_input = QLineEdit()
        self.flaresolverr_input.setPlaceholderText("http://localhost:8191/v1")
        self.flaresolverr_input.setToolTip(
            "FlareSolverr ist KEIN Proxy sondern eine API die Cloudflare-\n"
            "Challenges durch automatisierte Browser-Sitzungen löst.\n\n"
            "Installation:\n"
            "  docker run -d --name flaresolverr -p 8191:8191 ghcr.io/flaresolverr/flaresolverr:latest\n\n"
            "Standard-Endpoint:\n"
            "  http://localhost:8191/v1\n\n"
            "WICHTIG: Sehr langsam (~10-30s pro Seite). Nur verwenden\n"
            "wenn normaler HTTP-Zugriff durch Cloudflare blockiert wird.\n"
            "Bild-Downloads laufen weiterhin direkt (mit FlareSolverr-Cookies)."
        )
        prx.addWidget(self.flaresolverr_input)

        fs_quick_row = QHBoxLayout()
        fs_quick_row.setSpacing(8)
        fs_quick_lbl = QLabel("Schnellauswahl:")
        fs_quick_lbl.setStyleSheet("color:#888; font-size:11px;")
        fs_quick_row.addWidget(fs_quick_lbl)
        for label, value in [
            ("FlareSolverr (Docker)", "http://localhost:8191/v1"),
            ("FlareSolverr (custom)", "http://localhost:1234"),
        ]:
            btn = QPushButton(label)
            btn.setFixedHeight(26)
            btn.setStyleSheet("font-size:11px; padding:2px 8px;")
            btn.clicked.connect(lambda checked, v=value: self.flaresolverr_input.setText(v))
            fs_quick_row.addWidget(btn)
        fs_quick_row.addStretch()
        prx.addLayout(fs_quick_row)

        # Hybrid/Vollmodus-Checkbox
        self.fs_hybrid_check = QCheckBox(
            "Hybrid-Modus  (schnell, aber kann bei aggressivem Cloudflare fehlschlagen)"
        )
        self.fs_hybrid_check.setChecked(False)   # Standard: Vollmodus (zuverlässig)
        self.fs_hybrid_check.setToolTip(
            "AUS (Default, empfohlen): Alle HTML-Anfragen laufen über\n"
            "  FlareSolverr. Zuverlässig auch bei TLS-Fingerprint-Detection\n"
            "  und Bot-Score-Checks. Langsam (~10-30s pro Seite).\n\n"
            "AN: Erste Anfrage geht direkt, FlareSolverr nur wenn\n"
            "  Cloudflare blockt. Sehr schnell wenn die Site nur\n"
            "  Standard-Cloudflare hat. Bei aggressivem Schutz\n"
            "  (TLS-Fingerprint, leere 200-Antworten) → 0 Alben gefunden."
        )
        prx.addWidget(self.fs_hybrid_check)

        prx.addWidget(_sep())

        # Info-Box
        info_box = QTextEdit()
        info_box.setReadOnly(True)
        info_box.setFixedHeight(160)
        info_box.setStyleSheet(
            "background:#161616; border:1px solid #333; border-radius:4px;"
            "font-family:Segoe UI; font-size:11px; color:#888;"
        )
        info_box.setPlainText(
            "HTTP-Proxy:\n"
            "  Alle Verbindungen werden transparent durchgeleitet.\n"
            "  • Tor (SOCKS5, Port 9050): langsam aber anonym.\n"
            "  • Burp/mitmproxy: für Debugging.\n"
            "  • 'SSL verifizieren' deaktivieren wenn der Proxy\n"
            "    das Zertifikat ersetzt (z.B. mitmproxy).\n\n"
            "FlareSolverr (Cloudflare-Umgehung):\n"
            "  • Vollmodus (Default): alle HTML-Anfragen laufen über\n"
            "    FlareSolverr-Browser-Sessions. Zuverlässig auch bei\n"
            "    aggressivem Schutz (TLS-Fingerprint Detection).\n"
            "    Langsam: ~10-30s pro HTML-Seite.\n"
            "  • Hybrid-Modus: erste Anfrage direkt, FlareSolverr nur\n"
            "    bei Cloudflare-Block. Schnell, funktioniert aber NICHT\n"
            "    wenn Cloudflare TLS-Fingerprints prüft (dann kommen\n"
            "    leere 200-Antworten ohne Inhalt zurück).\n\n"
            "  Bild-Downloads laufen IMMER direkt mit den FlareSolverr-\n"
            "  Cookies – das ist 100x schneller.\n\n"
            "  Empfehlung: Standard (Vollmodus) lassen. Hybrid nur\n"
            "  einschalten wenn du sicher bist dass die Site nur\n"
            "  Standard-Cloudflare ohne Bot-Detection nutzt."
        )
        prx.addWidget(info_box)
        prx.addStretch()

        # Tabs zusammenbauen
        self._tabs.addTab(tab1, "⬇  Download")
        self._tabs.addTab(tab2, "🔀  Proxy")

        # ===================== RECHTE SPALTE – Kategorie-Baum =====================
        right_widget = QWidget()
        right = QVBoxLayout(right_widget)
        right.setContentsMargins(8, 16, 16, 16)
        right.setSpacing(6)

        right.addWidget(_make_label("Gallery-Struktur"))
        self.tree = GalleryTreeWidget()
        right.addWidget(self.tree)

        # Legende
        legend_row = QHBoxLayout()
        for color, label in [("#5a8aff","Neu"), ("#f0c040","Fortsetzen"), ("#555","Überspringen"), ("#3abf6a","Fertig")]:
            dot = QLabel("●")
            dot.setStyleSheet(f"color:{color}; font-size:14px;")
            lbl = QLabel(label)
            lbl.setStyleSheet("color:#888; font-size:10px; margin-right:10px;")
            legend_row.addWidget(dot)
            legend_row.addWidget(lbl)
        legend_row.addStretch()
        right.addLayout(legend_row)

        splitter.addWidget(left_widget)
        splitter.addWidget(right_widget)
        splitter.setSizes([620, 380])

    # ------------------------------------------------------------------
    # Signals
    # ------------------------------------------------------------------

    def _connect_signals(self) -> None:
        s = self._sig
        s.info.connect(lambda t: self._log(t, "#e0e0e0"))
        s.error.connect(lambda t: self._log(t, "#f04040"))
        s.abort.connect(self._on_abort)
        s.resolve_init.connect(self._on_resolve_init)
        s.resolve_tick.connect(self._on_resolve_tick)
        s.dl_start.connect(self._on_dl_start)
        s.dl_progress.connect(self._on_dl_progress)
        s.dl_done.connect(self._on_dl_done)
        s.dl_error.connect(self._on_dl_error)
        s.gallery_album.connect(lambda info: self.tree.add_album(info))
        s.album_active.connect(self.tree.set_active)
        s.album_done.connect(self.tree.set_done)

    def _on_abort(self, msg: str) -> None:
        self._log(msg, "#ff5555")
        # Direkt Start-Button freigeben – Thread läuft noch kurz aus, aber
        # finished() kommt danach und setzt ihn nochmals (idempotent)
        self.start_btn.setEnabled(True)
        self.start_btn.setText("▶  Start")

    def _on_resolve_init(self, total: int) -> None:
        self.resolve_bar.setRange(0, total)
        self.resolve_bar.setValue(0)
        self.resolve_count_lbl.setText(f"0 / {total}")

    def _on_resolve_tick(self, current: int, total: int) -> None:
        self.resolve_bar.setValue(current)
        self.resolve_count_lbl.setText(f"{current} / {total}")

    # Maximale Anzahl FileProgressRow-Widgets – verhindert Speicherleck und
    # Qt-Layout-Slowdown bei langen Läufen. Reduziert von 150 → 50.
    _MAX_DL_ROWS = 50

    def _on_dl_start(self, filename: str, total: int) -> None:
        # Älteste Rows entfernen wenn Limit erreicht
        while len(self._dl_rows) >= self._MAX_DL_ROWS:
            oldest_key = next(iter(self._dl_rows))
            old_row = self._dl_rows.pop(oldest_key)
            self._dl_layout.removeWidget(old_row)
            old_row.deleteLater()

        row = FileProgressRow(filename, total)
        self._dl_rows[filename] = row
        idx = self._dl_layout.count() - 1
        self._dl_layout.insertWidget(idx, row)
        # Auto-Scroll nur wenn User bereits am Ende ist
        sb = self._dl_scroll.verticalScrollBar()
        if sb.value() >= sb.maximum() - 50:
            sb.setValue(sb.maximum())

    def _on_dl_progress(self, filename: str, downloaded: int) -> None:
        if filename in self._dl_rows:
            self._dl_rows[filename].update_progress(downloaded)

    def _on_dl_done(self, filename: str) -> None:
        if filename in self._dl_rows:
            self._dl_rows[filename].mark_done()

    def _on_dl_error(self, filename: str, msg: str) -> None:
        if filename in self._dl_rows:
            self._dl_rows[filename].mark_error()
        self._log(f"❌ {filename}: {msg}", "#f04040")

    _MAX_LOG_LINES   = 2000
    _LOG_TRIM_EVERY  = 100      # nur alle 100 Zeilen prüfen ob getrimmt werden muss
    _log_lines_since_check = 0

    def _log(self, text: str, color: str) -> None:
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        cursor = self.output.textCursor()

        # Nur periodisch Trimming prüfen — blockCount() + movePosition über
        # tausende Zeilen ist teuer und blockiert die GUI bei häufigen Logs
        self._log_lines_since_check += 1
        if self._log_lines_since_check >= self._LOG_TRIM_EVERY:
            self._log_lines_since_check = 0
            block_count = self.output.document().blockCount()
            if block_count > self._MAX_LOG_LINES:
                trim = QTextCursor(self.output.document())
                trim.movePosition(QTextCursor.MoveOperation.Start)
                trim.movePosition(
                    QTextCursor.MoveOperation.Down, QTextCursor.MoveMode.KeepAnchor,
                    block_count - self._MAX_LOG_LINES,
                )
                trim.removeSelectedText()

        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text + "\n", fmt)
        # Auto-Scroll nur wenn User nicht hochgescrollt hat
        sb = self.output.verticalScrollBar()
        if sb.value() >= sb.maximum() - 50:
            self.output.setTextCursor(cursor)
            self.output.ensureCursorVisible()

    def _pick_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Zielordner wählen", self.dest_input.text())
        if folder:
            self.dest_input.setText(folder)

    def _clear_all(self) -> None:
        self.output.clear()
        for row in self._dl_rows.values():
            self._dl_layout.removeWidget(row)
            row.deleteLater()
        self._dl_rows.clear()
        self.resolve_bar.setValue(0)
        self.resolve_bar.setRange(0, 1)
        self.resolve_count_lbl.setText("")
        self.tree.clear_tree()
        
    # ------------------------------------------------------------------
    # Splash
    # ------------------------------------------------------------------

    def _show_splash(self) -> None:
        splash = SplashScreen(
            build_info=NAMEandVERSION,
            parent=self,
        )
        splash.show_centered(self)

    def _start(self) -> None:
        if self._worker and self._worker.isRunning():
            return
        url                  = self.url_input.text().strip()
        dest                 = Path(self.dest_input.text().strip())
        prefix               = self.prefix_input.text().strip()
        parallel             = self.parallel_spin.value()
        resolve_concurrency  = self.resolve_spin.value()
        dl_delay             = float(self.delay_spin.value())
        rate_limit_delay     = float(self.rl_spin.value())
        ssl                  = self.ssl_check.isChecked()
        dry_run              = self.dryrun_check.isChecked()
        proxy_url            = self.proxy_input.text().strip()
        flaresolverr_url     = self.flaresolverr_input.text().strip()
        fs_hybrid            = self.fs_hybrid_check.isChecked()
        if not url:
            self._log("⚠ Bitte eine URL eingeben.", "#f0c040")
            return
        self._clear_all()
        self.start_btn.setEnabled(False)
        self.start_btn.setText("⏳ Läuft ...")
        self.stop_btn.setEnabled(True)
        self._worker = DownloadThread(
            url, dest, prefix, parallel, ssl, dry_run, self._sig,
            resolve_concurrency=resolve_concurrency,
            dl_delay=dl_delay,
            rate_limit_delay=rate_limit_delay,
            proxy_url=proxy_url,
            flaresolverr_url=flaresolverr_url,
            fs_hybrid=fs_hybrid,
        )
        self._worker.finished.connect(self._on_finished)
        self._worker.start()

    def _stop(self) -> None:
        """Setzt abort_event und wartet sauber auf Thread-Ende."""
        if not (self._worker and self._worker.isRunning()):
            return
        self.stop_btn.setEnabled(False)
        self.stop_btn.setText("⏳ Wird gestoppt …")
        self._log("⏹ Abbruch angefordert – laufende Anfragen werden beendet …", "#f0c040")
        self._worker.stop()
        # _on_finished wird durch finished-Signal aufgerufen

    def _on_finished(self) -> None:
        self.start_btn.setEnabled(True)
        self.start_btn.setText("▶  Start")
        self.stop_btn.setEnabled(False)
        self.stop_btn.setText("⏹  Stopp")

    def _on_abort(self, msg: str) -> None:
        self._log(msg, "#ff5555")
        # abort_event ist bereits gesetzt; finished()-Signal kommt gleich
        # Button-Reset erfolgt in _on_finished


# ==========================
# Entry Point
# ==========================

if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())
