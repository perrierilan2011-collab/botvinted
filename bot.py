"""
Bot Vinted -> Discord
- /setup (admin) : poste le panneau dans un salon
- Chaque membre crée ses "salons" privés (ex : Nike Tech, Running...) avec ses propres filtres
- Chaque salon a son webhook, les annonces arrivent avec photos, infos et bouton Acheter
"""
import asyncio
import json
import logging
import os
import re
import sqlite3
import time
import unicodedata
from datetime import datetime, timezone

import aiohttp
import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID") or 0)
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "3"))
PROXY = os.getenv("PROXY") or None
MAX_SALONS = min(int(os.getenv("MAX_SALONS_PER_USER", "5")), 25)
CATEGORY_NAME = os.getenv("CATEGORY_NAME", "🛒 Vinted")
DB_PATH = os.getenv("DB_PATH", "vinted_bot.db")
FETCH_DETAILS = os.getenv("FETCH_DETAILS", "0") == "1"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("vinted")

COLOR = 0x09B1BA
CONDITIONS = ["Neuf avec étiquette", "Neuf sans étiquette", "Très bon état", "Bon état", "Satisfaisant"]
COUNTRIES = {
    "fr": "🇫🇷 France", "be": "🇧🇪 Belgique", "de": "🇩🇪 Allemagne", "es": "🇪🇸 Espagne",
    "it": "🇮🇹 Italie", "nl": "🇳🇱 Pays-Bas", "pl": "🇵🇱 Pologne", "pt": "🇵🇹 Portugal",
    "lu": "🇱🇺 Luxembourg", "at": "🇦🇹 Autriche", "cz": "🇨🇿 Tchéquie", "lt": "🇱🇹 Lituanie",
}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


# ───────────────────────── Utilitaires ─────────────────────────
def norm(text: str) -> str:
    text = unicodedata.normalize("NFD", str(text or "").lower())
    return "".join(c for c in text if unicodedata.category(c) != "Mn").strip()


def parse_list(text: str) -> list:
    out = []
    for part in re.split(r"[,\n;]+", text or ""):
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


def fmt_list(values) -> str:
    return ", ".join(values) if values else "— (tout accepter)"


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", norm(text)).strip("-")[:60] or "salon"


# ───────────────────────── Base de données ─────────────────────────
LIST_FIELDS = ("keywords", "excluded", "brands", "sizes", "conditions")


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
        self.c.commit()

    @staticmethod
    def _row(row):
        if row is None:
            return None
        d = dict(row)
        for f in LIST_FIELDS:
            d[f] = json.loads(d[f] or "[]")
        return d

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
    user = it.get("user") or {}
    url = it.get("url") or it.get("path") or f"/items/{it['id']}"
    if url.startswith("/"):
        url = f"https://www.vinted.{domain}{url}"
    return {
        "id": int(it["id"]), "title": it.get("title") or "", "price": amount, "currency": cur,
        "brand": it.get("brand_title") or "", "size": it.get("size_title") or "",
        "status": it.get("status") or "", "url": url, "photo": purl, "ts": ts,
        "seller": user.get("login"), "seller_url": user.get("profile_url"), "domain": domain,
    }


ENDPOINTS = [
    "https://www.vinted.{d}/svc-catalogue/items",
    "https://api.www.vinted.{d}/svc-catalogue/items",
    "https://www.vinted.{d}/api/v2/catalog/items",
]


