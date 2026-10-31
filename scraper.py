"""
Crunchyroll release-calendar dub tracker (Castellano) - vía API, sin navegador.

Usa la misma API que la página https://www.crunchyroll.com/es-es/releasecalendar:

  1. POST /auth/v1/token            (token anónimo, grant_type=client_id)
  2. GET  /content/v2/discover/browse?type=episode&sort_by=newly_added
          &from_date=<ms>&to_date=<ms>&locale=es-ES&n=100&start=<offset>

Cada episodio es UNA versión de audio y su id termina con el código de esa
versión (GE00384102JAJP, GE00389819PTBR, ...). Se avisa de los que terminan
en ESES (Castellano).

Uso:
    python scraper.py                # ejecución normal (GitHub Actions)
    python scraper.py --dry-run      # no escribe feed.xml ni history.json
    python scraper.py --file x.json  # prueba con una respuesta guardada de la API
"""

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import xml.etree.ElementTree as ET

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

HISTORY_FILE = "history.json"
RSS_FILE = "feed.xml"
CALENDAR_URL = "https://www.crunchyroll.com/es-es/releasecalendar"

BASE = "https://www.crunchyroll.com"
TOKEN_URL = f"{BASE}/auth/v1/token"
BROWSE_URL = f"{BASE}/content/v2/discover/browse"
BASIC_AUTH = "Basic Y3Jfd2ViOg=="          # cliente público "cr_web" que usa la web
LOCALE = "es-ES"
PAGE_SIZE = 100

LANG_KEY = "ESES"
LANG_NAME = "Castellano (ESES)"
NEW_SEASON_GAP_DAYS = 21
WEEKS_BACK = 1                             # semanas anteriores a revisar además de la actual

MADRID = ZoneInfo("Europe/Madrid")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
)
BASE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "es-ES,es;q=0.9",
    "Origin": BASE,
    "Referer": CALENDAR_URL,
}

CHANNEL_DESCRIPTION = "Avisos de nuevos animes doblados al Castellano (ESES)."
DATE_FIELDS = ("premium_available_date", "availability_starts", "episode_air_date",
               "free_available_date", "upload_date")


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def make_session():
    """Sesión con huella TLS de Chrome si curl_cffi está disponible."""
    try:
        from curl_cffi import requests as cffi_requests
        return cffi_requests.Session(impersonate="chrome")
    except ImportError:
        import requests
        return requests.Session()


def request_with_retry(session, method, url, attempts=3, **kwargs):
    last = None
    for attempt in range(1, attempts + 1):
        try:
            resp = session.request(method, url, timeout=30, **kwargs)
        except Exception as e:
            last = f"error de conexión: {e}"
        else:
            if resp.status_code == 200:
                return resp.json()
            last = f"HTTP {resp.status_code}: {resp.text[:300]!r}"
            with open("debug_response.txt", "w", encoding="utf-8") as f:
                f.write(f"{method} {url}\nHTTP {resp.status_code}\n\n{resp.text}")
        print(f"  ⚠️ Intento {attempt}/{attempts} fallido ({last}).")
        time.sleep(5 * attempt)
    raise RuntimeError(f"{method} {url} -> {last}")


def get_token(session):
    headers = {
        **BASE_HEADERS,
        "Authorization": BASIC_AUTH,
        "Content-Type": "application/x-www-form-urlencoded",
        "ETP-Anonymous-ID": str(uuid.uuid4()),
    }
    data = request_with_retry(session, "POST", TOKEN_URL, headers=headers,
                              data="grant_type=client_id")
    country = data.get("country", "?")
    print(f"🔑 Token anónimo obtenido (país detectado por Crunchyroll: {country}).")
    if country not in ("ES", "?"):
        print("  ⚠️ Crunchyroll no te ve en España: el catálogo puede no incluir doblajes ESES.")
    return data["access_token"]


