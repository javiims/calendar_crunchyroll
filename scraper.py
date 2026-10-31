"""
Crunchyroll release-calendar dub tracker (Castellano).

Opens https://www.crunchyroll.com/es-es/releasecalendar, applies the calendar's
audio filter "Español (España)" and publishes an RSS item every time a series
starts receiving episodes with Castellano audio.

The calendar is rendered client-side, so the page is driven with a headless
Chromium (Playwright).

Usage:
    python scraper.py                      # live scrape (used by GitHub Actions)
    python scraper.py --file page.mhtml    # parse a page saved WITH the filter applied
    python scraper.py --debug              # also saves debug_*.png / debug_*.html
"""

import argparse
import email
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from email import policy
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

HISTORY_FILE = "history.json"
RSS_FILE = "feed.xml"
CALENDAR_URL = "https://www.crunchyroll.com/es-es/releasecalendar"

AUDIO_FILTER_LABEL = "Español (España)"   # Opción del filtro de audio del calendario
LANG_KEY = "ESES"                          # Sufijo usado en history.json
LANG_NAME = "Castellano (ESES)"

# Si una serie no ha aparecido con castellano en este número de días,
# se considera un nuevo bloque (nueva temporada / nuevo doblaje).
NEW_SEASON_GAP_DAYS = 21

# Semanas anteriores a revisar además de la actual.
WEEKS_BACK = 1

TZ = ZoneInfo("Europe/Madrid")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

CARD_SELECTOR = '[data-t="release-calendar-card"]'

SPANISH_MONTHS = {
    "ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6,
    "jul": 7, "ago": 8, "sep": 9, "set": 9, "oct": 10, "nov": 11, "dic": 12,
}

LANG_LABEL_RE = re.compile(r"^[^:]+:\s*([A-Z]{2,3}(?:\s*,\s*[A-Z]{2,3})*)\s*$")


class FilterError(RuntimeError):
    pass


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


CHANNEL_DESCRIPTION = "Avisos de nuevos animes doblados al Castellano (ESES)."


def load_or_create_rss():
    if os.path.exists(RSS_FILE):
        try:
            tree = ET.parse(RSS_FILE)
            root = tree.getroot()
            channel = root.find("channel")
            link = channel.find("link")
            if link is not None:
                link.text = CALENDAR_URL
            desc = channel.find("description")
            if desc is not None:
                desc.text = CHANNEL_DESCRIPTION
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
# Obtención del HTML
# ---------------------------------------------------------------------------

def read_saved_page(path):
    """Lee un .html o un .mhtml/.mht guardado desde el navegador."""
    with open(path, "rb") as f:
        raw = f.read()
    head = raw[:2000].decode("utf-8", errors="ignore")
    if "MIME-Version" in head and "multipart/related" in head:
        msg = email.message_from_bytes(raw, policy=policy.default)
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                return part.get_content()
        raise ValueError("El MHTML no contiene ninguna parte text/html.")
    return raw.decode("utf-8", errors="ignore")


def card_count(page):
    return page.locator(CARD_SELECTOR).count()


def dismiss_overlays(page):
    """Cierra el banner de cookies (OneTrust) y el aviso de Términos de Uso."""
    selectors = [
        "#onetrust-reject-all-handler",
        "#onetrust-accept-btn-handler",
        '[data-t="grant-anonymous-consent-btn"]',
    ]
    for sel in selectors:
        loc = page.locator(sel)
        try:
            if loc.count() and loc.first.is_visible():
                loc.first.click(timeout=3_000)
                page.wait_for_timeout(500)
        except Exception:
            pass


def find_filter_option(page):
    """
    Busca la opción "Español (España)" SOLO dentro de controles de filtro
    (menús, listas, diálogos, checkboxes/radios). Así evitamos pulsar el
    selector de idioma del sitio que aparece en el pie de página.
    """
    name = re.compile(rf"^\s*{re.escape(AUDIO_FILTER_LABEL)}\s*$")
    candidates = [
        page.get_by_role("option", name=name),
        page.get_by_role("menuitemradio", name=name),
        page.get_by_role("menuitemcheckbox", name=name),
        page.get_by_role("menuitem", name=name),
        page.get_by_role("checkbox", name=name),
        page.get_by_role("radio", name=name),
        page.locator(
            '.erc-language-filtering, [role="listbox"], [role="menu"], [role="dialog"]'
        ).get_by_text(AUDIO_FILTER_LABEL, exact=True),
    ]
    for loc in candidates:
        for i in range(loc.count()):
            el = loc.nth(i)
            if el.is_visible():
                return el
    return None


