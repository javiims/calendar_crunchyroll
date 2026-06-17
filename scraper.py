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
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Accept-Language": "es-ES,es;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
}

def load_history():
    if not os.path.exists(HISTORY_FILE):
        return {}
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except:
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
            print("  [DEBUG] El archivo feed.xml está vacío o corrupto. Se creará uno nuevo.")
            pass
    
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "Estrenos Doblajes - Calendario Crunchyroll"
    ET.SubElement(channel, "link").text = "https://www.crunchyroll.com/es-es/simulcastcalendar"
    ET.SubElement(channel, "description").text = "Avisos de nuevos animes doblados al Castellano (ESES) e Italiano (ITIT) en las próximas 8 semanas."
    return ET.ElementTree(rss), rss

def add_rss_item(channel, title, lang, release_time, url):
    item = ET.SubElement(channel, "item")
    ET.SubElement(item, "title").text = f"¡NUEVO DOBLAJE ({lang})! - {title}"
    ET.SubElement(item, "link").text = url
    ET.SubElement(item, "description").text = f"El anime '{title}' estrenará doblaje en {lang}. Fecha y hora programada: {release_time}."
    ET.SubElement(item, "pubDate").text = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")

def get_next_8_mondays():
    today = datetime.now(timezone.utc)
    days_since_monday = today.weekday()
    last_monday = today - timedelta(days=days_since_monday)
    return [(last_monday + timedelta(days=7 * i)).strftime('%Y-%m-%d') for i in range(8)]

def main():
    history = load_history()
    tree, rss = load_or_create_rss()
    channel = rss.find("channel")
    
    mondays = get_next_8_mondays()
    session = requests.Session()
    found_any_new = False

    print(f"[DEBUG] Historial cargado con {len(history)} registros previos.")

    for i, date_str in enumerate(mondays):
        # AÑADIDO EL FILTRO PREMIUM AQUÍ
        url = f"https://www.crunchyroll.com/es-es/simulcastcalendar?filter=premium&date={date_str}"
        print(f"\n🗓️ Analizando semana del: {date_str}")
        print(f"  [DEBUG] Solicitando URL: {url}")
        
        try:
            r = session.get(url, headers=HEADERS, timeout=30)
            print(f"  [DEBUG] Código HTTP recibido: {r.status_code}")
            r.raise_for_status()
        except Exception as e:
            print(f"  ⚠️ Error de conexión: {e}")
            time.sleep(2)
            continue

        if i == 0:
            print("  [DEBUG] Guardando el HTML en 'debug_crunchyroll.html' para inspección...")
            with open("debug_crunchyroll.html", "w", encoding="utf-8") as f:
                f.write(r.text)

        soup = BeautifulSoup(r.text, 'html.parser')
        
        articles = soup.find_all('article', class_='js-release')
        print(f"  [DEBUG] Etiquetas <article> de episodios encontradas: {len(articles)}")
        
        if len(articles) == 0:
            print("  [DEBUG] ❌ No se ha encontrado ningún episodio. Posible bloqueo antibot o calendario vacío.")
        elif i == 0: 
            print(f"  [DEBUG] Ejemplo del primer popover-url encontrado: {articles[0].get('data-popover-url', 'N/A')}")

        for art in articles:
            popover_url = art.get('data-popover-url', '')
            
            is_es = popover_url.endswith('ESES')
            is_it = popover_url.endswith('ITIT')
            
            if is_es or is_it:
                slug = art.get('data-slug', 'titulo-desconocido')
                lang = "Castellano (ESES)" if is_es else "Italiano (ITIT)"
                
                title_tag = art.find(itemprop='name')
                title = title_tag.text.strip() if title_tag else slug.replace('-', ' ').title()
                
                time_tag = art.find('time', class_='available-time')
                release_time = time_tag.get('datetime') if time_tag else "Fecha desconocida"
                
                unique_key = f"{slug}_{lang}"
                
                if unique_key not in history:
                    print(f"  ✅ ¡NUEVO ESTRENO! {title} en {lang}")
                    episode_url = f"https://www.crunchyroll.com/es-es/series/{slug}"
                    add_rss_item(channel, title, lang, release_time, episode_url)
                    history[unique_key] = release_time
                    found_any_new = True
                else:
                    pass # Ya está en el historial, lo ignoramos en silencio
                
        time.sleep(2)

    if found_any_new:
        save_history(history)
        tree.write(RSS_FILE, encoding="utf-8", xml_declaration=True)
        print("\n💾 RSS y base de datos actualizados.")
    else:
        print("\n❌ No hay nuevos estrenos esta ronda.")

if __name__ == "__main__":
    main()
