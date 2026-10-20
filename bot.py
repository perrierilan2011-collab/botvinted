"""
Bot Vinted -> Discord

Surveillance : 1 veilleur permanent par ligne de mots-clés (comme la barre de recherche Vinted, tri « plus récent »).
Seules les NOUVELLES annonces dont le titre contient tous les mots de la ligne sont envoyées.

- /setup   (owner/admin)  : menu de configuration (rôles Owner / VIP / Premium+, limite VIP)
                            + bouton pour poster le panneau public dans un salon
- /vip     (rôle VIP)     : gérer ses salons Vinted, avec une LIMITE de salons (réglable dans /setup)
- /premium (rôle Premium+): gérer ses salons Vinted, SANS limite
- Owner / admins          : aucune limite

Chaque membre crée ses "salons" privés (ex : Nike Tech, Running...) avec ses propres filtres.
Chaque salon a son webhook, les annonces arrivent avec photos, infos et bouton Acheter.
"""
import asyncio
import collections
import json
import logging
import os
import random
import re
import sqlite3
import time
import unicodedata

import aiohttp
import discord
from discord import app_commands
from dotenv import load_dotenv

try:  # empreinte TLS de vrai Chrome : c'est ce qui évite les 403 de Vinted (pip install curl_cffi)
    from curl_cffi.requests import AsyncSession as CurlSession