class VintedClient:
    def __init__(self):
        self.ep: dict = {}
        self._lastlog: dict = {}
        self.sessions: dict = {}
        self.locks: dict = {}
        self.cooldown: dict = {}

    def _headers(self, domain):
        return {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
                "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8", "Referer": f"https://www.vinted.{domain}/"}

    async def _session(self, domain) -> aiohttp.ClientSession:
        if domain not in self.sessions:
            self.sessions[domain] = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar())
            self.locks[domain] = asyncio.Lock()
            await self.refresh(domain)
        return self.sessions[domain]

    async def refresh(self, domain):
        """Récupère les cookies de session nécessaires à l'API."""
        lock = self.locks.setdefault(domain, asyncio.Lock())
        async with lock:
            sess = self.sessions[domain]
            try:
                async with sess.get(f"https://www.vinted.{domain}/", headers=self._headers(domain),
                                    proxy=PROXY, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    await r.read()
                    log.info("Cookies Vinted.%s rafraîchis (%s)", domain, r.status)
            except Exception as e:
                log.warning("Refresh cookies %s échoué: %s", domain, e)

    def _warn(self, key, msg, *args):
        """Évite de spammer les logs : 1 message identique toutes les 30 s."""
        now = time.time()
        if now - self._lastlog.get(key, 0) > 30:
            self._lastlog[key] = now
            log.warning(msg, *args)

    async def search(self, domain, text):
        if time.time() < self.cooldown.get(domain, 0):
            return None
        sess = await self._session(domain)
        params = {"search_text": text, "order": "newest_first", "per_page": "40", "page": "1"}
        order = [self.ep[domain]] if domain in self.ep else range(len(ENDPOINTS))
        refreshed = False
        for idx in order:
            url = ENDPOINTS[idx].format(d=domain)
            for _ in range(2):
                try:
                    async with sess.get(url, params=params, headers=self._headers(domain), proxy=PROXY,
                                        timeout=aiohttp.ClientTimeout(total=8)) as r:
                        if r.status in (401, 403) and not refreshed:
                            refreshed = True
                            await self.refresh(domain)
                            continue
                        if r.status == 404:
                            self.ep.pop(domain, None)
                            self._warn((domain, idx, 404), "404 sur %s (on essaie une autre adresse)", url)
                            break
                        if r.status == 429:
                            self.cooldown[domain] = time.time() + 30
                            log.warning("Rate limit sur vinted.%s, pause 30s (monte POLL_INTERVAL ou utilise un proxy)", domain)
                            return None
                        if r.status != 200:
                            self._warn((domain, r.status), "Vinted.%s a répondu HTTP %s (%s)%s", domain, r.status, url,
                                       " : IP probablement bloquée, utilise un proxy" if r.status in (401, 403) else "")
                            return None
                        data = await r.json(content_type=None)
                        raw = extract_items(data)
                        if raw is None:
                            self._warn((domain, "fmt"), "Réponse inattendue de %s, clés : %s", url,
                                       list(data.keys()) if isinstance(data, dict) else type(data))
                            return None
                        if domain not in self.ep:
                            self.ep[domain] = idx
                            sample = raw[0] if raw and isinstance(raw[0], dict) else {}
                            log.info("✅ API Vinted.%s OK via %s | clés d'une annonce : %s", domain, url, sorted(sample.keys()))
                        return [norm_item(i, domain) for i in raw if isinstance(i, dict) and i.get("id")]
                except Exception as e:
                    self._warn((domain, "exc"), "Erreur requête Vinted « %s » : %s", text, e)
                    break
        return None

    async def details(self, domain, item_id):
        try:
            sess = await self._session(domain)
            async with sess.get(f"https://www.vinted.{domain}/api/v2/items/{item_id}",
                                headers=self._headers(domain), proxy=PROXY,
                                timeout=aiohttp.ClientTimeout(total=2.5)) as r:
                if r.status != 200:
                    return None
                return (await r.json(content_type=None)).get("item")
        except Exception:
            return None

    async def close(self):
        for s in self.sessions.values():
            await s.close()


# ───────────────────────── Filtres ─────────────────────────
def matches(s: dict, kw: str, it: dict) -> bool:
    title = norm(it["title"])
    hay = f"{title} {norm(it['brand'])}"
    if not all(w in hay for w in norm(kw).split()):
        return False
    if any(x in title for x in s["excluded"]):
        return False
    if it["price"] is not None:
        if s["price_min"] is not None and it["price"] < s["price_min"]:
            return False
        if s["price_max"] is not None and it["price"] > s["price_max"]:
            return False
    if s["brands"] and not any(b in norm(it["brand"]) or b in title for b in s["brands"]):
        return False
    if s["sizes"]:
        size = norm(it["size"])
        tokens = set(re.split(r"[\s/,\-]+", size))
        if not any(w == size or w in tokens for w in s["sizes"]):
            return False
    if s["conditions"] and it["status"]:
        if norm(it["status"]) not in {norm(c) for c in s["conditions"]}:
            return False
    return True


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
def main_payload(user, guild_id):
    salons = db.by_user(user.id, guild_id)
    e = discord.Embed(title="📂 Tes salons Vinted", color=COLOR)
    e.description = (
        "Un **salon** = une recherche avec son propre salon privé.\n"
        "Exemple : un salon **Nike Tech**, un autre **Running**, etc. Rien n'est mélangé !\n\n"
        + ("**Tes salons :**\n" + "\n".join(
            f"{'🟢' if s['active'] else '⏸️'} **{s['name']}** → <#{s['channel_id']}>" for s in salons)
           if salons else "Tu n'as pas encore de salon. Clique sur **➕ Nouveau salon** pour commencer.")
        + f"\n\n*Tu peux avoir jusqu'à {MAX_SALONS} salons.*"
    )
    return e, MainView(user, guild_id, salons)


class MainView(discord.ui.View):
    def __init__(self, user, guild_id, salons):
        super().__init__(timeout=900)
        self.user, self.guild_id = user, guild_id
        if salons:
            self.add_item(SalonSelect(salons))
        self.add_item(NewButton())


class SalonSelect(discord.ui.Select):
    def __init__(self, salons):
        opts = [discord.SelectOption(label=s["name"][:100], value=str(s["id"]),
                                     description=(", ".join(s["keywords"]) or "aucun mot-clé")[:100])
                for s in salons]
        super().__init__(placeholder="✏️ Choisir un salon à modifier…", options=opts, row=0)

    async def callback(self, interaction: discord.Interaction):
        s = db.get(int(self.values[0]))
        await interaction.response.edit_message(embed=salon_embed(s), view=SalonView(s))


class NewButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="➕ Nouveau salon", style=discord.ButtonStyle.success, row=1)

    async def callback(self, interaction: discord.Interaction):
        if len(db.by_user(interaction.user.id, interaction.guild_id)) >= MAX_SALONS:
            return await interaction.response.send_message(
                f"❌ Tu as atteint la limite de {MAX_SALONS} salons. Supprime-en un d'abord.", ephemeral=True)
        await interaction.response.send_modal(NewSalonModal())


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
        self.kw_in = discord.ui.TextInput(label="Mots-clés (sépare par des virgules)", max_length=300,
                                          placeholder="Ex : nike tech, nike tech fleece")
        self.add_item(self.name_in)
        self.add_item(self.kw_in)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        guild, member = interaction.guild, interaction.user
        try:
            cat = await find_category(guild)
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                member: discord.PermissionOverwrite(view_channel=True, read_message_history=True,
                                                    send_messages=False),
                guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True,
                                                      manage_webhooks=True, manage_channels=True),
            }
            channel = await guild.create_text_channel(
                f"{slugify(self.name_in.value)}-{slugify(member.name)}"[:95], category=cat,
                overwrites=overwrites, topic=f"Alertes Vinted de {member} • {self.name_in.value}")
            wh = await channel.create_webhook(name="Vinted Bot")
        except discord.Forbidden:
            return await interaction.followup.send(
                "❌ Il me manque des permissions : **Gérer les salons** et **Gérer les webhooks**.", ephemeral=True)
        sid = db.add(user_id=member.id, guild_id=guild.id, name=self.name_in.value.strip(),
                     channel_id=channel.id, webhook_url=wh.url, keywords=parse_list(self.kw_in.value))
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
                       "Les changements sont pris en compte **immédiatement**.")
    pmin = f"{s['price_min']:g} €" if s["price_min"] is not None else "—"
    pmax = f"{s['price_max']:g} €" if s["price_max"] is not None else "—"
    e.add_field(name="🔎 Mots-clés", value=fmt_list(s["keywords"]), inline=False)
    e.add_field(name="🚫 Mots exclus", value=", ".join(s["excluded"]) or "—", inline=False)
    e.add_field(name="💶 Prix", value=f"min {pmin} • max {pmax}", inline=False)
    e.add_field(name="🏷️ Marques", value=fmt_list(s["brands"]), inline=True)
    e.add_field(name="📏 Tailles", value=fmt_list(s["sizes"]), inline=True)
    e.add_field(name="✨ États", value=fmt_list(s["conditions"]), inline=True)
    e.add_field(name="🌍 Pays", value=COUNTRIES.get(s["country"], s["country"]), inline=True)
    e.set_footer(text="💡 Astuce : plusieurs mots-clés = plusieurs recherches. "
                      "« nike tech, nike trail » te montre les deux.")
    return e