def fetch_week(session, token, week_start):
    """Todos los episodios de la semana que empieza en week_start (lunes 00:00 Madrid)."""
    start_dt = datetime(week_start.year, week_start.month, week_start.day, tzinfo=MADRID)
    end_dt = start_dt + timedelta(days=7)
    params = {
        "type": "episode",
        "sort_by": "newly_added",
        "ratings": "true",
        "n": PAGE_SIZE,
        "from_date": int(start_dt.timestamp() * 1000),
        "to_date": int(end_dt.timestamp() * 1000),
        "locale": LOCALE,
    }
    headers = {**BASE_HEADERS, "Authorization": f"Bearer {token}"}

    items, offset = [], 0
    while True:
        page_params = dict(params, start=offset) if offset else params
        data = request_with_retry(session, "GET", BROWSE_URL, headers=headers, params=page_params)
        page = data.get("data", [])
        items.extend(page)
        total = data.get("total")
        offset += len(page)
        if len(page) < PAGE_SIZE or (total is not None and offset >= total) or offset > 2000:
            break
    print(f"🗓️ Semana del {week_start.isoformat()}: {len(items)} episodios.")
    return items


# ---------------------------------------------------------------------------
# Interpretación de la respuesta
# ---------------------------------------------------------------------------

def parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(MADRID)
    except ValueError:
        return None


def audio_code(item):
    """Código de audio: sufijo del id (GE...ESES) o episode_metadata.audio_locale."""
    item_id = item.get("id", "")
    for code in ("ES419", "ESES"):
        if item_id.upper().endswith(code):
            return code
    locale = (item.get("episode_metadata") or {}).get("audio_locale", "")
    if locale:
        return locale.replace("-", "").upper()        # es-ES -> ESES, es-419 -> ES419
    return item_id[-4:].upper()


def to_release(item):
    meta = item.get("episode_metadata") or {}
    release_dt = None
    for field in DATE_FIELDS:
        release_dt = parse_iso(meta.get(field) or item.get(field))
        if release_dt:
            break

    series_slug = meta.get("series_slug_title") or meta.get("series_id") or ""
    series_id = meta.get("series_id", "")
    season = meta.get("season_number")
    episode = meta.get("episode_number") or meta.get("episode")

    parts = []
    if season:
        parts.append(f"Temporada {season}")
    if episode:
        parts.append(f"episodio {episode}")

    return {
        "title": meta.get("series_title") or item.get("title", "?"),
        "slug": series_slug or item.get("title", "").lower().replace(" ", "-"),
        "series_url": f"{BASE}/es-es/series/{series_id}/{series_slug}" if series_id else "",
        "watch_url": f"{BASE}/es-es/watch/{item.get('id')}/{item.get('slug_title', '')}",
        "audio_code": audio_code(item),
        "episodes": ", ".join(parts),
        "premiere": str(episode) == "1",
        "date": release_dt.date() if release_dt else None,
        "release_time": release_dt.isoformat() if release_dt else "Fecha desconocida",
    }


# ---------------------------------------------------------------------------
# Historial y RSS
# ---------------------------------------------------------------------------

def load_history():
    if not os.path.exists(HISTORY_FILE):
        return {}
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except Exception:
            return {}


def save_history(history):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=4, ensure_ascii=False)


def load_or_create_rss():
    if os.path.exists(RSS_FILE):
        try:
            tree = ET.parse(RSS_FILE)
            root = tree.getroot()
            channel = root.find("channel")
            if channel.find("link") is not None:
                channel.find("link").text = CALENDAR_URL
            if channel.find("description") is not None:
                channel.find("description").text = CHANNEL_DESCRIPTION
            return tree, root
        except ET.ParseError:
            pass
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "Estrenos Doblajes - Calendario Crunchyroll"
    ET.SubElement(channel, "link").text = CALENDAR_URL
    ET.SubElement(channel, "description").text = CHANNEL_DESCRIPTION
    return ET.ElementTree(rss), rss


def add_rss_item(channel, release, guid):
    episodes = f" ({release['episodes']})" if release["episodes"] else ""
    premiere = " [Estreno]" if release["premiere"] else ""
    item = ET.SubElement(channel, "item")
    ET.SubElement(item, "title").text = f"¡NUEVO DOBLAJE ({LANG_NAME})! - {release['title']}"
    ET.SubElement(item, "link").text = release["watch_url"] or release["series_url"] or CALENDAR_URL
    ET.SubElement(item, "description").text = (
        f"El anime '{release['title']}'{episodes}{premiere} estrena doblaje en "
        f"{LANG_NAME}. Fecha y hora programada: {release['release_time']}."
    )
    ET.SubElement(item, "guid", isPermaLink="false").text = guid
    ET.SubElement(item, "pubDate").text = datetime.now(timezone.utc).strftime(
        "%a, %d %b %Y %H:%M:%S GMT"
    )


