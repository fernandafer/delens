"""
Delens - Radar de Señales de Negocio

Objetivo: detectar temas para posts de LinkedIn dirigidos a directores generales
de pymes españolas y autónomos, y temas de IA y negocio para marca personal.
Prioridad: Comunidad de Madrid y España.

Busca en las fuentes de fuentes.csv vía Serper.dev, se queda solo con lo
publicado en los últimos 15 días, filtra, comprueba que cada link responde,
prioriza y guarda como máximo 20 entradas en novedades.csv.

Uso:
    python radar_delens.py

Variables de entorno:
    SERPER_API_KEY     (obligatoria)
    ANTHROPIC_API_KEY  (opcional) si existe, el snippet se reescribe en una frase
                       sobre por qué interesa a un DG de pyme o a un autónomo.
                       Si no existe, se usa la primera frase del snippet de Google.

Input:  fuentes.csv  (Fuente,Dominio,Territorio,Keywords,Frecuencia,Prioridad,Max,Requiere,Excluir)
          - Dominio:    dominio o ruta. Varios separados por |
          - Keywords:   expresión de búsqueda opcional. Vacío = todo lo nuevo del dominio
          - Frecuencia: semanal | mensual (mensual = solo en la primera ejecución del mes)
          - Prioridad:  1 = máxima (Madrid), 2 = España / IA, 3 = complementaria, 4 = internacional
          - Max:        máximo de entradas por semana para esa fuente
          - Requiere:   términos separados por | ; el título o snippet debe contener al menos uno
          - Excluir:    términos separados por | ; si el título o snippet contiene alguno, se descarta
Output: novedades.csv  (mismas columnas de siempre, va adjunto al email)
        seen_links.csv (registro interno de links ya enviados, para no repetir)
"""

import os
import csv
import re
import sys
import time
import unicodedata
from datetime import date, datetime, timedelta

import requests

