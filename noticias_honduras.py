#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Recopilador de noticias de Honduras -> Telegram
------------------------------------------------
Reúne las noticias del día publicadas por los principales diarios digitales
hondureños y las envía a un canal de Telegram tres veces al día
(mañana, tarde y noche).

Fuente de datos: Google News RSS (edición Honduras, español). Esto evita
depender de que cada diario tenga su propio feed RSS estable: consultamos
Google News restringido a cada dominio con el operador "site:".

Estado (para no repetir noticias): estado.json, que GitHub Actions guarda
de vuelta en el repositorio después de cada ejecución.

Variables de entorno requeridas:
    TELEGRAM_BOT_TOKEN   token del bot (de @BotFather)
    TELEGRAM_CHAT_ID     id del canal (p. ej. -1001234567890) o @usuariocanal

Opcionales:
    MOMENTO              "manana" | "tarde" | "noche"  (si no, se deduce por la hora HN)
    VENTANA_HORAS        horas hacia atrás a considerar (por defecto 14)
    MAX_POR_FUENTE       máximo de titulares por diario (por defecto 6)
"""

import os
import re
import sys
import json
import html
import time
import hashlib
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

import requests
import feedparser

# --------------------------------------------------------------------------
# CONFIGURACIÓN
# --------------------------------------------------------------------------

# Honduras usa UTC-6 todo el año (no hay horario de verano).
HN_TZ = timezone(timedelta(hours=-6))

# Diarios digitales de Honduras. Cada valor es la consulta que se envía a
# Google News. Puedes agregar, quitar o comentar líneas según tu preferencia.
FUENTES = {
    "El Heraldo":       "site:elheraldo.hn",
    "La Prensa":        "site:laprensa.hn",
    "La Tribuna":       "site:latribuna.hn",
    "Diario Tiempo":    "site:tiempo.hn",
    "El Mundo":         "site:elmundo.hn",
    "El País":          "site:elpais.hn",
    "Proceso Digital":  "site:proceso.hn",
    "Hondudiario":      "site:hondudiario.com",
    "Contracorriente":  "site:contracorriente.red",
    "Criterio.hn":      "site:criterio.hn",
}

# Búsqueda general de respaldo, para no perder temas relevantes que quizá
# no queden bien atribuidos a un dominio en particular.
BUSQUEDA_GENERAL = ("Honduras", "Honduras noticias")

# Plantilla de Google News RSS (edición Honduras / español latinoamericano).
GOOGLE_NEWS = (
    "https://news.google.com/rss/search?"
    "q={q}&hl=es-419&gl=HN&ceid=HN:es-419"
)

MAX_POR_FUENTE = int(os.environ.get("MAX_POR_FUENTE", "6"))
VENTANA_HORAS  = int(os.environ.get("VENTANA_HORAS", "14"))
STATE_FILE     = os.environ.get("STATE_FILE", "estado.json")
MAX_HISTORIAL  = 1200          # cuántos IDs de noticias recordar como "ya enviadas"
TELEGRAM_LIMIT = 3800          # margen seguro bajo el límite real de 4096

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    )
}

MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
         "agosto", "septiembre", "octubre", "noviembre", "diciembre"]


# --------------------------------------------------------------------------
# UTILIDADES
# --------------------------------------------------------------------------

def ahora_hn():
    return datetime.now(HN_TZ)


def momento_del_dia(dt=None):
    """Devuelve 'manana', 'tarde' o 'noche' según la hora en Honduras."""
    forzado = os.environ.get("MOMENTO", "").strip().lower()
    if forzado in ("manana", "mañana", "tarde", "noche"):
        return "manana" if forzado == "mañana" else forzado
    dt = dt or ahora_hn()
    if dt.hour < 12:
        return "manana"
    if dt.hour < 18:
        return "tarde"
    return "noche"


def saludo(momento):
    return {
        "manana": "☀️ <b>Buenos días</b>",
        "tarde":  "🌤️ <b>Buenas tardes</b>",
        "noche":  "🌙 <b>Buenas noches</b>",
    }[momento]


def fecha_larga(dt=None):
    dt = dt or ahora_hn()
    return f"{dt.day} de {MESES[dt.month - 1]} de {dt.year}"


def id_noticia(link, titulo):
    """Identificador estable para deduplicar y recordar lo ya enviado."""
    base = (link or "") + "|" + (titulo or "")
    return hashlib.sha1(base.encode("utf-8", "ignore")).hexdigest()[:16]


def limpiar_titulo(titulo, fuente):
    """Google News agrega ' - Nombre del medio' al final; lo quitamos."""
    if not titulo:
        return ""
    t = html.unescape(titulo).strip()
    # Quitar el último segmento ' - Editor' si es corto (típico de Google News).
    if " - " in t:
        izquierda, derecha = t.rsplit(" - ", 1)
        if izquierda and len(derecha) <= 45:
            t = izquierda.strip()
    return t


def normalizar(texto):
    return re.sub(r"\s+", " ", (texto or "").lower()).strip()


# --------------------------------------------------------------------------
# ESTADO (noticias ya enviadas)
# --------------------------------------------------------------------------

def cargar_estado():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return list(data.get("enviadas", []))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def guardar_estado(enviadas):
    enviadas = enviadas[-MAX_HISTORIAL:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {"actualizado": ahora_hn().isoformat(), "enviadas": enviadas},
            f, ensure_ascii=False, indent=2,
        )


# --------------------------------------------------------------------------
# OBTENER NOTICIAS
# --------------------------------------------------------------------------

def descargar_feed(query, intentos=3):
    """Descarga y parsea un feed de Google News para una consulta."""
    url = GOOGLE_NEWS.format(q=quote_plus(query))
    for intento in range(1, intentos + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=25)
            r.raise_for_status()
            return feedparser.parse(r.content)
        except Exception as e:
            print(f"   ! intento {intento}/{intentos} falló para '{query}': {e}")
            time.sleep(2 * intento)
    return None


def fecha_entrada(entry):
    """Devuelve la fecha de publicación (aware, UTC) o None."""
    for campo in ("published_parsed", "updated_parsed"):
        t = entry.get(campo)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return None


def recolectar():
    """Recolecta noticias recientes agrupadas por fuente."""
    limite = datetime.now(timezone.utc) - timedelta(hours=VENTANA_HORAS)
    resultado = {}       # fuente -> [ {titulo, link, id, fecha}, ... ]
    vistos_url = set()
    vistos_titulo = set()

    def procesar(nombre_fuente, feed):
        if not feed or not getattr(feed, "entries", None):
            return
        items = []
        for e in feed.entries:
            titulo = limpiar_titulo(e.get("title", ""), nombre_fuente)
            link = e.get("link", "")
            if not titulo or not link:
                continue

            f = fecha_entrada(e)
            if f is not None and f < limite:
                continue  # demasiado vieja

            clave_titulo = normalizar(titulo)
            if link in vistos_url or clave_titulo in vistos_titulo:
                continue
            vistos_url.add(link)
            vistos_titulo.add(clave_titulo)

            items.append({
                "titulo": titulo,
                "link": link,
                "id": id_noticia(link, titulo),
                "fecha": f,
            })
            if len(items) >= MAX_POR_FUENTE:
                break
        if items:
            resultado.setdefault(nombre_fuente, []).extend(items)

    # 1) Una consulta por diario.
    for nombre, consulta in FUENTES.items():
        print(f" -> {nombre}")
        feed = descargar_feed(f"{consulta} when:1d")
        procesar(nombre, feed)
        time.sleep(1)  # cortesía con el servicio

    # 2) Búsqueda general de respaldo.
    for q in BUSQUEDA_GENERAL:
        feed = descargar_feed(f"{q} when:1d")
        procesar("Otros medios", feed)
        time.sleep(1)

    return resultado


# --------------------------------------------------------------------------
# FORMATO Y ENVÍO
# --------------------------------------------------------------------------

def construir_mensajes(noticias_por_fuente, momento):
    """Arma uno o varios mensajes (respetando el límite de Telegram)."""
    encabezado = (
        f"{saludo(momento)}, Paco\n"
        f"📰 <b>Resumen de noticias — Honduras</b>\n"
        f"🗓️ {fecha_larga()}\n"
        f"{'━' * 16}\n"
    )

    bloques = []
    total = 0
    for fuente, items in noticias_por_fuente.items():
        if not items:
            continue
        lineas = [f"\n🔹 <b>{html.escape(fuente)}</b>"]
        for it in items:
            titulo = html.escape(it["titulo"])
            lineas.append(f"• <a href=\"{html.escape(it['link'])}\">{titulo}</a>")
            total += 1
        bloques.append("\n".join(lineas))

    if total == 0:
        return []

    pie = (
        f"\n{'━' * 16}\n"
        f"✅ {total} titulares · Fuente: diarios digitales de Honduras"
    )

    # Ensamblar respetando el límite; el encabezado va en el primer mensaje.
    mensajes = []
    actual = encabezado
    for bloque in bloques:
        if len(actual) + len(bloque) + 2 > TELEGRAM_LIMIT:
            mensajes.append(actual.rstrip())
            actual = ""  # continuaciones sin repetir encabezado
        actual += bloque + "\n"

    # Añadir el pie al último mensaje (o crear uno nuevo si no cabe).
    if len(actual) + len(pie) > TELEGRAM_LIMIT:
        mensajes.append(actual.rstrip())
        actual = pie
    else:
        actual += pie
    mensajes.append(actual.rstrip())

    return mensajes


def enviar_telegram(token, chat_id, texto):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": texto,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    r = requests.post(url, data=payload, timeout=30)
    if r.status_code != 200:
        print(f"   ! Telegram respondió {r.status_code}: {r.text[:300]}")
    r.raise_for_status()
    return r.json()


# --------------------------------------------------------------------------
# PRINCIPAL
# --------------------------------------------------------------------------

def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("ERROR: faltan TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID.")
        sys.exit(1)

    momento = momento_del_dia()
    print(f"Momento del día: {momento}  ({ahora_hn():%Y-%m-%d %H:%M} HN)")

    print("Recolectando noticias...")
    crudas = recolectar()

    # Filtrar lo ya enviado en corridas anteriores.
    ya_enviadas = cargar_estado()
    set_enviadas = set(ya_enviadas)

    nuevas_por_fuente = {}
    ids_nuevos = []
    for fuente, items in crudas.items():
        frescas = [it for it in items if it["id"] not in set_enviadas]
        if frescas:
            nuevas_por_fuente[fuente] = frescas
            ids_nuevos.extend(it["id"] for it in frescas)

    total_nuevas = sum(len(v) for v in nuevas_por_fuente.values())
    print(f"Noticias nuevas encontradas: {total_nuevas}")

    if total_nuevas == 0:
        print("No hay noticias nuevas. No se envía nada.")
        return

    mensajes = construir_mensajes(nuevas_por_fuente, momento)
    print(f"Enviando {len(mensajes)} mensaje(s) a Telegram...")
    for i, m in enumerate(mensajes, 1):
        enviar_telegram(token, chat_id, m)
        print(f"   Mensaje {i}/{len(mensajes)} enviado.")
        time.sleep(1)

    # Registrar como enviadas y guardar estado.
    guardar_estado(ya_enviadas + ids_nuevos)
    print("Listo.")


if __name__ == "__main__":
    main()