# ---------------------------------------------------------------------------
# Detección de novedades
# ---------------------------------------------------------------------------

def seen_recently(slug, release_date, history):
    prefix = f"{slug}_{LANG_KEY}_"
    for key in history:
        if not key.startswith(prefix):
            continue
        try:
            old = datetime.strptime(key[len(prefix):], "%Y-%m-%d").date()
        except ValueError:
            continue
        if abs((release_date - old).days) <= NEW_SEASON_GAP_DAYS:
            return True
    return False


def process(releases, history, channel):
    new_alerts = changed = False
    grouped = {}
    for r in releases:
        if r["audio_code"] != LANG_KEY or r["date"] is None:
            continue
        key = (r["slug"], r["date"])
        if key in grouped:
            grouped[key]["premiere"] |= r["premiere"]
        else:
            grouped[key] = r

    for (slug, release_date), r in sorted(grouped.items(), key=lambda x: x[0][1]):
        unique_key = f"{slug}_{LANG_KEY}_{release_date.isoformat()}"
        if unique_key in history:
            continue
        is_new = not seen_recently(slug, release_date, history)
        history[unique_key] = r["release_time"]
        changed = True
        if is_new:
            print(f"  ✅ ¡NUEVO DOBLAJE! {r['title']} - {r['release_time']}")
            add_rss_item(channel, r, unique_key)
            new_alerts = True
        else:
            print(f"  ↪️ Ya conocido: {r['title']} ({release_date})")
    return new_alerts, changed


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("ℹ️ scraper vía API de Crunchyroll (sin navegador)")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", action="append", help="Respuesta JSON guardada de /discover/browse (repetible).")
    parser.add_argument("--dry-run", action="store_true", help="No escribe feed.xml ni history.json.")
    args = parser.parse_args()

    items = []
    if args.file:
        for path in args.file:
            with open(path, encoding="utf-8") as f:
                items.extend(json.load(f).get("data", []))
    else:
        session = make_session()
        token = get_token(session)
        today = datetime.now(MADRID).date()
        monday = today - timedelta(days=today.weekday())
        for n in range(WEEKS_BACK, -1, -1):
            items.extend(fetch_week(session, token, monday - timedelta(weeks=n)))

    if not items:
        print("⚠️ La API no devolvió ningún episodio.")
        sys.exit(1)

    # Diagnóstico: campos del primer episodio (útil si cambia la API).
    meta_keys = sorted((items[0].get("episode_metadata") or {}).keys())
    print(f"🔎 Campos de episode_metadata: {', '.join(meta_keys) or '(ninguno)'}")

    seen, releases = set(), []
    for item in items:
        if item.get("id") in seen:
            continue
        seen.add(item.get("id"))
        releases.append(to_release(item))

    codes = {}
    for r in releases:
        codes[r["audio_code"]] = codes.get(r["audio_code"], 0) + 1
    print(f"📺 {len(releases)} episodios. Por audio: "
          + ", ".join(f"{k}={v}" for k, v in sorted(codes.items())))

    history = load_history()
    tree, rss = load_or_create_rss()
    new_alerts, changed = process(releases, history, rss.find("channel"))

    if args.dry_run:
        print("\n(dry-run) No se guardan cambios.")
        return
    if changed:
        save_history(history)
    if new_alerts:
        ET.indent(tree, space="  ")
        tree.write(RSS_FILE, encoding="utf-8", xml_declaration=True)
        print("\n💾 RSS y base de datos actualizados.")
    elif changed:
        print("\n💾 Base de datos actualizada (sin nuevos estrenos).")
    else:
        print("\n❌ No hay nuevos estrenos esta ronda.")


if __name__ == "__main__":
    main()