SERPER_API_KEY = os.environ.get("SERPER_API_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = "claude-haiku-5-5"

FUENTES_FILE = "fuentes.csv"
NOVEDADES_FILE = "novedades.csv"
SEEN_FILE = "seen_links.csv"

DIAS_VENTANA = 15
MAX_TOTAL = 20

# Respuestas que indican que la web bloquea bots pero el link existe
CODIGOS_BLOQUEO_BOT = {401, 403, 405, 429}

MESES = {
    "jan": 1, "ene": 1, "january": 1, "enero": 1,
    "feb": 2, "february": 2, "febrero": 2,
    "mar": 3, "march": 3, "marzo": 3,
    "apr": 4, "abr": 4, "april": 4, "abril": 4,
    "may": 5, "mayo": 5,
    "jun": 6, "june": 6, "junio": 6,
    "jul": 7, "july": 7, "julio": 7,
    "aug": 8, "ago": 8, "august": 8, "agosto": 8,
    "sep": 9, "sept": 9, "september": 9, "septiembre": 9, "setiembre": 9,
    "oct": 10, "october": 10, "octubre": 10,
    "nov": 11, "november": 11, "noviembre": 11,
    "dec": 12, "dic": 12, "december": 12, "diciembre": 12,
}


# ---------------------------------------------------------------- utilidades

def normalizar(texto: str) -> str:
    """Minúsculas y sin tildes, para comparar términos."""
    texto = unicodedata.normalize("NFKD", texto or "")
    return "".join(c for c in texto if not unicodedata.combining(c)).lower()


def parsear_fecha(fecha_txt, fecha_ref: date):
    """Convierte la fecha que devuelve Serper ('hace 5 días', '2 weeks ago',
    'Nov 12, 2026', '12 nov 2026'...) a date. Devuelve None si no la reconoce."""
    if not fecha_txt:
        return None
    texto = normalizar(fecha_txt.strip())

    m = re.search(r"(\d+)\s*(hora|hour|minuto|minute|dia|day|semana|week|mes|month|ano|year)", texto)
    if m:
        n, unidad = int(m.group(1)), m.group(2)
        if unidad in ("hora", "hour", "minuto", "minute"):
            delta = timedelta(0)
        elif unidad in ("dia", "day"):
            delta = timedelta(days=n)
        elif unidad in ("semana", "week"):
            delta = timedelta(weeks=n)
        elif unidad in ("mes", "month"):
            delta = timedelta(days=n * 30)
        else:
            delta = timedelta(days=n * 365)
        return fecha_ref - delta
    if "ayer" in texto or "yesterday" in texto:
        return fecha_ref - timedelta(days=1)
    if "hoy" in texto or "today" in texto:
        return fecha_ref

    # 'nov 12, 2026'
    m = re.search(r"([a-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})", texto)
    if m and m.group(1) in MESES:
        try:
            return date(int(m.group(3)), MESES[m.group(1)], int(m.group(2)))
        except ValueError:
            pass
    # '12 nov 2026' / '12 de noviembre de 2026'
    m = re.search(r"(\d{1,2})\s+(?:de\s+)?([a-z]+)\.?\s+(?:de\s+)?(\d{4})", texto)
    if m and m.group(2) in MESES:
        try:
            return date(int(m.group(3)), MESES[m.group(2)], int(m.group(1)))
        except ValueError:
            pass
    # '12/11/2026'
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", texto)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            pass
    return None


def lista_terminos(campo: str) -> list[str]:
    return [normalizar(t.strip()) for t in (campo or "").split("|") if t.strip()]


def contiene_alguno(texto_norm: str, terminos: list[str]) -> bool:
    return any(re.search(r"\b" + t, texto_norm) for t in terminos)


def puntuacion(texto_norm: str, keywords: str, requiere: list[str]) -> int:
    """Relevancia simple: cuántos términos de la búsqueda y de 'Requiere' aparecen."""
    kw = normalizar(keywords)
    frases = re.findall(r'"([^"]+)"', kw)
    sueltas = re.findall(r"\w{3,}", re.sub(r'"[^"]+"', " ", kw))
    terminos = {re.escape(t) for t in frases + sueltas if t not in ("and", "or", "not")}
    terminos |= set(requiere)
    return sum(1 for t in terminos if re.search(r"\b" + t, texto_norm))


# ---------------------------------------------------------------- Serper

def _post_serper(query: str, contexto: str, desde: date, hasta: date) -> list[dict]:
    """Llama a Serper limitando a [desde, hasta]. Si Serper responde con error,
    imprime el cuerpo de la respuesta antes de relanzar la excepción."""
    tbs = f"cdr:1,cd_min:{desde:%m/%d/%Y},cd_max:{hasta:%m/%d/%Y}"
    resp = requests.post(
        "https://google.serper.dev/search",
        headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
        json={"q": query, "num": 10, "tbs": tbs},
        timeout=30,
    )
    if not resp.ok:
        print(f"  [{contexto}] Serper respondió {resp.status_code}: {resp.text[:500]}")
        print(f"  [{contexto}] Query enviada: {query!r}")
    resp.raise_for_status()
    organic = resp.json().get("organic", [])[:10]
    return [
        {"titulo": r.get("title") or "", "snippet": r.get("snippet") or "",
         "link": r.get("link") or "", "fecha": r.get("date") or ""}
        for r in organic
    ]


def construir_query(dominio: str, keywords: str) -> str:
    dominios = [d.strip() for d in dominio.split("|") if d.strip()]
    site_filter = " OR ".join(f"site:{d}" for d in dominios)
    query = f"({site_filter})" if len(dominios) > 1 else site_filter
    if keywords:
        query += f" ({keywords})"
    return query


# ---------------------------------------------------------------- comprobaciones

def link_valido(url: str) -> bool:
    """Comprueba que el link existe. Descarta 404/410, otros errores y fallos de
    conexión. Acepta respuestas de bloqueo anti-bot (403, 429...), porque la
    página existe aunque no deje entrar al script."""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; DelensRadar/1.0)"}
    try:
        r = requests.get(url, headers=headers, timeout=20, allow_redirects=True, stream=True)
        r.close()
    except requests.RequestException as e:
        print(f"    link descartado (sin respuesta): {url} -> {e.__class__.__name__}")
        return False
    if r.status_code < 400 or r.status_code in CODIGOS_BLOQUEO_BOT:
        return True
    print(f"    link descartado ({r.status_code}): {url}")
    return False


def snippet_por_defecto(snippet: str) -> str:
    snippet = re.sub(r"\s+", " ", snippet or "").strip()
    m = re.match(r"(.+?[.!?])(\s|$)", snippet)
    return (m.group(1) if m else snippet)[:300]


def snippet_para_dg(fuente: str, titulo: str, snippet: str) -> str:
    """Si hay ANTHROPIC_API_KEY, reescribe en una frase por qué interesa a un DG
    de pyme o a un autónomo, usando solo la información del título y el snippet.
    Si no hay clave o falla, devuelve la primera frase del snippet de Google."""
    if not ANTHROPIC_API_KEY:
        return snippet_por_defecto(snippet)
    prompt = (
        "Escribe UNA sola frase en español de España (máximo 35 palabras) que explique "
        "por qué esta publicación puede interesar a un director general de pyme o a un "
        "autónomo en España. Usa solo la información del título y el extracto; no añadas "
        "cifras, fechas ni datos que no aparezcan en ellos. Devuelve solo la frase.\n\n"
        f"Fuente: {fuente}\nTítulo: {titulo}\nExtracto: {snippet}"
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": ANTHROPIC_MODEL, "max_tokens": 150,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=60,
        )
        r.raise_for_status()
        texto = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
        texto = re.sub(r"\s+", " ", texto).strip()
        return texto or snippet_por_defecto(snippet)
    except requests.RequestException as e:
        print(f"    resumen con IA falló ({e.__class__.__name__}); uso el snippet de Google")
        return snippet_por_defecto(snippet)


# ---------------------------------------------------------------- ficheros

def toca_hoy(frecuencia: str, hoy: date) -> bool:
    if frecuencia.strip().lower() == "mensual":
        return hoy.day <= 7
    return True