def apply_audio_filter(page):
    """Abre el filtro de audio y selecciona "Español (España)"."""
    before = card_count(page)

    option = find_filter_option(page)  # por si las opciones ya están visibles
    if option is None:
        # Posibles botones que abren el filtro de audio.
        # Botón "Filtros" junto al título del calendario
        # (<div class="erc-language-filtering"><div role="button" aria-label="Filtrar" aria-haspopup="listbox">).
        openers = [
            page.locator('.erc-language-filtering [aria-haspopup="listbox"]'),
            page.locator('.erc-language-filtering [role="button"]'),
            page.get_by_role("button", name=re.compile(r"^\s*filtr", re.I)),
        ]
        for loc in openers:
            for i in range(loc.count()):
                el = loc.nth(i)
                if not el.is_visible():
                    continue
                try:
                    el.click(timeout=3_000)
                except Exception:
                    continue
                page.wait_for_timeout(800)
                option = find_filter_option(page)
                if option is not None:
                    break
            if option is not None:
                break

    if option is None:
        raise FilterError(f"No se encontró la opción de filtro '{AUDIO_FILTER_LABEL}'.")

    option.click(timeout=5_000)
    page.wait_for_timeout(1_000)

    # Algunos filtros requieren confirmar con un botón "Aplicar".
    apply_btn = page.get_by_role("button", name=re.compile(r"^\s*(aplicar|mostrar|guardar|listo)", re.I))
    if apply_btn.count() and apply_btn.first.is_visible():
        apply_btn.first.click(timeout=3_000)

    page.keyboard.press("Escape")
    page.wait_for_load_state("networkidle", timeout=30_000)
    page.wait_for_timeout(2_500)

    after = card_count(page)
    print(f"🎚️ Filtro '{AUDIO_FILTER_LABEL}' aplicado: {before} → {after} tarjetas. URL: {page.url}")

    # Comprobación de seguridad: si el número de tarjetas no baja, el filtro
    # probablemente no se aplicó y avisaríamos de TODO lo que tenga audio ES.
    if before > 0 and after >= before:
        raise FilterError("El número de tarjetas no cambió tras aplicar el filtro.")


def fetch_pages_live(weeks_back=WEEKS_BACK, debug=False):
    from playwright.sync_api import sync_playwright

    pages_html = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        context = browser.new_context(
            locale="es-ES",
            timezone_id="Europe/Madrid",
            user_agent=USER_AGENT,
            viewport={"width": 1600, "height": 1000},
        )
        page = context.new_page()
        try:
            page.goto(CALENDAR_URL, wait_until="domcontentloaded", timeout=90_000)
            page.wait_for_selector(CARD_SELECTOR, timeout=90_000)
            page.wait_for_timeout(2_000)
            dismiss_overlays(page)

            apply_audio_filter(page)

            week = page.inner_text('[data-t="week-range"]').strip()
            print(f"🗓️ Semana cargada: {week}")
            pages_html.append(page.content())

            for n in range(weeks_back):
                button = page.query_selector('[data-t="previous-week"]')
                if button is None or button.is_disabled():
                    break
                page.eval_on_selector('[data-t="previous-week"]', "el => el.click()")
                try:
                    page.wait_for_function(
                        """(prev) => {
                            const el = document.querySelector('[data-t="week-range"]');
                            return el && el.innerText.trim() !== prev;
                        }""",
                        arg=week,
                        timeout=30_000,
                    )
                except Exception:
                    print("  ⚠️ No se pudo cambiar de semana.")
                    break
                page.wait_for_timeout(2_500)
                week = page.inner_text('[data-t="week-range"]').strip()
                print(f"🗓️ Semana cargada: {week}")
                pages_html.append(page.content())
        except Exception:
            if debug or os.environ.get("GITHUB_ACTIONS"):
                page.screenshot(path="debug_page.png", full_page=True)
                with open("debug_page.html", "w", encoding="utf-8") as f:
                    f.write(page.content())
                print("🐞 Guardados debug_page.png y debug_page.html")
            raise
        finally:
            browser.close()
    return pages_html


# ---------------------------------------------------------------------------
# Parseo
# ---------------------------------------------------------------------------

def parse_day_date(text, today):
    """'26 oct' -> date. Deduce el año a partir de la fecha actual."""
    m = re.match(r"\s*(\d{1,2})\s+([a-záéíóú]+)\.?", text.strip().lower())
    if not m:
        return None
    month = SPANISH_MONTHS.get(m.group(2)[:3])
    if not month:
        return None
    candidate = date(today.year, month, int(m.group(1)))
    if (candidate - today).days > 180:
        candidate = date(today.year - 1, candidate.month, candidate.day)
    elif (today - candidate).days > 180:
        candidate = date(today.year + 1, candidate.month, candidate.day)
    return candidate


def parse_time(text):
    """'4:15 p. m.' / '12:00 a. m.' / '16:15' -> (h, m)."""
    t = text.lower().replace("\xa0", " ")
    m = re.search(r"(\d{1,2}):(\d{2})", t)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    compact = re.sub(r"[\s.]", "", t)
    if compact.endswith("pm") and hour != 12:
        hour += 12
    elif compact.endswith("am") and hour == 12:
        hour = 0
    return hour, minute