class SalonView(discord.ui.View):
    def __init__(self, s: dict):
        super().__init__(timeout=900)
        self.sid, self.owner = s["id"], s["user_id"]
        self.toggle.label = "⏸️ Mettre en pause" if s["active"] else "▶️ Réactiver"
        self.toggle.style = discord.ButtonStyle.secondary if s["active"] else discord.ButtonStyle.success
        self.add_item(CondSelect(s))
        self.add_item(CountrySelect(s))

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

    @discord.ui.button(label="⏸️", row=3)
    async def toggle(self, interaction: discord.Interaction, _):
        s = db.get(self.sid)
        db.update(self.sid, active=0 if s["active"] else 1)
        await self._refresh(interaction)

    @discord.ui.button(label="🗑️ Supprimer", style=discord.ButtonStyle.danger, row=3)
    async def delete(self, interaction: discord.Interaction, _):
        v = ConfirmDelete(self.sid, self.owner)
        e = discord.Embed(title="🗑️ Supprimer ce salon ?", color=0xE74C3C,
                          description="Le salon Discord et ses réglages seront **définitivement supprimés**.")
        await interaction.response.edit_message(embed=e, view=v)

    @discord.ui.button(label="⬅️ Retour", row=3)
    async def back(self, interaction: discord.Interaction, _):
        e, v = main_payload(interaction.user, interaction.guild_id)
        await interaction.response.edit_message(embed=e, view=v)


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


