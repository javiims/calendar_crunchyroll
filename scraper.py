import os
import json
import time
import requests
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET

HISTORY_FILE = "history.json"
RSS_FILE = "feed.xml"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Accept-Language": "es-ES,es;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/avif,image/webp,*/*;q=0.8",
}


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
            return tree, tree.getroot()
        except ET.ParseError:
            pass

    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")

    ET.SubElement(channel, "title").text = (
        "Estrenos Doblajes - Calendario Crunchyroll"
    )
    ET.SubElement(channel, "link").text = (
        "https://www.crunchyroll.com/es-es/simulcastcalendar"
    )
    ET.SubElement(channel, "description").text = (
        "Avisos de nuevos animes doblados al Castellano (ESES) e Italiano (ITIT)."
    )

    return ET.ElementTree(rss), rss


def add_rss_item(channel, title, lang, release_time, calendar_url):
    item = ET.SubElement(channel, "item")

    ET.SubElement(item, "title").text = (
        f"¡NUEVO DOBLAJE ({lang})! - {title}"
    )

    ET.SubElement(item, "link").text = calendar_url

    ET.SubElement(item, "description").text = (
        f"El anime '{title}' estrenará doblaje en {lang}. "
        f"Fecha y hora programada: {release_time}."
    )

    ET.SubElement(item, "pubDate").text = (
        datetime.now(timezone.utc).strftime(
            "%a, %d %b %Y %H:%M:%S GMT"
        )
    )


def get_next_8_mondays():
    today = datetime.now(timezone.utc)

    days_since_monday = today.weekday()
    last_monday = today - timedelta(days=days_since_monday)

    return [
        (last_monday + timedelta(days=7 * i)).strftime("%Y-%m-%d")
        for i in range(8)
    ]


def normalize_release_date(release_time):
    """
    Extrae únicamente la fecha del datetime de Crunchyroll.

    Ejemplo:
        2026-09-21T18:00:00+02:00
        -> 2026-09-21

    Esto permite agrupar todos los episodios de una misma semana.
    """
    if not release_time or release_time == "Fecha desconocida":
        return None

    try:
        return release_time[:10]
    except Exception:
        return None


def get_season_key(slug, lang_key, release_date, history):
    """
    Genera una clave que permite detectar nuevas temporadas.

    Problema original:
        slug + idioma

    Eso hacía que:
        serie-x_ESES

    bloqueara también las temporadas futuras.

    Ahora usamos:
        slug + idioma + fecha de inicio

    De esta forma, una nueva temporada que empiece en otra fecha
    puede generar una nueva alerta.
    """

    if release_date:
        return f"{slug}_{lang_key}_{release_date}"

    # Fallback para casos donde Crunchyroll no proporciona fecha.
    return f"{slug}_{lang_key}"


def already_seen_same_series_recently(slug, lang_key, release_date, history):
    """
    Evita crear una alerta por cada episodio semanal.

    Si ya existe una entrada para la misma serie/idioma dentro
    de los últimos 7 días, consideramos que pertenece al mismo
    bloque de estreno y no generamos otra alerta.

    Las temporadas nuevas normalmente estarán separadas por mucho
    más de 7 días.
    """

    if not release_date:
        return False

    try:
        current_date = datetime.strptime(release_date, "%Y-%m-%d").date()
    except ValueError:
        return False

    prefix = f"{slug}_{lang_key}_"

    for key, value in history.items():

        if not key.startswith(prefix):
            continue

        try:
            old_date = datetime.strptime(
                key[len(prefix):],
                "%Y-%m-%d"
            ).date()
        except ValueError:
            continue

        difference = abs((current_date - old_date).days)

        if difference <= 7:
            return True

    return False


def main():
    history = load_history()

    tree, rss = load_or_create_rss()
    channel = rss.find("channel")

    mondays = get_next_8_mondays()

    session = requests.Session()

    found_any_new = False

    for date_str in mondays:

        calendar_url = (
            "https://www.crunchyroll.com/es-es/"
            f"simulcastcalendar?filter=premium&date={date_str}"
        )

        print(f"🗓️ Analizando semana del: {date_str}")

        try:
            r = session.get(
                calendar_url,
                headers=HEADERS,
                timeout=30
            )

            r.raise_for_status()

        except Exception as e:
            print(f"  ⚠️ Error de conexión: {e}")
            time.sleep(2)
            continue

        soup = BeautifulSoup(r.text, "html.parser")

        articles = soup.find_all(
            "article",
            class_="js-release"
        )

        # Agrupamos los estrenos de la semana por slug.
        weekly_releases = {}

        for art in articles:

            popover_url = art.get(
                "data-popover-url",
                ""
            )

            is_es = popover_url.endswith("ESES")
            is_it = popover_url.endswith("ITIT")

            if not (is_es or is_it):
                continue

            slug = art.get(
                "data-slug",
                "titulo-desconocido"
            )

            lang_code = (
                "ESES"
                if is_es
                else "ITIT"
            )

            title_tag = art.find(
                itemprop="name"
            )

            title = (
                title_tag.text.strip()
                if title_tag
                else slug.replace("-", " ").title()
            )

            time_tag = art.find(
                "time",
                class_="available-time"
            )

            release_time = (
                time_tag.get("datetime")
                if time_tag
                else "Fecha desconocida"
            )

            release_date = normalize_release_date(
                release_time
            )

            if slug not in weekly_releases:

                weekly_releases[slug] = {
                    "title": title,
                    "time": release_time,
                    "date": release_date,
                    "langs": set()
                }

            weekly_releases[slug]["langs"].add(
                lang_code
            )

        # Procesamos la lista filtrada.
        for slug, data in weekly_releases.items():

            # Si tiene Castellano, ignoramos Italiano.
            if "ESES" in data["langs"]:

                lang = "Castellano (ESES)"
                lang_key = "ESES"

            elif "ITIT" in data["langs"]:

                lang = "Italiano (ITIT)"
                lang_key = "ITIT"

            else:
                continue

            title = data["title"]
            release_time = data["time"]
            release_date = data["date"]

            # ---------------------------------------------------------
            # NUEVA LÓGICA
            # ---------------------------------------------------------
            #
            # Antes:
            #
            #   unique_key = f"{slug}_{lang_key}"
            #
            # Esto hacía imposible detectar una nueva temporada.
            #
            # Ahora cada periodo de estreno tiene su propia entrada.
            # ---------------------------------------------------------

            unique_key = get_season_key(
                slug,
                lang_key,
                release_date,
                history
            )

            # Caso normal: ya conocemos exactamente este estreno.
            if unique_key in history:
                continue

            # Si no tenemos fecha, usamos el comportamiento antiguo.
            if release_date is None:

                legacy_key = f"{slug}_{lang_key}"

                if legacy_key in history:
                    continue

            # Evitamos una alerta por cada episodio semanal.
            if already_seen_same_series_recently(
                slug,
                lang_key,
                release_date,
                history
            ):
                continue

            print(
                f"  ✅ ¡NUEVO ESTRENO! "
                f"{title} en {lang}"
            )

            add_rss_item(
                channel,
                title,
                lang,
                release_time,
                calendar_url
            )

            history[unique_key] = release_time

            found_any_new = True

        time.sleep(2)

    if found_any_new:

        save_history(history)

        tree.write(
            RSS_FILE,
            encoding="utf-8",
            xml_declaration=True
        )

        print(
            "\n💾 RSS y base de datos actualizados."
        )

    else:

        print(
            "\n❌ No hay nuevos estrenos esta ronda."
        )


if __name__ == "__main__":
    main()