def text_without_sr_only(tag):
    clone = BeautifulSoup(str(tag), "html.parser")
    for sr in clone.select('[data-t="sr-only"]'):
        sr.decompose()
    return clone.get_text(" ", strip=True)


def extract_langs(card):
    for el in card.find_all(attrs={"aria-label": True}):
        m = LANG_LABEL_RE.match(el["aria-label"].strip())
        if m:
            return [c.strip() for c in m.group(1).split(",")]
    return []


def parse_calendar(html, today):
    """Todas las tarjetas del HTML se consideran Castellano (el filtro ya está aplicado)."""
    soup = BeautifulSoup(html, "html.parser")
    releases = []

    for column in soup.select('[data-t="day-column"]'):
        date_tag = column.select_one('[class*="day-date"]')
        day = parse_day_date(date_tag.get_text(strip=True), today) if date_tag else None

        for card in column.select(CARD_SELECTOR):
            series_link = card.select_one('[data-t="series-page-link"]')
            watch_link = card.select_one('[data-t="watch-page-link"]')
            time_tag = card.select_one('[data-t="publish-time"]')
            episodes_tag = card.select_one('[data-t="episodes"] [data-t="sr-only"]')

            series_url = urljoin(CALENDAR_URL, series_link.get("href", "")) if series_link else ""
            watch_url = urljoin(CALENDAR_URL, watch_link.get("href", "")) if watch_link else ""

            m = re.search(r"/series/([^/]+)/([^/?#]+)", series_url)
            series_id = m.group(1) if m else ""
            slug = m.group(2) if m else ""
            title = series_link.get_text(strip=True) if series_link else slug.replace("-", " ").title()

            release_dt = None
            hm = parse_time(text_without_sr_only(time_tag)) if time_tag else None
            if day and hm:
                release_dt = datetime(day.year, day.month, day.day, hm[0], hm[1], tzinfo=TZ)

            releases.append({
                "title": title,
                "slug": slug or series_id or title.lower().replace(" ", "-"),
                "series_url": series_url,
                "watch_url": watch_url,
                "langs": extract_langs(card),
                "premiere": card.select_one('[data-t="premiere-tag"]') is not None,
                "episodes": episodes_tag.get_text(strip=True) if episodes_tag else "",
                "date": day,
                "release_time": release_dt.isoformat() if release_dt
                else (day.isoformat() if day else "Fecha desconocida"),
            })
    return releases


# ---------------------------------------------------------------------------
# Lógica de detección
# ---------------------------------------------------------------------------

def seen_recently(slug, release_date, history, gap_days):
    prefix = f"{slug}_{LANG_KEY}_"
    for key in history:
        if not key.startswith(prefix):
            continue
        try:
            old = datetime.strptime(key[len(prefix):], "%Y-%m-%d").date()
        except ValueError:
            continue
        if abs((release_date - old).days) <= gap_days:
            return True
    return False


def process(releases, history, channel):
    """Devuelve (hay_alertas_nuevas, historial_modificado)."""
    new_alerts = False
    changed = False

    # Una serie puede tener varias tarjetas el mismo día: agrupamos por serie y día.
    grouped = {}
    for r in releases:
        if r["date"] is None:
            continue
        key = (r["slug"], r["date"])
        if key not in grouped:
            grouped[key] = r
        else:
            grouped[key]["premiere"] = grouped[key]["premiere"] or r["premiere"]

    for (slug, release_date), r in sorted(grouped.items(), key=lambda x: x[0][1]):
        unique_key = f"{slug}_{LANG_KEY}_{release_date.isoformat()}"
        if unique_key in history:
            continue

        is_new = not seen_recently(slug, release_date, history, NEW_SEASON_GAP_DAYS)

        # Guardamos SIEMPRE la aparición para que la ventana de "visto
        # recientemente" avance semana a semana y no se repitan avisos.
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
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", action="append", help="HTML/MHTML guardado con el filtro aplicado (repetible).")
    parser.add_argument("--today", help="Fecha de referencia YYYY-MM-DD (para deducir el año).")
    parser.add_argument("--dry-run", action="store_true", help="No escribe feed.xml ni history.json.")
    parser.add_argument("--debug", action="store_true", help="Guarda captura y HTML si algo falla.")
    args = parser.parse_args()

    today = date.fromisoformat(args.today) if args.today else datetime.now(TZ).date()

    if args.file:
        pages = [read_saved_page(p) for p in args.file]
    else:
        try:
            pages = fetch_pages_live(debug=args.debug)
        except FilterError as e:
            # Abortamos sin tocar nada: sin filtro avisaríamos de doblajes que no son castellano.
            print(f"❌ {e} Abortando sin modificar el feed.")
            sys.exit(1)

    releases = []
    for html in pages:
        releases.extend(parse_calendar(html, today))

    print(f"📺 {len(releases)} lanzamientos con audio {AUDIO_FILTER_LABEL}.")

    history = load_history()
    tree, rss = load_or_create_rss()
    channel = rss.find("channel")

    new_alerts, changed = process(releases, history, channel)

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