class KeywordsModal(discord.ui.Modal, title="✏️ Nom & mots-clés"):
    def __init__(self, s):
        super().__init__()
        self.sid, self.old_name = s["id"], s["name"]
        self.channel_id = s["channel_id"]
        self.name_in = discord.ui.TextInput(label="Nom du salon", max_length=50, default=s["name"])
        self.kw_in = discord.ui.TextInput(
            label="Mots-clés (séparés par des virgules)", style=discord.TextStyle.paragraph, max_length=500,
            default=", ".join(s["keywords"]), placeholder="nike tech, nike trail, nike running")
        self.ex_in = discord.ui.TextInput(
            label="Mots à exclure (optionnel)", style=discord.TextStyle.paragraph, required=False,
            max_length=500, default=", ".join(s["excluded"]), placeholder="enfant, faux, lot, fake")
        for i in (self.name_in, self.kw_in, self.ex_in):
            self.add_item(i)

    async def on_submit(self, interaction: discord.Interaction):
        name = self.name_in.value.strip()
        db.update(self.sid, name=name, keywords=parse_list(self.kw_in.value),
                  excluded=parse_list(self.ex_in.value))
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


class PanelView(discord.ui.View):
    """Le panneau permanent posté par /setup."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="📂 Gérer mes salons Vinted", style=discord.ButtonStyle.primary,
                       custom_id="vinted:open")
    async def open(self, interaction: discord.Interaction, _):
        e, v = main_payload(interaction.user, interaction.guild_id)
        await interaction.response.send_message(embed=e, view=v, ephemeral=True)


# ───────────────────────── Bot ─────────────────────────
class VintedBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.vinted = VintedClient()
        self.seen: dict = {}
        self.sent: dict = {}
        self.web_session: aiohttp.ClientSession | None = None
        self.sem = asyncio.Semaphore(12)

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
        await self.vinted.close()
        if self.web_session:
            await self.web_session.close()
        await super().close()

    async def on_ready(self):
        log.info("Connecté en tant que %s", self.user)

    # --- boucle de surveillance ---
    async def poll_loop(self):
        await self.wait_until_ready()
        while not self.is_closed():
            t0 = time.monotonic()
            try:
                await self.poll_once()
            except Exception:
                log.exception("Erreur dans la boucle")
            await asyncio.sleep(max(0.2, POLL_INTERVAL - (time.monotonic() - t0)))

    async def _search(self, domain, kw):
        async with self.sem:
            return await self.vinted.search(domain, kw)

    async def poll_once(self):
        groups: dict = {}
        for s in db.active():
            for kw in s["keywords"]:
                groups.setdefault((s["country"], kw), []).append(s)
        if not groups:
            return
        for k in list(self.seen):
            if k not in groups:
                del self.seen[k]
        results = await asyncio.gather(*(self._search(c, kw) for (c, kw) in groups))
        self.polls = getattr(self, "polls", 0) + 1
        if time.time() - getattr(self, "last_hb", 0) > 60:
            self.last_hb = time.time()
            ok = sum(1 for r in results if r is not None)
            log.info("❤️ Scan #%s : %s recherche(s) actives, %s réponse(s) OK, %s annonces envoyées au total",
                     self.polls, len(groups), ok, getattr(self, "total_sent", 0))
        tasks = []
        for (key, salons), items in zip(groups.items(), results):
            if items is None:
                continue
            ids = {i["id"] for i in items}
            seen = self.seen.get(key)
            if seen is None:           # première fois : on mémorise sans notifier
                self.seen[key] = ids
                continue
            new = sorted((i for i in items if i["id"] not in seen), key=lambda i: i["id"])
            self.seen[key] = ids if len(seen) > 5000 else (seen | ids)
            for it in new:
                for s in salons:
                    k = (s["id"], it["id"])
                    if k not in self.sent and matches(s, key[1], it):
                        self.sent[k] = None
                        tasks.append(self.deliver(s, it))
        while len(self.sent) > 20000:
            self.sent.pop(next(iter(self.sent)))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def deliver(self, s, it):
        extra = await self.vinted.details(it["domain"], it["id"]) if FETCH_DETAILS else None
        embeds, view = build_message(s, it, extra)
        wh = discord.Webhook.from_url(s["webhook_url"], session=self.web_session, client=self)
        try:
            try:
                await wh.send(embeds=embeds, view=view, username="Vinted Bot")
            except discord.NotFound:
                raise
            except Exception:
                await wh.send(embeds=embeds, username="Vinted Bot")   # sans boutons si non supporté
            self.total_sent = getattr(self, "total_sent", 0) + 1
            log.info("→ %s : %s (%s)", s["name"], it["title"], it["url"])
        except discord.NotFound:
            log.warning("Webhook introuvable pour le salon %s, désactivé.", s["name"])
            db.update(s["id"], active=0)
        except Exception as e:
            log.warning("Envoi échoué: %s", e)


bot = VintedBot()


@bot.tree.command(name="setup", description="Poste le panneau Vinted dans ce salon (admin)")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def setup(interaction: discord.Interaction):
    e = discord.Embed(title="🛒 Alertes Vinted personnalisées", color=COLOR)
    e.description = (
        "Reçois les nouvelles annonces Vinted **en quelques secondes**, dans **ton propre salon privé**.\n\n"
        "**Comment ça marche ?**\n"
        "**1️⃣** Clique sur le bouton ci-dessous\n"
        "**2️⃣** Clique sur **➕ Nouveau salon** et donne un nom + des mots-clés\n"
        "　　*Ex : « Nike Tech » → `nike tech`*\n"
        "**3️⃣** Affine avec le prix, la marque, la taille, l'état, le pays\n"
        "**4️⃣** C'est tout ! Les annonces arrivent avec photos, infos et bouton **Acheter**\n\n"
        "**Tu veux plusieurs recherches séparées ?**\n"
        "Crée plusieurs salons : un **Nike Tech**, un **Running**, un **Trail**… "
        "Chacun a ses propres réglages et rien n'est mélangé.\n\n"
        "*Seul toi (et les admins) pouvez voir tes salons.*"
    )
    await interaction.channel.send(embed=e, view=PanelView())
    await interaction.response.send_message("✅ Panneau posté !", ephemeral=True)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN manquant : remplis le fichier .env")
    bot.run(TOKEN)