except ImportError:  # repli sur aiohttp (plus souvent bloqué)
    CurlSession = None

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID") or 0)
# Pause entre deux vérifications d'UN MÊME mot-clé (chaque ligne de mots-clés a son propre veilleur)
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "3"))
# Un ou plusieurs proxys séparés par des virgules (rotation automatique si Vinted bloque l'un d'eux)
PROXIES = [p.strip() for p in (os.getenv("PROXY") or "").split(",") if p.strip()]
# Nombre max de requêtes Vinted simultanées
MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", "8"))
# Garde-fou anti-ban : écart minimum moyen entre 2 requêtes Vinted (s). Si tu as beaucoup de mots-clés,
# l'intervalle de chaque veilleur est automatiquement rallongé (nb de mots-clés × MIN_REQ_GAP).
MIN_REQ_GAP = float(os.getenv("MIN_REQ_GAP", "0.35"))
# Annonces lues à chaque vérification (les plus récentes). 30 suffit largement pour du temps réel.
POLL_PER_PAGE = int(os.getenv("POLL_PER_PAGE", "30"))
PER_PAGE = 40  # pages complètes (bouton Rafraîchir)
# Limite VIP par défaut (modifiable ensuite dans /setup, par serveur)
DEFAULT_VIP_LIMIT = int(os.getenv("MAX_SALONS_PER_USER", "5"))
CATEGORY_NAME = os.getenv("CATEGORY_NAME", "🛒 Vinted")
DB_PATH = os.getenv("DB_PATH", "vinted_bot.db")
FETCH_DETAILS = os.getenv("FETCH_DETAILS", "0") == "1"
# 0 (défaut) = les mots-clés doivent être dans le TITRE (ou la marque). Fiable et instantané.
# 1 = si un mot manque dans le titre, on lit aussi la description (l'endpoint détail de Vinted est souvent bloqué).
CHECK_DESCRIPTION = os.getenv("CHECK_DESCRIPTION", "0") == "1"
PAGE_SIZE = 25  # un menu déroulant Discord = 25 choix max -> on pagine
# Bouton « Rafraîchir » : aucune limite d'annonces. Garde-fou technique uniquement : nombre max de pages
# Vinted lues par recherche (40 annonces par page) pour ne pas se faire bloquer.
BACKFILL_MAX_PAGES = int(os.getenv("BACKFILL_MAX_PAGES", "50"))
# Une annonce n'est « nouvelle » que si son id est proche des plus récents déjà vus (≈ 15 min).
# Évite d'envoyer de vieilles annonces mises en avant (promues) par Vinted.
NEW_ID_MARGIN = int(os.getenv("NEW_ID_MARGIN", "300000"))
# Verrou anti-anciennes annonces : au démarrage on mémorise le plus grand id déjà en ligne pour chaque recherche.
# Ensuite SEULES les annonces avec un id plus grand sont envoyées (≈ 3 000 ids ≈ 10 s de tolérance).
BASELINE_MARGIN = int(os.getenv("BASELINE_MARGIN", "3000"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("vinted")

COLOR = 0x09B1BA
CONDITIONS = ["Neuf avec étiquette", "Neuf sans étiquette", "Très bon état", "Bon état", "Satisfaisant"]
COUNTRIES = {
    "fr": "🇫🇷 France", "be": "🇧🇪 Belgique", "de": "🇩🇪 Allemagne", "es": "🇪🇸 Espagne",
    "it": "🇮🇹 Italie", "nl": "🇳🇱 Pays-Bas", "pl": "🇵🇱 Pologne", "pt": "🇵🇹 Portugal",
    "lu": "🇱🇺 Luxembourg", "at": "🇦🇹 Autriche", "cz": "🇨🇿 Tchéquie", "lt": "🇱🇹 Lituanie",
}
TIER_LABEL = {"owner": "👑 Owner", "premium": "💎 Premium+", "vip": "⭐ VIP"}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


# ───────────────────────── Utilitaires ─────────────────────────
def norm(text: str) -> str:
    text = unicodedata.normalize("NFD", str(text or "").lower())
    return "".join(c for c in text if unicodedata.category(c) != "Mn").strip()


def parse_list(text: str, commas: bool = True) -> list:
    """commas=False : un élément par ligne uniquement (mots-clés, exclusions)."""
    out = []
    for part in re.split(r"[,\n;]+" if commas else r"[\r\n]+", text or ""):
        part = norm(part)
        if part and part not in out:
            out.append(part)
    return out


def parse_price(text: str):
    text = (text or "").replace(",", ".").replace("€", "").strip()
    try:
        return float(text) if text else None
    except ValueError:
        return None


AGE_OPTIONS = [("Pas de limite", 0), ("30 secondes", 30), ("1 minute", 60), ("2 minutes", 120),
               ("5 minutes", 300), ("10 minutes", 600), ("30 minutes", 1800), ("1 heure", 3600)]


def fmt_age(sec) -> str:
    for label, v in AGE_OPTIONS:
        if v == (sec or 0):
            return label if v else "Pas de limite"
    return f"{sec} s"


def fmt_list(values) -> str:
    return ", ".join(values) if values else "— (tout accepter)"


def fmt_lines(values, empty="—") -> str:
    return "\n".join(f"• {v}" for v in values) if values else empty


def is_excluded(title: str, brand: str, excluded) -> bool:
    """Exclusion par mot entier (accepte le pluriel) : « lot » n'exclut plus « pilote »."""
    hay = f"{title} {brand}"
    for x in excluded:
        if re.search(r"(?<!\w)" + re.escape(x) + r"(?:s|x)?(?!\w)", hay):
            return True
    return False


def words_in(text: str, query: str) -> bool:
    """Vrai si TOUS les mots de la requête sont dans le texte (début de mot : « doudoune » trouve « doudounes »)."""
    return all(re.search(r"(?<!\w)" + re.escape(w), text) for w in query.split())


def build_query(*parts) -> str:
    words = []
    for p in parts:
        for w in norm(p).split():
            if w not in words:
                words.append(w)
    return " ".join(words)


def salon_queries(s: dict) -> list:
    """Une recherche Vinted par combinaison marque × type × modèle (ex : « nike doudoune tech fleece »)."""
    brands = s["brands"] or [""]
    types = s["item_types"] or [""]
    kws = s["keywords"] or [""]
    out = []
    for b in brands:
        for t in types:
            for k in kws:
                q = build_query(b, t, k)
                if q and q not in out:
                    out.append(q)
    return out[:20]


def parse_ts(v):
    try:
        v = int(float(v)) if v else None
        return v // 1000 if v and v > 10**11 else v
    except (TypeError, ValueError):
        return None


def ts_from_details(extra):
    """Heure de mise en ligne depuis la fiche détaillée (la recherche ne la fournit plus)."""
    if not extra:
        return None
    for k in ("created_at_ts", "created_at", "updated_at_ts"):
        ts = parse_ts(extra.get(k))
        if ts:
            return ts
    for ph in extra.get("photos") or []:
        if isinstance(ph, dict):
            ts = parse_ts((ph.get("high_resolution") or {}).get("timestamp"))
            if ts:
                return ts
    return None


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", norm(text)).strip("-")[:60] or "salon"


# ───────────────────────── Base de données ─────────────────────────
LIST_FIELDS = ("keywords", "excluded", "brands", "sizes", "conditions", "item_types")
CFG_FIELDS = ("owner_role_id", "vip_role_id", "premium_role_id", "vip_limit")


class DB:
    def __init__(self, path):
        self.c = sqlite3.connect(path, check_same_thread=False)
        self.c.row_factory = sqlite3.Row
        self.c.execute("""CREATE TABLE IF NOT EXISTS salons(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, guild_id INTEGER, name TEXT,
            channel_id INTEGER, webhook_url TEXT,
            keywords TEXT DEFAULT '[]', excluded TEXT DEFAULT '[]',
            brands TEXT DEFAULT '[]', sizes TEXT DEFAULT '[]',
            conditions TEXT DEFAULT '[]',
            price_min REAL, price_max REAL,
            country TEXT DEFAULT 'fr', active INTEGER DEFAULT 1)""")
        try:
            self.c.execute("ALTER TABLE salons ADD COLUMN max_age INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # colonne déjà présente
        try:
            self.c.execute("ALTER TABLE salons ADD COLUMN item_types TEXT DEFAULT '[]'")
        except sqlite3.OperationalError:
            pass  # colonne déjà présente
        # Configuration par serveur (rôles + limite VIP), réglée via /setup
        self.c.execute("""CREATE TABLE IF NOT EXISTS guild_config(
            guild_id INTEGER PRIMARY KEY,
            owner_role_id INTEGER,
            vip_role_id INTEGER,
            premium_role_id INTEGER,
            vip_limit INTEGER DEFAULT 5)""")
        self.c.commit()

    @staticmethod
    def _row(row):
        if row is None:
            return None
        d = dict(row)
        for f in LIST_FIELDS:
            d[f] = json.loads(d[f] or "[]")
        return d

    # --- config serveur ---
    def get_cfg(self, guild_id) -> dict:
        row = self.c.execute("SELECT * FROM guild_config WHERE guild_id=?", (guild_id,)).fetchone()
        if row is None:
            return {"guild_id": guild_id, "owner_role_id": None, "vip_role_id": None,
                    "premium_role_id": None, "vip_limit": DEFAULT_VIP_LIMIT}
        d = dict(row)
        if d.get("vip_limit") is None:
            d["vip_limit"] = DEFAULT_VIP_LIMIT
        return d

    def set_cfg(self, guild_id, **f):
        f = {k: v for k, v in f.items() if k in CFG_FIELDS}
        if not f:
            return
        self.c.execute("INSERT OR IGNORE INTO guild_config(guild_id, vip_limit) VALUES(?, ?)",
                       (guild_id, DEFAULT_VIP_LIMIT))
        sets = ",".join(f"{k}=?" for k in f)
        self.c.execute(f"UPDATE guild_config SET {sets} WHERE guild_id=?", [*f.values(), guild_id])
        self.c.commit()

    # --- salons ---
    def add(self, **f) -> int:
        for k in LIST_FIELDS:
            if k in f:
                f[k] = json.dumps(f[k])
        cols = ",".join(f)
        cur = self.c.execute(f"INSERT INTO salons({cols}) VALUES({','.join('?' * len(f))})", list(f.values()))
        self.c.commit()
        return cur.lastrowid

    def get(self, sid):
        return self._row(self.c.execute("SELECT * FROM salons WHERE id=?", (sid,)).fetchone())

    def by_user(self, user_id, guild_id):
        rows = self.c.execute("SELECT * FROM salons WHERE user_id=? AND guild_id=? ORDER BY id",
                              (user_id, guild_id)).fetchall()
        return [self._row(r) for r in rows]

    def active(self):
        return [self._row(r) for r in self.c.execute("SELECT * FROM salons WHERE active=1").fetchall()]

    def update(self, sid, **f):
        for k in LIST_FIELDS:
            if k in f:
                f[k] = json.dumps(f[k])
        sets = ",".join(f"{k}=?" for k in f)
        self.c.execute(f"UPDATE salons SET {sets} WHERE id=?", [*f.values(), sid])
        self.c.commit()

    def delete(self, sid):
        self.c.execute("DELETE FROM salons WHERE id=?", (sid,))
        self.c.commit()


db = DB(DB_PATH)


# ───────────────────────── Rôles & limites ─────────────────────────
def has_role(member, role_id) -> bool:
    return bool(role_id) and hasattr(member, "get_role") and member.get_role(int(role_id)) is not None


def is_owner(member) -> bool:
    """Owner = administrateur du serveur OU membre du rôle Owner configuré dans /setup."""
    perms = getattr(member, "guild_permissions", None)
    if perms is None:
        return False
    if perms.administrator:
        return True
    return has_role(member, db.get_cfg(member.guild.id)["owner_role_id"])


def access(member):
    """Retourne (niveau, limite). limite = None -> illimité. niveau = None -> aucun accès."""
    if getattr(member, "guild", None) is None:
        return None, 0
    if is_owner(member):
        return "owner", None
    cfg = db.get_cfg(member.guild.id)
    if has_role(member, cfg["premium_role_id"]):
        return "premium", None
    if has_role(member, cfg["vip_role_id"]):
        return "vip", int(cfg["vip_limit"])
    return None, 0


# ───────────────────────── Client Vinted ─────────────────────────
def extract_items(data):
    """Trouve la liste d'annonces dans la réponse, quel que soit le format."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("items", "catalog_items"):
            if isinstance(data.get(k), list):
                return data[k]
        inner = data.get("data")
        if isinstance(inner, dict):
            return extract_items(inner)
    return None


def canon_status(text):
    """Reconnaît un état Vinted (français OU anglais) et le ramène au libellé français de CONDITIONS."""
    t = norm(text)
    if not t:
        return None
    if t.startswith(("neuf avec", "new with", "brand new with")):
        return CONDITIONS[0]
    if t.startswith(("neuf sans", "new without", "brand new without")):
        return CONDITIONS[1]
    if t in ("tres bon etat", "very good"):
        return CONDITIONS[2]
    if t in ("bon etat", "good"):
        return CONDITIONS[3]
    if t in ("satisfaisant", "satisfactory"):
        return CONDITIONS[4]
    return None


def parse_attrs(it: dict):
    """(marque, taille, état). La nouvelle API les met dans item_box :
    - first_line = marque (seulement si elle diffère du titre)
    - second_line = « taille · état » (la taille n'existe que si l'annonce en a une !)
    - accessibility_label = « …, marque : X, état : Y, taille : Z, prix… » (le plus explicite)
    On lit donc par CONTENU (un état connu est l'état, le reste est la taille), jamais par position."""
    brand = it.get("brand_title") or ""
    size = it.get("size_title") or ""
    status = canon_status(it.get("status")) or ""
    box = it.get("item_box") or {}
    label = box.get("accessibility_label") or ""
    if label:
        def grab(*names):
            m = re.search(r"(?:" + "|".join(names) + r")\s*:\s*([^,]+)", label, re.I)
            return m.group(1).strip() if m else ""
        brand = brand or grab("brand", "marque")
        size = size or grab("size", "taille")
        status = status or canon_status(grab("condition", "état", "etat")) or ""
    for part in re.split(r"[·•|]", box.get("second_line") or ""):
        part = part.strip()
        if not part:
            continue
        c = canon_status(part)
        if c:
            status = status or c
        elif not size:
            size = re.sub(r"^(?:taille|size)\s*:?\s*", "", part, flags=re.I)
    first = (box.get("first_line") or "").strip()
    if not brand and first and norm(first) != norm(it.get("title")):
        brand = first
    return brand, size, status


def norm_item(it: dict, domain: str) -> dict:
    price = it.get("price")
    if isinstance(price, dict):
        amount, cur = price.get("amount"), price.get("currency_code", "EUR")
    else:
        amount, cur = price, it.get("currency", "EUR")
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        amount = None
    photo = it.get("photo")
    purl, ts = None, None
    if isinstance(photo, dict):
        purl = photo.get("full_size_url") or photo.get("url")
        ts = (photo.get("high_resolution") or {}).get("timestamp")
    elif isinstance(photo, str):
        purl = photo
    if not purl and it.get("photos"):
        first = it["photos"][0]
        purl = (first.get("full_size_url") or first.get("url")) if isinstance(first, dict) else first
    if not ts:
        ts = it.get("created_at_ts") or it.get("created_at")
    try:
        ts = int(float(ts)) if ts else None
        if ts and ts > 10**11:  # millisecondes -> secondes
            ts //= 1000
    except (TypeError, ValueError):
        ts = None
    user = it.get("user") or {}
    brand, size, status = parse_attrs(it)
    url = it.get("url") or it.get("path") or f"/items/{it['id']}"
    if url.startswith("/"):
        url = f"https://www.vinted.{domain}{url}"
    return {
        "id": int(it["id"]), "title": it.get("title") or "", "price": amount, "currency": cur,
        "brand": brand, "size": size, "box": (it.get("item_box") or {}),
        "status": status, "url": url, "photo": purl, "ts": ts,
        "seller": user.get("login"),
        "seller_url": user.get("profile_url") or (f"https://www.vinted.{domain}/member/{user['id']}" if user.get("id") else None),
        "domain": domain,
    }


# Équivalent « données » de la page https://www.vinted.fr/catalog?order=newest_first&search_text=... :
# on essaie les endpoints dans l'ordre et on garde le premier qui répond (404 -> endpoint suivant).
API_URLS = [u for u in (os.getenv("VINTED_API_URL"),
                        "https://api.vinted.{d}/svc-catalogue/items",
                        "https://www.vinted.{d}/api/v2/catalog/items") if u]
TOKEN_TTL = 25 * 60  # le token anonyme dure ~2 h ; on le renouvelle bien avant


class VintedClient:
    """Même requête que la barre de recherche du site : /api/v2/catalog/items?search_text=...&order=newest_first
    - cookies de session (access_token_web) récupérés sur la page d'accueil, renouvelés automatiquement
    - empreinte TLS Chrome via curl_cffi (sinon repli aiohttp)
    - rotation de proxy si Vinted renvoie 403/429"""

    def __init__(self):
        self.tokens: dict = {}
        self._lastlog: dict = {}
        self.sessions: dict = {}
        self.locks: dict = {}
        self.cooldown: dict = {}
        self.proxy_i = 0
        self.ok_logged: set = set()
        self.token_at: dict = {}
        self.api_idx: dict = {}

    def _proxy(self):
        return PROXIES[self.proxy_i % len(PROXIES)] if PROXIES else None

    def _rotate_proxy(self):
        if len(PROXIES) > 1:
            self.proxy_i += 1
            log.info("Rotation de proxy → #%s", self.proxy_i % len(PROXIES) + 1)

    def _headers(self, domain):
        h = {"Accept": "application/json, text/plain, */*", "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
             "Referer": f"https://www.vinted.{domain}/", "Origin": f"https://www.vinted.{domain}"}
        if CurlSession is None:
            h["User-Agent"] = UA
        tok = self.tokens.get(domain, {})
        if tok.get("bearer"):
            h["Authorization"] = f"Bearer {tok['bearer']}"
        if tok.get("anon"):
            h["X-Anon-Id"] = tok["anon"]
        if tok.get("csrf"):
            h["X-CSRF-Token"] = tok["csrf"]
        return h

    def _new_session(self):
        if CurlSession is not None:
            return CurlSession(impersonate="chrome")
        return aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar())

    async def _get(self, domain, url, params=None, timeout=8, method="GET"):
        """Requête -> (status, texte). Même interface avec curl_cffi ou aiohttp."""
        sess = self.sessions[domain]
        proxy = self._proxy()
        if CurlSession is not None:
            r = await sess.request(method, url, params=params, headers=self._headers(domain), timeout=timeout,
                                   proxies={"http": proxy, "https": proxy} if proxy else None)
            return r.status_code, ("" if method == "HEAD" else r.text)
        async with sess.request(method, url, params=params, headers=self._headers(domain), proxy=proxy,
                                timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            return r.status, ("" if method == "HEAD" else await r.text())

    def _cookie(self, domain, name):
        """Lit un cookie de la session (access_token_web = token anonyme utilisé en Bearer)."""
        sess = self.sessions[domain]
        try:
            if CurlSession is not None:
                val = None
                for c in sess.cookies.jar:  # on garde le dernier non vide
                    if c.name == name and c.value:
                        val = c.value
                return val
            val = None
            for c in sess.cookie_jar:
                if c.key == name and c.value:
                    val = c.value
            return val
        except Exception:
            return None

    async def _session(self, domain):
        if domain not in self.sessions:
            self.sessions[domain] = self._new_session()
            self.locks[domain] = asyncio.Lock()
            await self.refresh(domain)
        return self.sessions[domain]

    async def refresh(self, domain):
        """Comme un navigateur : charge /catalog pour obtenir le token anonyme (cookie access_token_web)."""
        lock = self.locks.setdefault(domain, asyncio.Lock())
        if lock.locked():  # un autre veilleur est déjà en train de le faire : on attend juste
            async with lock:
                return
        async with lock:
            if domain not in self.sessions:
                return
            tok = self.tokens.setdefault(domain, {})
            try:
                status, _ = await self._get(domain, f"https://www.vinted.{domain}/catalog", timeout=12, method="HEAD")
                bearer = self._cookie(domain, "access_token_web")
                anon = csrf = None
                if not bearer or status >= 400:  # repli : la page d'accueil complète
                    status, html = await self._get(domain, f"https://www.vinted.{domain}/", timeout=12)
                    bearer = self._cookie(domain, "access_token_web") or bearer
                    m = re.search(r'anon_id\\?"\s*:\s*\\?"([0-9a-fA-F-]{20,})', html)
                    anon = m.group(1) if m else None
                    m = re.search(r'csrf_token\\?"\s*:\s*\\?"([^"\\]+)', html, re.I)
                    csrf = m.group(1) if m else None
                tok.update({"bearer": bearer, "anon": anon or tok.get("anon"), "csrf": csrf or tok.get("csrf")})
                self.token_at[domain] = time.time()
                log.info("Session Vinted.%s initialisée (HTTP %s, token=%s, moteur=%s)", domain, status,
                         "OK" if bearer else "ABSENT", "curl_cffi/Chrome" if CurlSession else "aiohttp")
                if status in (403, 429):
                    self._rotate_proxy()
            except Exception as e:
                log.warning("Initialisation session %s échouée: %s", domain, e)

    def _warn(self, key, msg, *args):
        """Évite de spammer les logs : 1 message identique toutes les 30 s."""
        now = time.time()
        if now - self._lastlog.get(key, 0) > 30:
            self._lastlog[key] = now
            log.warning(msg, *args)

    async def search(self, domain, text, page=1, per_page=PER_PAGE):
        """Équivalent de taper `text` dans la barre de recherche Vinted, triée par plus récent."""
        if time.time() < self.cooldown.get(domain, 0):
            return None
        try:
            await self._session(domain)
        except Exception as e:
            self._warn((domain, "sess"), "Session Vinted.%s impossible: %s", domain, e)
            return None
        if time.time() - self.token_at.get(domain, 0) > TOKEN_TTL:
            await self.refresh(domain)
        # Mêmes paramètres que l'URL du site : catalog?order=newest_first&page=1&search_by_image_uuid=
        # &search_by_image_id=&time=...&search_text=<mots-clés>  (search_text = la ligne de mots-clés du salon)
        params = {"order": "newest_first", "page": str(page), "per_page": str(per_page),
                  "search_by_image_uuid": "", "search_by_image_id": "",
                  "time": str(int(time.time())), "search_text": text}
        idx = self.api_idx.get(domain, 0) % len(API_URLS)
        url = API_URLS[idx].format(d=domain)
        for attempt in range(3):
            try:
                status, body = await self._get(domain, url, params)
            except Exception as e:
                self._warn((domain, "exc"), "Erreur requête Vinted « %s » : %s", text, e)
                self._rotate_proxy()
                return None
            if status in (401, 403) and attempt == 0:
                await self.refresh(domain)  # cookie expiré -> on le renouvelle et on retente
                continue
            if status == 404 and attempt < len(API_URLS) - 1:
                self.api_idx[domain] = (idx + 1) % len(API_URLS)
                idx = self.api_idx[domain]
                url = API_URLS[idx].format(d=domain)
                log.info("Endpoint introuvable, essai de : %s", url)
                continue
            if status == 429:
                self.cooldown[domain] = time.time() + 30
                self._rotate_proxy()
                log.warning("Rate limit sur vinted.%s, pause 30 s (augmente POLL_INTERVAL ou ajoute des proxys)", domain)
                return None
            if status != 200:
                self._warn((domain, status), "Vinted.%s a répondu HTTP %s sur %s%s", domain, status, url,
                           " : IP bloquée → mets un proxy résidentiel dans PROXY" if status in (401, 403) else
                           (" : endpoint introuvable (Vinted a encore changé son API ?)" if status == 404 else ""))
                if status in (401, 403):
                    self._rotate_proxy()
                return None
            try:
                data = json.loads(body)
            except ValueError:
                self._warn((domain, "json"), "Réponse non-JSON de Vinted.%s (page de protection anti-bot ?)", domain)
                return None
            raw = extract_items(data)
            if raw is None:
                self._warn((domain, "fmt"), "Réponse inattendue de Vinted, clés : %s",
                           list(data.keys()) if isinstance(data, dict) else type(data))
                return None
            normed = [norm_item(i, domain) for i in raw if isinstance(i, dict) and i.get("id")]
            if domain not in self.ok_logged:
                self.ok_logged.add(domain)
                sample = raw[0] if raw and isinstance(raw[0], dict) else {}
                log.info("✅ API Vinted.%s OK | %s annonces | clés d'une annonce : %s | heure de mise en ligne dispo : %s",
                         domain, len(normed), sorted(sample.keys()), any(i["ts"] for i in normed))
                if normed:
                    log.info("Exemple lu : %s", {k: normed[0][k] for k in ("id", "title", "price", "brand", "size", "status")})
                    log.info("item_box brut : %s", json.dumps(normed[0].get("box"), ensure_ascii=False)[:400])
            return normed
        return None

    async def details(self, domain, item_id):
        """Vinted a supprimé l'API de détail d'une annonce (sept. 2026) : on ne la demande plus."""
        return None

    async def close(self):
        for s in self.sessions.values():
            try:
                r = s.close()
                if asyncio.iscoroutine(r):
                    await r
            except Exception:
                pass


# ───────────────────────── Filtres ─────────────────────────
def filter_reason(s: dict, it: dict):
    """None si l'annonce passe tous les filtres du salon, sinon la raison du rejet."""
    title = norm(it["title"])
    brand = norm(it["brand"])
    if is_excluded(title, brand, s["excluded"]):
        return "exclu"
    if it["price"] is not None:
        if s["price_min"] is not None and it["price"] < s["price_min"]:
            return "prix"
        if s["price_max"] is not None and it["price"] > s["price_max"]:
            return "prix"
    if s["brands"] and not any(b in brand or b in title for b in s["brands"]):
        return "marque"
    if s["sizes"]:
        size = norm(it["size"])
        tokens = {t for t in re.split(r"[\s/,\-–—·]+", size) if t}
        if not any(w == size or w in tokens for w in s["sizes"]):
            return "taille"  # taille différente OU annonce sans taille
    if s["conditions"]:
        if norm(it["status"]) not in {norm(c) for c in s["conditions"]}:
            return "état"
    return None


def basic_ok(s: dict, it: dict) -> bool:
    return filter_reason(s, it) is None


# ───────────────────────── Message d'annonce ─────────────────────────
def build_message(s: dict, it: dict, extra: dict | None):
    photos = [it["photo"]] if it["photo"] else []
    desc = None
    rating = None
    if extra:
        photos = [p.get("full_size_url") or p.get("url") for p in (extra.get("photos") or [])] or photos
        desc = (extra.get("description") or "").strip()
        u = extra.get("user") or {}
        rep, cnt = u.get("feedback_reputation"), u.get("feedback_count")
        if rep is not None:
            rating = f"⭐ {float(rep) * 5:.1f}/5" + (f" ({cnt} avis)" if cnt else "")
    photos = [p for p in photos if p][:4]
    price = f"{it['price']:.2f} {it['currency']}".replace("EUR", "€") if it["price"] is not None else "?"
    e = discord.Embed(title=it["title"][:250], url=it["url"], color=COLOR)
    e.add_field(name="💶 Prix", value=f"**{price}**", inline=True)
    e.add_field(name="📏 Taille", value=it["size"] or "—", inline=True)
    e.add_field(name="🏷️ Marque", value=it["brand"] or "—", inline=True)
    e.add_field(name="✨ État", value=it["status"] or "—", inline=True)
    seller = f"[{it['seller']}]({it['seller_url']})" if it["seller"] and it["seller_url"] else (it["seller"] or "—")
    e.add_field(name="👤 Vendeur", value=seller + (f"\n{rating}" if rating else ""), inline=True)
    if it["ts"]:
        e.add_field(name="🕒 Mis en ligne", value=f"<t:{int(it['ts'])}:R>", inline=True)
    if desc:
        e.add_field(name="📝 Description", value=desc[:300] + ("…" if len(desc) > 300 else ""), inline=False)
    e.add_field(name="🛒 Acheter", value=f"[**Ouvrir l'annonce sur Vinted**]({it['url']})", inline=False)
    e.set_footer(text=f"Alerte : {s['name']} • Vinted {it['domain'].upper()}")
    if photos:
        e.set_image(url=photos[0])
    embeds = [e]
    # Astuce Discord : plusieurs embeds avec la même URL = galerie de photos
    for p in photos[1:]:
        embeds.append(discord.Embed(url=it["url"], color=COLOR).set_image(url=p))
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="🛒 Acheter", url=it["url"], style=discord.ButtonStyle.link))
    if it["seller_url"]:
        view.add_item(discord.ui.Button(label="👤 Vendeur", url=it["seller_url"], style=discord.ButtonStyle.link))
    return embeds, view


# ───────────────────────── Menus (interface) ─────────────────────────
def main_payload(user, guild_id, page=0):
    tier, limit = access(user)
    salons = db.by_user(user.id, guild_id)
    pages = max(1, -(-len(salons) // PAGE_SIZE))
    page = min(max(page, 0), pages - 1)
    chunk = salons[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    if tier is None:
        quota = "*⚠️ Tu n'as plus de rôle qui permet de créer des salons.*"
    elif limit is None:
        quota = f"*{TIER_LABEL[tier]} • salons **illimités** ({len(salons)} actuellement).*"
    else:
        quota = f"*{TIER_LABEL[tier]} • **{len(salons)}/{limit}** salons utilisés.*"

    e = discord.Embed(title="📂 Tes salons Vinted", color=COLOR)
    e.description = (
        "Un **salon** = une recherche avec son propre salon privé.\n"
        "Exemple : un salon **Nike Tech**, un autre **Running**, etc. Rien n'est mélangé !\n\n"
        + ("**Tes salons :**\n" + "\n".join(
            f"{'🟢' if s['active'] else '⏸️'} **{s['name']}** → <#{s['channel_id']}>" for s in chunk)
           if chunk else "Tu n'as pas encore de salon. Clique sur **➕ Nouveau salon** pour commencer.")
        + f"\n\n{quota}"
    )
    if pages > 1:
        e.set_footer(text=f"Page {page + 1}/{pages}")
    return e, MainView(chunk, page, pages)


class MainView(discord.ui.View):
    def __init__(self, chunk, page=0, pages=1):
        super().__init__(timeout=900)
        if chunk:
            self.add_item(SalonSelect(chunk))
        self.add_item(NewButton())
        if pages > 1:
            self.add_item(PageButton("◀️", page - 1, disabled=page <= 0))
            self.add_item(PageButton("▶️", page + 1, disabled=page >= pages - 1))


class PageButton(discord.ui.Button):
    def __init__(self, label, target, disabled=False):
        super().__init__(label=label, style=discord.ButtonStyle.secondary, row=1, disabled=disabled)
        self.target = target

    async def callback(self, interaction: discord.Interaction):
        e, v = main_payload(interaction.user, interaction.guild_id, self.target)
        await interaction.response.edit_message(embed=e, view=v)


class SalonSelect(discord.ui.Select):
    def __init__(self, salons):
        opts = [discord.SelectOption(label=s["name"][:100], value=str(s["id"]),
                                     description=(", ".join(s["item_types"] + s["keywords"]) or "aucun mot-clé")[:100])
                for s in salons]
        super().__init__(placeholder="✏️ Choisir un salon à modifier…", options=opts, row=0)

    async def callback(self, interaction: discord.Interaction):
        s = db.get(int(self.values[0]))
        await interaction.response.edit_message(embed=salon_embed(s), view=SalonView(s))


class NewButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="➕ Nouveau salon", style=discord.ButtonStyle.success, row=1)

    async def callback(self, interaction: discord.Interaction):
        err = check_can_create(interaction.user, interaction.guild_id)
        if err:
            return await interaction.response.send_message(err, ephemeral=True)
        await interaction.response.send_modal(NewSalonModal())


def check_can_create(member, guild_id):
    """Retourne un message d'erreur si le membre ne peut pas créer de salon, sinon None."""
    tier, limit = access(member)
    if tier is None:
        return "❌ Tu n'as pas (ou plus) le rôle nécessaire pour créer des salons."
    if limit is not None and len(db.by_user(member.id, guild_id)) >= limit:
        return f"❌ Tu as atteint la limite de {limit} salons. Supprime-en un d'abord."
    return None


async def find_category(guild: discord.Guild):
    """Trouve (ou crée) une catégorie qui n'est pas pleine (limite Discord : 50 salons)."""
    i = 1
    while True:
        name = CATEGORY_NAME if i == 1 else f"{CATEGORY_NAME} {i}"
        cat = discord.utils.get(guild.categories, name=name)
        if cat is None:
            return await guild.create_category(name, overwrites={
                guild.default_role: discord.PermissionOverwrite(view_channel=False)})
        if len(cat.channels) < 50:
            return cat
        i += 1


class NewSalonModal(discord.ui.Modal, title="➕ Nouveau salon"):
    def __init__(self):
        super().__init__()
        self.name_in = discord.ui.TextInput(label="Nom du salon", max_length=50,
                                            placeholder="Ex : Nike Tech")
        self.type_in = discord.ui.TextInput(label="Type d'article (ex : doudoune)", max_length=100,
                                            required=False, placeholder="doudoune")
        self.kw_in = discord.ui.TextInput(label="Modèles / mots-clés (un par ligne)", max_length=300,
                                          style=discord.TextStyle.paragraph,
                                          placeholder="Ex :\nnike tech\nnike trail")
        self.add_item(self.name_in)
        self.add_item(self.type_in)
        self.add_item(self.kw_in)

    async def on_submit(self, interaction: discord.Interaction):
        guild, member = interaction.guild, interaction.user
        # On revérifie au moment de la création (rôle retiré / limite atteinte entre-temps)
        err = check_can_create(member, guild.id)
        if err:
            return await interaction.response.send_message(err, ephemeral=True)
        await interaction.response.defer()
        try:
            cat = await find_category(guild)
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                member: discord.PermissionOverwrite(view_channel=True, read_message_history=True,
                                                    send_messages=False),
                guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True,
                                                      manage_webhooks=True, manage_channels=True,
                                                      manage_messages=True, read_message_history=True),
            }
            channel = await guild.create_text_channel(
                f"{slugify(self.name_in.value)}-{slugify(member.name)}"[:95], category=cat,
                overwrites=overwrites, topic=f"Alertes Vinted de {member} • {self.name_in.value}")
            wh = await channel.create_webhook(name="Vinted Bot")
        except discord.Forbidden:
            return await interaction.followup.send(
                "❌ Il me manque des permissions : **Gérer les salons** et **Gérer les webhooks**.", ephemeral=True)
        sid = db.add(user_id=member.id, guild_id=guild.id, name=self.name_in.value.strip(),
                     channel_id=channel.id, webhook_url=wh.url,
                     item_types=parse_list(self.type_in.value, commas=False),
                     keywords=parse_list(self.kw_in.value, commas=False))
        await channel.send(f"📦 {member.mention} ton salon **{self.name_in.value}** est prêt ! "
                           f"Les annonces correspondantes arriveront ici.")
        s = db.get(sid)
        await interaction.edit_original_response(embed=salon_embed(s, created=True), view=SalonView(s))


def salon_embed(s: dict, created=False):
    state = "🟢 **Actif**" if s["active"] else "⏸️ **En pause**"
    e = discord.Embed(title=f"📂 {s['name']}", color=COLOR)
    e.description = (("✅ **Salon créé !** " if created else "")
                     + f"{state} • Annonces dans <#{s['channel_id']}>\n"
                       "Utilise les boutons ci-dessous pour régler tes filtres. "
                       "Les changements sont pris en compte **immédiatement** pour les nouvelles annonces. "
                       "Clique sur **🔄 Rafraîchir** pour revoir les annonces avec tes nouveaux filtres.")
    pmin = f"{s['price_min']:g} €" if s["price_min"] is not None else "—"
    pmax = f"{s['price_max']:g} €" if s["price_max"] is not None else "—"
    e.add_field(name="👕 Types d'articles", value=fmt_lines(s["item_types"], "— (tous les types)")[:1000],
                inline=False)
    e.add_field(name="🔎 Modèles / mots-clés", value=fmt_lines(s["keywords"], "— (aucun)")[:1000], inline=False)
    e.add_field(name="🚫 Exclusions (types d'articles)", value=fmt_lines(s["excluded"])[:1000], inline=False)
    e.add_field(name="💶 Prix", value=f"min {pmin} • max {pmax}", inline=False)
    e.add_field(name="🏷️ Marques", value=fmt_list(s["brands"]), inline=True)
    e.add_field(name="📏 Tailles", value=fmt_list(s["sizes"]), inline=True)
    e.add_field(name="✨ États", value=fmt_list(s["conditions"]), inline=True)
    e.add_field(name="🌍 Pays", value=COUNTRIES.get(s["country"], s["country"]), inline=True)
    e.add_field(name="⏱️ Âge max", value=fmt_age(s.get("max_age")), inline=True)
    e.set_footer(text="💡 Ex : type « doudoune » + marque « nike » + modèles « tech fleece » (un par ligne). "
                      "Tous les mots doivent être dans le titre ou la description.")
    return e


class SalonView(discord.ui.View):
    def __init__(self, s: dict):
        super().__init__(timeout=900)
        self.sid, self.owner = s["id"], s["user_id"]
        self.toggle.label = "⏸️ Mettre en pause" if s["active"] else "▶️ Réactiver"
        self.toggle.style = discord.ButtonStyle.secondary if s["active"] else discord.ButtonStyle.success
        self.add_item(CondSelect(s))
        self.add_item(CountrySelect(s))
        self.add_item(AgeSelect(s))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Ce n'est pas ton salon.", ephemeral=True)
            return False
        return True

    async def _refresh(self, interaction):
        s = db.get(self.sid)
        await interaction.response.edit_message(embed=salon_embed(s), view=SalonView(s))

    @discord.ui.button(label="✏️ Nom & mots-clés", style=discord.ButtonStyle.primary, row=0)
    async def kw(self, interaction: discord.Interaction, _):
        await interaction.response.send_modal(KeywordsModal(db.get(self.sid)))

    @discord.ui.button(label="💶 Prix", style=discord.ButtonStyle.primary, row=0)
    async def price(self, interaction: discord.Interaction, _):
        await interaction.response.send_modal(PriceModal(db.get(self.sid)))

    @discord.ui.button(label="🏷️ Marques & tailles", style=discord.ButtonStyle.primary, row=0)
    async def brands(self, interaction: discord.Interaction, _):
        await interaction.response.send_modal(BrandsModal(db.get(self.sid)))

    @discord.ui.button(label="👕 Type", style=discord.ButtonStyle.primary, row=0)
    async def item_type(self, interaction: discord.Interaction, _):
        await interaction.response.send_modal(TypeModal(db.get(self.sid)))

    @discord.ui.button(label="🚫 Exclure", style=discord.ButtonStyle.primary, row=0)
    async def exclude(self, interaction: discord.Interaction, _):
        await interaction.response.send_modal(ExcludeModal(db.get(self.sid)))

    @discord.ui.button(label="⏸️", row=4)
    async def toggle(self, interaction: discord.Interaction, _):
        s = db.get(self.sid)
        db.update(self.sid, active=0 if s["active"] else 1)
        await self._refresh(interaction)

    @discord.ui.button(label="🗑️ Supprimer", style=discord.ButtonStyle.danger, row=4)
    async def delete(self, interaction: discord.Interaction, _):
        v = ConfirmDelete(self.sid, self.owner)
        e = discord.Embed(title="🗑️ Supprimer ce salon ?", color=0xE74C3C,
                          description="Le salon Discord et ses réglages seront **définitivement supprimés**.")
        await interaction.response.edit_message(embed=e, view=v)

    @discord.ui.button(label="⬅️ Retour", row=4)
    async def back(self, interaction: discord.Interaction, _):
        e, v = main_payload(interaction.user, interaction.guild_id)
        await interaction.response.edit_message(embed=e, view=v)

    @discord.ui.button(label="🧹 Vider", style=discord.ButtonStyle.secondary, row=4)
    async def clear_btn(self, interaction: discord.Interaction, _):
        """Supprime tous les messages du salon Discord."""
        s = db.get(self.sid)
        ch = interaction.guild.get_channel(s["channel_id"]) if s else None
        if ch is None:
            return await interaction.response.send_message("❌ Salon Discord introuvable.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        try:
            n = await bot.clear_channel(ch)
        except discord.Forbidden:
            return await interaction.followup.send(
                "❌ Il me manque la permission **Gérer les messages** (et **Voir les anciens messages**) "
                "dans ce salon.", ephemeral=True)
        await interaction.followup.send(f"🧹 Salon vidé : **{n}** message(s) supprimé(s).", ephemeral=True)

    @discord.ui.button(label="🔄 Rafraîchir", style=discord.ButtonStyle.success, row=4)
    async def refresh_btn(self, interaction: discord.Interaction, _):
        """Vide le salon puis relance la recherche comme si les mots étaient tapés dans Vinted."""
        if self.sid in bot.backfilling:
            return await interaction.response.send_message("⏳ Un rafraîchissement est déjà en cours.",
                                                           ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        try:
            n = await bot.backfill(self.sid)
        except discord.Forbidden:
            return await interaction.followup.send(
                "❌ Il me manque la permission **Gérer les messages** (et **Voir les anciens messages**) "
                "dans ce salon.", ephemeral=True)
        if n is None:
            return await interaction.followup.send(
                "⚠️ Vinted n'a pas répondu (ou aucun mot-clé/type/marque défini). Rien n'a été supprimé, réessaie.",
                ephemeral=True)
        await interaction.followup.send(
            f"🔄 Salon rafraîchi : **{n}** annonce(s) correspondante(s) envoyée(s)."
            if n else "🔄 Salon vidé, mais aucune annonce ne correspond à tes filtres en ce moment.",
            ephemeral=True)


class ConfirmDelete(discord.ui.View):
    def __init__(self, sid, owner):
        super().__init__(timeout=120)
        self.sid, self.owner = sid, owner

    @discord.ui.button(label="Oui, supprimer", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, _):
        s = db.get(self.sid)
        if s:
            ch = interaction.guild.get_channel(s["channel_id"])
            if ch:
                try:
                    await ch.delete(reason="Salon Vinted supprimé")
                except discord.HTTPException:
                    pass
            db.delete(self.sid)
        e, v = main_payload(interaction.user, interaction.guild_id)
        await interaction.response.edit_message(embed=e, view=v)

    @discord.ui.button(label="Annuler", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, _):
        s = db.get(self.sid)
        await interaction.response.edit_message(embed=salon_embed(s), view=SalonView(s))


class CondSelect(discord.ui.Select):
    def __init__(self, s):
        self.sid = s["id"]
        opts = [discord.SelectOption(label=c, value=c, default=c in s["conditions"]) for c in CONDITIONS]
        super().__init__(placeholder="✨ État de l'article (rien de coché = tous)", min_values=0,
                         max_values=len(opts), options=opts, row=1)

    async def callback(self, interaction: discord.Interaction):
        db.update(self.sid, conditions=list(self.values))
        s = db.get(self.sid)
        await interaction.response.edit_message(embed=salon_embed(s), view=SalonView(s))


class CountrySelect(discord.ui.Select):
    def __init__(self, s):
        self.sid = s["id"]
        opts = [discord.SelectOption(label=n, value=c, default=c == s["country"]) for c, n in COUNTRIES.items()]
        super().__init__(placeholder="🌍 Pays Vinted", options=opts, row=2)

    async def callback(self, interaction: discord.Interaction):
        db.update(self.sid, country=self.values[0])
        s = db.get(self.sid)
        await interaction.response.edit_message(embed=salon_embed(s), view=SalonView(s))


class AgeSelect(discord.ui.Select):
    def __init__(self, s):
        self.sid = s["id"]
        cur = s.get("max_age") or 0
        opts = [discord.SelectOption(label=("⏱️ " + l) if v else "⏱️ Pas de limite d'âge", value=str(v),
                                     description=("Annonces postées il y a moins de " + l) if v else "Toutes les nouvelles annonces",
                                     default=(v == cur)) for l, v in AGE_OPTIONS]
        super().__init__(placeholder="⏱️ Âge max de l'annonce", options=opts, row=3)

    async def callback(self, interaction: discord.Interaction):
        db.update(self.sid, max_age=int(self.values[0]))
        s = db.get(self.sid)
        await interaction.response.edit_message(embed=salon_embed(s), view=SalonView(s))


class KeywordsModal(discord.ui.Modal, title="✏️ Nom & mots-clés"):
    def __init__(self, s):
        super().__init__()
        self.sid, self.old_name = s["id"], s["name"]
        self.channel_id = s["channel_id"]
        self.name_in = discord.ui.TextInput(label="Nom du salon", max_length=50, default=s["name"])
        self.kw_in = discord.ui.TextInput(
            label="Modèles / mots-clés (un par ligne)", style=discord.TextStyle.paragraph, max_length=500,
            default="\n".join(s["keywords"]), placeholder="nike tech\nnike trail\nnike running")
        for i in (self.name_in, self.kw_in):
            self.add_item(i)

    async def on_submit(self, interaction: discord.Interaction):
        name = self.name_in.value.strip()
        db.update(self.sid, name=name, keywords=parse_list(self.kw_in.value, commas=False))
        s = db.get(self.sid)
        await interaction.response.edit_message(embed=salon_embed(s), view=SalonView(s))
        if name != self.old_name:
            ch = interaction.guild.get_channel(self.channel_id)
            if ch:
                asyncio.create_task(self._rename(ch, name, interaction.user))

    @staticmethod
    async def _rename(ch, name, member):
        try:
            await ch.edit(name=f"{slugify(name)}-{slugify(member.name)}"[:95])
        except Exception:
            pass


class TypeModal(discord.ui.Modal, title="👕 Type d'article"):
    def __init__(self, s):
        super().__init__()
        self.sid = s["id"]
        self.t_in = discord.ui.TextInput(
            label="Type d'article (un par ligne, vide = tous)", style=discord.TextStyle.paragraph,
            required=False, max_length=300, default="\n".join(s["item_types"]),
            placeholder="doudoune\nmanteau")
        self.add_item(self.t_in)

    async def on_submit(self, interaction: discord.Interaction):
        db.update(self.sid, item_types=parse_list(self.t_in.value, commas=False))
        s = db.get(self.sid)
        await interaction.response.edit_message(embed=salon_embed(s), view=SalonView(s))


class ExcludeModal(discord.ui.Modal, title="🚫 Exclure des articles"):
    def __init__(self, s):
        super().__init__()
        self.sid = s["id"]
        self.ex_in = discord.ui.TextInput(
            label="Types d'articles à exclure (un par ligne)", style=discord.TextStyle.paragraph,
            required=False, max_length=500, default="\n".join(s["excluded"]),
            placeholder="t-shirt\nenfant\nlot\nfake")
        self.add_item(self.ex_in)

    async def on_submit(self, interaction: discord.Interaction):
        db.update(self.sid, excluded=parse_list(self.ex_in.value, commas=False))
        s = db.get(self.sid)
        await interaction.response.edit_message(embed=salon_embed(s), view=SalonView(s))


class PriceModal(discord.ui.Modal, title="💶 Fourchette de prix"):
    def __init__(self, s):
        super().__init__()
        self.sid = s["id"]
        self.min_in = discord.ui.TextInput(
            label="Prix minimum en € (vide = aucun)", required=False, max_length=10,
            default="" if s["price_min"] is None else f"{s['price_min']:g}", placeholder="Ex : 10")
        self.max_in = discord.ui.TextInput(
            label="Prix maximum en € (vide = aucun)", required=False, max_length=10,
            default="" if s["price_max"] is None else f"{s['price_max']:g}", placeholder="Ex : 40")
        self.add_item(self.min_in)
        self.add_item(self.max_in)

    async def on_submit(self, interaction: discord.Interaction):
        db.update(self.sid, price_min=parse_price(self.min_in.value), price_max=parse_price(self.max_in.value))
        s = db.get(self.sid)
        await interaction.response.edit_message(embed=salon_embed(s), view=SalonView(s))


class BrandsModal(discord.ui.Modal, title="🏷️ Marques & tailles"):
    def __init__(self, s):
        super().__init__()
        self.sid = s["id"]
        self.b_in = discord.ui.TextInput(
            label="Marques (vide = toutes)", required=False, max_length=300,
            default=", ".join(s["brands"]), placeholder="Ex : nike, adidas")
        self.s_in = discord.ui.TextInput(
            label="Tailles (vide = toutes)", required=False, max_length=300,
            default=", ".join(s["sizes"]), placeholder="Ex : m, l, 43, 44")
        self.add_item(self.b_in)
        self.add_item(self.s_in)

    async def on_submit(self, interaction: discord.Interaction):
        db.update(self.sid, brands=parse_list(self.b_in.value), sizes=parse_list(self.s_in.value))
        s = db.get(self.sid)
        await interaction.response.edit_message(embed=salon_embed(s), view=SalonView(s))


# ───────────────────────── Panneau public ─────────────────────────
def panel_embed(guild: discord.Guild):
    cfg = db.get_cfg(guild.id)

    def role(rid):
        r = guild.get_role(rid) if rid else None
        return r.mention if r else "*(non configuré)*"

    e = discord.Embed(title="🛒 Alertes Vinted personnalisées", color=COLOR)
    e.description = (
        "Reçois les nouvelles annonces Vinted **en quelques secondes**, dans **ton propre salon privé**.\n\n"
        "**Comment ça marche ?**\n"
        "**1️⃣** Clique sur le bouton ci-dessous (ou utilise `/vip` / `/premium`)\n"
        "**2️⃣** Clique sur **➕ Nouveau salon** et donne un nom + des mots-clés\n"
        "   *Ex : « Nike Tech » → `nike tech`*\n"
        "**3️⃣** Affine avec le prix, la marque, la taille, l'état, le pays\n"
        "**4️⃣** C'est tout ! Les annonces arrivent avec photos, infos et bouton **Acheter**\n\n"
        "**Accès :**\n"
        f"⭐ {role(cfg['vip_role_id'])} → jusqu'à **{cfg['vip_limit']}** salons (`/vip`)\n"
        f"💎 {role(cfg['premium_role_id'])} → salons **illimités** (`/premium`)\n\n"
        "*Seul toi (et les admins) pouvez voir tes salons.*"
    )
    return e


class PanelView(discord.ui.View):
    """Le panneau permanent posté depuis /setup."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="📂 Gérer mes salons Vinted", style=discord.ButtonStyle.primary,
                       custom_id="vinted:open")
    async def open(self, interaction: discord.Interaction, _):
        tier, _limit = access(interaction.user)
        if tier is None:
            cfg = db.get_cfg(interaction.guild_id)
            roles = [f"<@&{cfg[k]}>" for k in ("vip_role_id", "premium_role_id") if cfg[k]]
            txt = "❌ Cette fonction est réservée aux membres " + (" / ".join(roles) if roles else "VIP / Premium+") + "."
            return await interaction.response.send_message(txt, ephemeral=True)
        e, v = main_payload(interaction.user, interaction.guild_id)
        await interaction.response.send_message(embed=e, view=v, ephemeral=True)


# ───────────────────────── /setup : configuration (owner) ─────────────────────────
def setup_payload(guild: discord.Guild):
    cfg = db.get_cfg(guild.id)

    def role(rid, empty):
        r = guild.get_role(rid) if rid else None
        return r.mention if r else empty

    e = discord.Embed(title="⚙️ Configuration du bot Vinted", color=COLOR)
    e.description = (
        "Choisis les rôles ci-dessous. Les changements sont **immédiats**.\n\n"
        f"👑 **Owner** : {role(cfg['owner_role_id'], '*admins du serveur uniquement*')}\n"
        "   → salons **illimités**, accès au `/setup`\n\n"
        f"⭐ **VIP** : {role(cfg['vip_role_id'], '*non configuré*')}\n"
        f"   → commande `/vip` • limite de **{cfg['vip_limit']}** salons\n\n"
        f"💎 **Premium+** : {role(cfg['premium_role_id'], '*non configuré*')}\n"
        "   → commande `/premium` • salons **illimités**\n\n"
        "*Les administrateurs du serveur sont toujours considérés comme Owner.*"
    )
    return e, SetupView(guild)


class RoleCfgSelect(discord.ui.RoleSelect):
    def __init__(self, field, placeholder, current_role, row):
        kw = {"default_values": [current_role]} if current_role else {}
        super().__init__(placeholder=placeholder, min_values=0, max_values=1, row=row, **kw)
        self.field = field

    async def callback(self, interaction: discord.Interaction):
        role = self.values[0] if self.values else None
        db.set_cfg(interaction.guild_id, **{self.field: role.id if role else None})
        e, v = setup_payload(interaction.guild)
        await interaction.response.edit_message(embed=e, view=v)


class LimitModal(discord.ui.Modal, title="🔢 Limite de salons VIP"):
    def __init__(self, cfg):
        super().__init__()
        self.inp = discord.ui.TextInput(label="Nombre max de salons par VIP (1 à 200)", max_length=3,
                                        default=str(cfg["vip_limit"]), placeholder="Ex : 5")
        self.add_item(self.inp)

    async def on_submit(self, interaction: discord.Interaction):
        if not is_owner(interaction.user):
            return await interaction.response.send_message("❌ Réservé à l'owner.", ephemeral=True)
        try:
            n = int(self.inp.value.strip())
        except ValueError:
            n = None
        if n is None or not 1 <= n <= 200:
            return await interaction.response.send_message("❌ Entre un nombre entre 1 et 200.", ephemeral=True)
        db.set_cfg(interaction.guild_id, vip_limit=n)
        e, v = setup_payload(interaction.guild)
        await interaction.response.edit_message(embed=e, view=v)


class SetupView(discord.ui.View):
    def __init__(self, guild: discord.Guild):
        super().__init__(timeout=600)
        cfg = db.get_cfg(guild.id)
        self.add_item(RoleCfgSelect("owner_role_id", "👑 Rôle Owner (illimité)",
                                    guild.get_role(cfg["owner_role_id"]) if cfg["owner_role_id"] else None, 0))
        self.add_item(RoleCfgSelect("vip_role_id", "⭐ Rôle VIP (avec limite)",
                                    guild.get_role(cfg["vip_role_id"]) if cfg["vip_role_id"] else None, 1))
        self.add_item(RoleCfgSelect("premium_role_id", "💎 Rôle Premium+ (sans limite)",
                                    guild.get_role(cfg["premium_role_id"]) if cfg["premium_role_id"] else None, 2))
        self.limit.label = f"🔢 Limite VIP : {cfg['vip_limit']}"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not is_owner(interaction.user):
            await interaction.response.send_message("❌ Réservé à l'owner.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="🔢 Limite VIP", style=discord.ButtonStyle.primary, row=3)
    async def limit(self, interaction: discord.Interaction, _):
        await interaction.response.send_modal(LimitModal(db.get_cfg(interaction.guild_id)))

    @discord.ui.button(label="📌 Poster le panneau ici", style=discord.ButtonStyle.success, row=3)
    async def post_panel(self, interaction: discord.Interaction, _):
        try:
            await interaction.channel.send(embed=panel_embed(interaction.guild), view=PanelView())
        except (discord.Forbidden, discord.HTTPException):
            return await interaction.response.send_message(
                "❌ Je ne peux pas écrire dans ce salon (permission manquante).", ephemeral=True)
        await interaction.response.send_message("✅ Panneau posté !", ephemeral=True)


# ───────────────────────── Bot ─────────────────────────
class VintedBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.vinted = VintedClient()
        self.seen: dict = {}
        self.sent: dict = {}
        self.web_session: aiohttp.ClientSession | None = None
        self.sem = asyncio.Semaphore(MAX_CONCURRENCY)
        self.dcache: dict = {}
        self.backfilling: set = set()
        self.maxid: dict = {}
        self.watchers: dict = {}
        self.groups: dict = {}
        self.id_hist = collections.deque(maxlen=4000)  # (heure, plus grand id Vinted vu) -> « horloge » des ids
        self.gmax = 0
        self.rejects = collections.Counter()
        self.scans = self.scans_ok = self.total_sent = 0
        self.last_hb = 0.0

    async def setup_hook(self):
        self.web_session = aiohttp.ClientSession()
        self.add_view(PanelView())
        if GUILD_ID:
            g = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=g)
            await self.tree.sync(guild=g)
        else:
            await self.tree.sync()
        self.loop.create_task(self.poll_loop())

    async def close(self):
        for t in self.watchers.values():
            t.cancel()
        await self.vinted.close()
        if self.web_session:
            await self.web_session.close()
        await super().close()

    async def on_ready(self):
        log.info("Connecté en tant que %s", self.user)

    # --- surveillance continue : 1 « veilleur » par ligne de mots-clés ---
    def build_groups(self) -> dict:
        """(pays, recherche) -> salons concernés. Une même recherche partagée par plusieurs salons n'est faite qu'une fois."""
        groups: dict = {}
        for s in db.active():
            for q in salon_queries(s):
                groups.setdefault((s["country"], q), []).append(s)
        return groups

    async def poll_loop(self):
        """Superviseur : lance un veilleur par recherche, arrête ceux devenus inutiles (salon supprimé/modifié)."""
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                self.groups = self.build_groups()
                for key in self.groups:
                    t = self.watchers.get(key)
                    if t is None or t.done():
                        self.watchers[key] = asyncio.create_task(self.watch(key))
                for key in [k for k in self.watchers if k not in self.groups]:
                    self.watchers.pop(key).cancel()
                    self.seen.pop(key, None)
                    self.maxid.pop(key, None)
                if time.time() - self.last_hb > 60:
                    self.last_hb = time.time()
                    log.info("❤️ %s mot(s)-clé(s) surveillé(s) en continu | %s/%s requêtes OK | %s annonces envoyées",
                             len(self.watchers), self.scans_ok, self.scans, self.total_sent)
                    if self.rejects:
                        log.info("Filtres : annonces écartées depuis 1 min → %s", dict(self.rejects))
                        self.rejects.clear()
                    self.scans = self.scans_ok = 0
            except Exception:
                log.exception("Erreur superviseur")
            await asyncio.sleep(2)

    async def watch(self, key):
        """Veilleur d'UNE recherche : relit sans arrêt la même recherche Vinted (plus récent en premier)."""
        interval = lambda: max(POLL_INTERVAL, len(self.groups) * MIN_REQ_GAP)
        await asyncio.sleep(random.uniform(0, interval()))  # étale les veilleurs dans le temps
        while True:
            t0 = time.monotonic()
            try:
                await self.scan(key)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Erreur veilleur %s", key)
            await asyncio.sleep(max(0.3, interval() - (time.monotonic() - t0)) + random.uniform(0, 0.4))

    # --- fraîcheur des annonces ---
    # Les ids Vinted augmentent avec le temps. On note régulièrement le plus grand id vu (tous mots-clés confondus) :
    # une annonce n'a que X secondes si son id est plus grand que le plus grand id qui existait il y a X secondes.
    def note_ids(self, ids):
        if not ids:
            return
        self.gmax = max(self.gmax, max(ids))
        now = time.time()
        if not self.id_hist or now - self.id_hist[-1][0] >= 1.5:
            self.id_hist.append((now, self.gmax))

    def id_floor(self, max_age):
        """Plus petit id exclu : toute annonce avec un id <= floor est plus vieille que max_age (0 = marge par défaut)."""
        if not self.id_hist:
            return None
        if not max_age:
            return self.gmax - NEW_ID_MARGIN
        cutoff = time.time() - max_age
        for t, m in reversed(self.id_hist):
            if t <= cutoff:
                return m
        return self.id_hist[0][1]  # historique plus court que la durée : seules les annonces apparues depuis le démarrage

    def is_recent(self, it, max_age):
        """Vrai si l'annonce est assez récente pour la durée choisie dans le salon."""
        if max_age and it.get("ts") and time.time() - it["ts"] > max(max_age, 600):
            return False  # l'heure de la photo dit clairement « vieux » (marge : la photo est postée avant l'annonce)
        floor = self.id_floor(max_age)
        return floor is None or it["id"] > floor

    async def scan(self, key):
        """Une lecture de la recherche : mémorise la 1re fois, ensuite envoie uniquement les NOUVELLES annonces."""
        country, query = key
        items = await self._search(country, query, 1, POLL_PER_PAGE)
        self.scans += 1
        if items is None:
            return
        self.scans_ok += 1
        ids = {i["id"] for i in items}
        seen = self.seen.get(key)
        if seen is None and not ids:
            return  # recherche vide : on réessaie, pour ne pas tout considérer comme « nouveau » ensuite
        if seen is None:  # première lecture : on mémorise l'existant, sans rien envoyer
            self.seen[key] = ids
            self.maxid[key] = max(ids) if ids else 0
            self.note_ids(ids)
            log.info("Veille démarrée « %s » (%s) : %s annonces existantes ignorées, j'envoie uniquement les suivantes",
                     query, country, len(ids))
            return
        # « nouvelle » = jamais vue ET id plus grand que tout ce qui existait au démarrage
        base = self.maxid.get(key, 0) - BASELINE_MARGIN
        new = sorted((i for i in items if i["id"] not in seen and i["id"] > base), key=lambda i: i["id"])
        self.seen[key] = ids if len(seen) > 5000 else (seen | ids)
        self.note_ids(ids)
        tasks = []
        for it in new:
            for s in self.groups.get(key, []):
                k = (s["id"], it["id"])
                if k in self.sent or not self.is_recent(it, s.get("max_age")):
                    continue
                why = filter_reason(s, it)
                if why:
                    self.rejects[why] += 1
                    continue
                self.sent[k] = None
                tasks.append(self.process(s, query, it))
        while len(self.sent) > 20000:
            self.sent.pop(next(iter(self.sent)))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _search(self, domain, kw, page=1, per_page=PER_PAGE):
        async with self.sem:
            return await self.vinted.search(domain, kw, page, per_page)

    async def item_ts(self, it):
        """Heure de mise en ligne : celle de la recherche, sinon lue dans la fiche de l'annonce."""
        if it["ts"]:
            return it["ts"]
        ts = ts_from_details(await self.get_details(it))
        if ts:
            it["ts"] = ts
        return ts

    async def _search_all(self, domain, kw, max_age=0):
        """Toutes les pages de résultats (du plus récent au plus ancien), jusqu'à la fin des résultats
        ou jusqu'à dépasser l'âge max choisi. Retourne None si Vinted ne répond pas du tout."""
        out, now = [], time.time()
        for page in range(1, BACKFILL_MAX_PAGES + 1):
            items = await self._search(domain, kw, page)
            if items is None:
                return out or None
            out.extend(items)
            if len(items) < PER_PAGE:
                break  # dernière page
            if max_age:
                self.note_ids([i["id"] for i in items])
                floor = self.id_floor(max_age)
                if floor is not None and min(i["id"] for i in items) <= floor:
                    break  # on est déjà remonté plus loin que la durée choisie
        return out

    async def get_details(self, it):
        """Détails d'une annonce (description…), avec cache pour ne pas les redemander."""
        cache = self.dcache
        if it["id"] in cache:
            return cache[it["id"]]
        async with self.sem:
            extra = await self.vinted.details(it["domain"], it["id"])
        if extra:
            cache[it["id"]] = extra
            while len(cache) > 2000:
                cache.pop(next(iter(cache)))
        return extra

    async def check(self, query, it):
        """Tous les mots de la recherche doivent être dans le titre/la marque OU dans la description.
        Retourne (ok, détails)."""
        hay = f"{norm(it['title'])} {norm(it['brand'])}"
        extra = None
        if not words_in(hay, query):
            if not CHECK_DESCRIPTION:
                return False, None
            extra = await self.get_details(it)
            if not extra:
                return False, None  # description introuvable : on ne peut pas confirmer, on n'envoie pas
            if not words_in(f"{hay} {norm(extra.get('description'))}", query):
                return False, None
        elif FETCH_DETAILS:
            extra = await self.get_details(it)
        return True, extra

    async def process(self, s, query, it):
        ok, extra = await self.check(query, it)
        if ok:
            await self.deliver(s, it, extra)

    async def clear_channel(self, channel) -> int:
        """Supprime tous les messages du salon (sauf les messages épinglés)."""
        deleted = await channel.purge(limit=None, check=lambda m: not m.pinned)
        return len(deleted)

    async def backfill(self, sid, clear=True):
        """Comme si on tapait les mots dans la barre de recherche Vinted : lance la recherche avec les
        réglages actuels du salon (dont l'âge max) et envoie toutes les annonces qui correspondent.
        Retourne le nombre d'annonces envoyées, ou None si Vinted n'a pas répondu (rien n'est supprimé)."""
        s = db.get(sid)
        if not s:
            return 0
        self.backfilling.add(sid)
        try:
            queries = salon_queries(s)
            if not queries:
                return None
            results = await asyncio.gather(*(self._search_all(s["country"], q, s.get("max_age") or 0) for q in queries))
            if all(r is None for r in results):
                return None
            cands = {}
            for q, items in zip(queries, results):
                for it in items or []:
                    if it["id"] not in cands and basic_ok(s, it):
                        cands[it["id"]] = (q, it)
            newest = sorted(cands.values(), key=lambda c: c[1]["id"], reverse=True)
            for _, items in zip(queries, results):
                self.note_ids([i["id"] for i in items or []])
            if s.get("max_age"):
                newest = [c for c in newest if self.is_recent(c[1], s["max_age"])]
            checks = await asyncio.gather(*(self.check(q, it) for q, it in newest))
            keep = [(it, extra) for (q, it), (ok, extra) in zip(newest, checks) if ok]
            if clear:
                ch = self.get_channel(s["channel_id"])
                if ch:
                    await self.clear_channel(ch)
            for k in [k for k in self.sent if k[0] == sid]:
                del self.sent[k]
            for it, extra in reversed(keep):  # le plus ancien d'abord -> le plus récent en bas du salon
                self.sent[(sid, it["id"])] = None
                await self.deliver(s, it, extra)
            return len(keep)
        finally:
            self.backfilling.discard(sid)

    async def deliver(self, s, it, extra=None):
        embeds, view = build_message(s, it, extra)
        wh = discord.Webhook.from_url(s["webhook_url"], session=self.web_session, client=self)
        try:
            try:
                await wh.send(embeds=embeds, view=view, username="Vinted Bot")
            except discord.NotFound:
                raise
            except Exception:
                await wh.send(embeds=embeds, username="Vinted Bot")  # sans boutons si non supporté
            self.total_sent += 1
            log.info("→ %s : %s (%s)", s["name"], it["title"], it["url"])
        except discord.NotFound:
            log.warning("Webhook introuvable pour le salon %s, désactivé.", s["name"])
            db.update(s["id"], active=0)
        except Exception as e:
            log.warning("Envoi échoué: %s", e)


bot = VintedBot()


# ───────────────────────── Commandes ─────────────────────────
@bot.tree.command(name="setup", description="Configuration du bot : rôles VIP / Premium+, limite (owner)")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def setup(interaction: discord.Interaction):
    if not is_owner(interaction.user):
        return await interaction.response.send_message("❌ Commande réservée à l'owner.", ephemeral=True)
    e, v = setup_payload(interaction.guild)
    await interaction.response.send_message(embed=e, view=v, ephemeral=True)


@bot.tree.command(name="vip", description="Gère tes salons Vinted (rôle VIP, nombre de salons limité)")
@app_commands.guild_only()
async def vip_cmd(interaction: discord.Interaction):
    cfg = db.get_cfg(interaction.guild_id)
    if not cfg["vip_role_id"]:
        return await interaction.response.send_message(
            "⚠️ Le rôle VIP n'est pas encore configuré. Un owner doit faire `/setup`.", ephemeral=True)
    if not (is_owner(interaction.user) or has_role(interaction.user, cfg["vip_role_id"])):
        return await interaction.response.send_message(
            f"❌ Cette commande est réservée au rôle <@&{cfg['vip_role_id']}>.", ephemeral=True)
    e, v = main_payload(interaction.user, interaction.guild_id)
    await interaction.response.send_message(embed=e, view=v, ephemeral=True)


@bot.tree.command(name="premium", description="Gère tes salons Vinted (rôle Premium+, salons illimités)")
@app_commands.guild_only()
async def premium_cmd(interaction: discord.Interaction):
    cfg = db.get_cfg(interaction.guild_id)
    if not cfg["premium_role_id"]:
        return await interaction.response.send_message(
            "⚠️ Le rôle Premium+ n'est pas encore configuré. Un owner doit faire `/setup`.", ephemeral=True)
    if not (is_owner(interaction.user) or has_role(interaction.user, cfg["premium_role_id"])):
        return await interaction.response.send_message(
            f"❌ Cette commande est réservée au rôle <@&{cfg['premium_role_id']}>.", ephemeral=True)
    e, v = main_payload(interaction.user, interaction.guild_id)
    await interaction.response.send_message(embed=e, view=v, ephemeral=True)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN manquant : remplis le fichier .env")
    bot.run(TOKEN)