def leer_fuentes() -> list[dict]:
    if not os.path.exists(FUENTES_FILE):
        print(f"ERROR: no existe {FUENTES_FILE}")
        sys.exit(1)
    with open(FUENTES_FILE, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def leer_seen() -> set:
    if not os.path.exists(SEEN_FILE):
        return set()
    with open(SEEN_FILE, newline="", encoding="utf-8") as f:
        return {row["link"] for row in csv.DictReader(f)}


def inicializar_csv(path: str, columnas: list[str]):
    if not os.path.exists(path):
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(columnas)


def entero(valor, defecto: int) -> int:
    try:
        return int(str(valor).strip())
    except (TypeError, ValueError):
        return defecto


# ---------------------------------------------------------------- main

def main():
    if not SERPER_API_KEY:
        print("ERROR: falta SERPER_API_KEY en variables de entorno")
        sys.exit(1)

    fuentes = leer_fuentes()
    seen = leer_seen()

    novedad_cols = ["fecha_revision", "fuente", "territorio", "titulo", "snippet", "link", "fecha_articulo"]
    inicializar_csv(SEEN_FILE, ["link", "fuente", "fecha_primera_vista"])

    hoy_date = date.today()
    hoy = hoy_date.isoformat()
    desde = hoy_date - timedelta(days=DIAS_VENTANA)
    errores = 0
    candidatos = []          # todos los resultados válidos de todas las fuentes
    links_en_ronda = set()

    for orden, row in enumerate(fuentes):
        fuente = (row.get("Fuente") or "").strip()
        dominio = (row.get("Dominio") or "").strip()
        territorio = (row.get("Territorio") or "").strip()
        keywords = (row.get("Keywords") or "").strip()
        frecuencia = (row.get("Frecuencia") or "semanal").strip()
        prioridad = entero(row.get("Prioridad"), 3)
        max_fuente = entero(row.get("Max"), 3)
        requiere = lista_terminos(row.get("Requiere"))
        excluir = lista_terminos(row.get("Excluir"))
        if not fuente or not dominio:
            continue
        if not toca_hoy(frecuencia, hoy_date):
            print(f"Saltando (mensual): {fuente}")
            continue

        print(f"Buscando: {fuente} ({territorio})")
        try:
            resultados = _post_serper(construir_query(dominio, keywords), fuente, desde, hoy_date)
        except requests.RequestException as e:
            print(f"  ERROR de red/API para {fuente}: {e}")
            errores += 1
            continue

        aceptados = []
        for r in resultados:
            link = r["link"]
            if not link or link in seen or link in links_en_ronda:
                continue
            texto = normalizar(f"{r['titulo']} {r['snippet']}")
            if requiere and not contiene_alguno(texto, requiere):
                continue
            if excluir and contiene_alguno(texto, excluir):
                continue
            fecha = parsear_fecha(r["fecha"], hoy_date)
            # Fuera de la ventana de 15 días -> fuera. Sin fecha: se acepta porque
            # Google ya ha filtrado por el rango, pero fecha_articulo queda vacía.
            if fecha and fecha < desde:
                continue
            aceptados.append({
                **r, "fecha_d": fecha, "fuente": fuente, "territorio": territorio,
                "prioridad": prioridad, "orden": orden,
                "score": puntuacion(texto, keywords, requiere),
            })

        # Las más relevantes y recientes de la fuente primero; tope por fuente
        aceptados.sort(key=lambda c: (-c["score"], -(c["fecha_d"] or date.min).toordinal()))
        elegidos = []
        for c in aceptados:
            if len(elegidos) >= max_fuente:
                break
            if link_valido(c["link"]):
                elegidos.append(c)
                links_en_ronda.add(c["link"])
        candidatos.extend(elegidos)
        print(f"  -> {len(elegidos)} candidata(s)")
        time.sleep(1)

    # Prioridad (Madrid > España/IA > resto), luego relevancia y fecha. Tope global.
    candidatos.sort(key=lambda c: (c["prioridad"], -c["score"],
                                   -(c["fecha_d"] or date.min).toordinal(), c["orden"]))
    finales = candidatos[:MAX_TOTAL]
    finales.sort(key=lambda c: (c["prioridad"], c["orden"]))

    with open(NOVEDADES_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(novedad_cols)
        for c in finales:
            w.writerow([
                hoy, c["fuente"], c["territorio"], c["titulo"],
                snippet_para_dg(c["fuente"], c["titulo"], c["snippet"]),
                c["link"],
                c["fecha_d"].strftime("%d/%m/%Y") if c["fecha_d"] else "",
            ])

    # Solo se marcan como vistos los que se envían; lo que queda fuera por el tope
    # puede salir la semana siguiente si sigue dentro de los 15 días.
    with open(SEEN_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        for c in finales:
            w.writerow([c["link"], c["fuente"], hoy])

    total = len(finales)
    print(f"Hecho. Novedades enviadas: {total} (de {len(candidatos)} candidatas). "
          f"Fuentes con error: {errores}")

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"novedades={total}\n")
            f.write(f"errores={errores}\n")


if __name__ == "__main__":
    main()
