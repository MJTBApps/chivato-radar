#!/usr/bin/env python3
"""Genera la base de radares de la app: ios/Radares/Recursos/radares.json.

Fuentes (todas públicas, sin clave):
  - OpenStreetMap (Overpass): radares fijos y de tramo de la península, Baleares y Canarias.
    Licencia ODbL: hay que citar «© colaboradores de OpenStreetMap» en la app.
  - Servei Català de Trànsit: radares fijos y de tramo de Cataluña, con carretera,
    punto kilométrico y límite (coordenadas UTM 31N ETRS89).
  - DGT (inventario DATEX II): cabinas fijas y tramos del resto de España, con carretera
    pero sin límite. Añade los que faltan; si su servidor falla se sigue sin ella.

Solo usa la biblioteca estándar:  python3 scripts/actualizar_radares.py
Con --osm ARCHIVO, --transit ARCHIVO o --dgt ARCHIVO se usa una copia ya descargada.
"""
import argparse
import datetime
import json
import math
import pathlib
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

RAIZ = pathlib.Path(__file__).resolve().parent.parent
SALIDA = RAIZ / "ios" / "Radares" / "Recursos" / "radares.json"
AGENTE = "radares-datos/0.1"

OVERPASS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
# sur, oeste, norte, este
ZONAS = {
    "península y Baleares": (35.2, -9.6, 43.9, 4.4),
    "Canarias": (27.5, -18.3, 29.5, -13.3),
}
TRANSIT = "http://transit.gencat.cat/web/.content/documents/seguretat_viaria/radars.txt"
# Inventario de la DGT en DATEX II (cabinas fijas y tramos de velocidad media). La dirección
# antigua (infocar.dgt.es) da error 502 desde noviembre de 2026; esta es la del punto de acceso nacional.
DGT = "https://nap.dgt.es/datex2/dgt/PredefinedLocationsPublication/radares/content.xml"
DATEX = "{http://datex2.eu/schema/1_0/1_0}"


def descargar(url, datos=None, espera=180):
    peticion = urllib.request.Request(url, data=datos, headers={"User-Agent": AGENTE})
    with urllib.request.urlopen(peticion, timeout=espera) as r:
        return r.read()


# ---------- geometría ----------

def distancia(lat1, lon1, lat2, lon2):
    f1, f2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((f2 - f1) / 2) ** 2 + math.cos(f1) * math.cos(f2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371000 * math.asin(min(1, math.sqrt(a)))


def rumbo(lat1, lon1, lat2, lon2):
    f1, f2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(f2)
    x = math.cos(f1) * math.sin(f2) - math.sin(f1) * math.cos(f2) * math.cos(dl)
    return math.degrees(math.atan2(y, x)) % 360


def utm_a_geograficas(x, y, huso=31):
    """UTM (ETRS89, hemisferio norte) a latitud y longitud."""
    a, f = 6378137.0, 1 / 298.257222101
    e2 = f * (2 - f)
    k0 = 0.9996
    x -= 500000.0
    m = y / k0
    mu = m / (a * (1 - e2 / 4 - 3 * e2 ** 2 / 64 - 5 * e2 ** 3 / 256))
    e1 = (1 - math.sqrt(1 - e2)) / (1 + math.sqrt(1 - e2))
    fp = (mu + (3 * e1 / 2 - 27 * e1 ** 3 / 32) * math.sin(2 * mu)
          + (21 * e1 ** 2 / 16 - 55 * e1 ** 4 / 32) * math.sin(4 * mu)
          + (151 * e1 ** 3 / 96) * math.sin(6 * mu)
          + (1097 * e1 ** 4 / 512) * math.sin(8 * mu))
    ep2 = e2 / (1 - e2)
    c1 = ep2 * math.cos(fp) ** 2
    t1 = math.tan(fp) ** 2
    n1 = a / math.sqrt(1 - e2 * math.sin(fp) ** 2)
    r1 = a * (1 - e2) / (1 - e2 * math.sin(fp) ** 2) ** 1.5
    d = x / (n1 * k0)
    lat = fp - (n1 * math.tan(fp) / r1) * (
        d ** 2 / 2 - (5 + 3 * t1 + 10 * c1 - 4 * c1 ** 2 - 9 * ep2) * d ** 4 / 24
        + (61 + 90 * t1 + 298 * c1 + 45 * t1 ** 2 - 252 * ep2 - 3 * c1 ** 2) * d ** 6 / 720)
    lon = (d - (1 + 2 * t1 + c1) * d ** 3 / 6
           + (5 - 2 * c1 + 28 * t1 - 3 * c1 ** 2 + 8 * ep2 + 24 * t1 ** 2) * d ** 5 / 120) / math.cos(fp)
    return math.degrees(lat), math.degrees(lon) + (huso * 6 - 183)


# ---------- OpenStreetMap ----------

def bajar_osm():
    elementos = []
    for nombre, (s, o, n, e) in ZONAS.items():
        caja = f"({s},{o},{n},{e})"
        consulta = (f'[out:json][timeout:150];(node["highway"="speed_camera"]{caja};'
                    f'relation["type"="enforcement"]{caja};);out body;>;out skel qt;')
        cuerpo = urllib.parse.urlencode({"data": consulta}).encode()
        for intento in range(6):
            servidor = OVERPASS[intento % len(OVERPASS)]
            try:
                datos = json.loads(descargar(servidor, cuerpo))
                elementos += datos["elements"]
                print(f"  OSM {nombre}: {len(datos['elements'])} elementos ({servidor.split('/')[2]})")
                break
            except Exception as error:  # servidores públicos: a menudo saturados
                print(f"  OSM {nombre}: {servidor.split('/')[2]} falla ({error}); reintento")
                time.sleep(15 * (intento + 1))
        else:
            raise SystemExit(f"No se ha podido descargar OpenStreetMap ({nombre})")
    return {"elements": elementos}


def limite_osm(etiquetas):
    texto = (etiquetas.get("maxspeed") or "").strip()
    if texto.isdigit() and 10 <= int(texto) <= 130:
        return int(texto)
    return None


def radares_osm(datos):
    nodos = {e["id"]: e for e in datos["elements"] if e["type"] == "node"}
    salida, en_relacion = [], set()

    for rel in (e for e in datos["elements"] if e["type"] == "relation"):
        etiquetas = rel.get("tags", {})
        clase = etiquetas.get("enforcement")
        if clase not in ("maxspeed", "average_speed"):
            continue   # semáforos, peso, peaje…: no son radares de velocidad

        def puntos(papel):
            return [nodos[m["ref"]] for m in rel.get("members", [])
                    if m["type"] == "node" and m.get("role") == papel and m["ref"] in nodos]

        aparatos, desde, hasta = puntos("device"), puntos("from"), puntos("to")
        en_relacion.update(n["id"] for n in aparatos)
        limite = limite_osm(etiquetas)

        if clase == "average_speed" and (desde or aparatos) and hasta:
            ini, fin = (desde or aparatos)[0], hasta[0]
            recta = distancia(ini["lat"], ini["lon"], fin["lat"], fin["lon"])
            if 300 <= recta <= 40000:
                salida.append({"id": f"osm-r{rel['id']}", "lat": ini["lat"], "lon": ini["lon"], "tipo": "tramo",
                               "limite": limite, "finLat": fin["lat"], "finLon": fin["lon"], "fuente": "osm"})
                continue
        # Radar fijo (o tramo sin extremos claros): un punto por aparato.
        for i, ap in enumerate(aparatos):
            r = {"id": f"osm-r{rel['id']}" + (f"-{i}" if i else ""), "lat": ap["lat"], "lon": ap["lon"],
                 "tipo": "fijo", "limite": limite, "fuente": "osm"}
            if clase == "maxspeed" and desde:
                d = distancia(desde[0]["lat"], desde[0]["lon"], ap["lat"], ap["lon"])
                if 20 <= d <= 3000:   # el sentido vigilado: de «from» hacia el aparato
                    r["rumbo"] = round(rumbo(desde[0]["lat"], desde[0]["lon"], ap["lat"], ap["lon"]), 1)
            salida.append(r)

    for n in nodos.values():
        etiquetas = n.get("tags", {})
        if etiquetas.get("highway") == "speed_camera" and n["id"] not in en_relacion:
            # La etiqueta «direction» no se usa: no queda claro si es hacia donde mira
            # la cámara o el sentido del tráfico, y un error dejaría el radar mudo.
            salida.append({"id": f"osm-n{n['id']}", "lat": n["lat"], "lon": n["lon"], "tipo": "fijo",
                           "limite": limite_osm(etiquetas), "fuente": "osm"})
    return salida


# ---------- Trànsit (Cataluña) ----------

def numero(texto):
    return float(texto.replace(".", "").replace(",", ".")) if texto.count(",") else float(texto)


def coordenada(texto, enteros):
    """Coordenada UTM con la coma donde toca: 6 cifras enteras la X y 7 la Y.

    En el archivo hay filas con la coma corrida o sin coma («42552972» por 425529,72).
    En las filas correctas no cambia nada.
    """
    cifras = "".join(c for c in texto if c.isdigit())
    if len(cifras) < enteros:
        raise ValueError(texto)
    return float(cifras[:enteros] + "." + (cifras[enteros:] or "0"))


def radares_transit(texto):
    salida, descartados, vistos, filas = [], [], set(), []
    for linea in texto.splitlines():
        campos = linea.split()
        if len(campos) != 5 or campos[0] == "Via":
            continue
        via, pk, velocidad, x, y = campos
        try:
            x, y = coordenada(x, 6), coordenada(y, 7)
            limite = int(velocidad)
        except ValueError:
            descartados.append(linea.strip())
            continue
        # Cataluña en UTM 31N: lo que caiga fuera es una errata de la fuente sin arreglo
        # (por ejemplo, dos X en vez de X e Y).
        if not (250000 <= x <= 540000 and 4480000 <= y <= 4760000):
            descartados.append(linea.strip())
            continue
        if (via, pk) in vistos:   # fila repetida
            continue
        vistos.add((via, pk))
        lat, lon = utm_a_geograficas(x, y)
        try:
            limpia = numero(campos[3]) == x and numero(campos[4]) == y
            km = numero(pk.split("-")[0])
        except ValueError:
            limpia, km = False, None
        filas.append((limpia, km, via, lat, lon, linea.strip()))
        r = {"id": f"cat-{via}-{pk}".replace(",", "."), "lat": lat, "lon": lon, "tipo": "fijo",
             "limite": limite if 10 <= limite <= 130 else None, "via": via, "fuente": "transit"}
        if "-" in pk:   # «501,5-506,5»: tramo, del primer punto kilométrico al segundo
            try:
                a, b = (numero(p) for p in pk.split("-"))
                r["tipo"] = "tramo"
                r["longitud"] = round(abs(b - a) * 1000)
            except ValueError:
                pass
        salida.append(r)

    # Una coordenada arreglada puede ser en realidad otra cosa (dos Y seguidas pasan por
    # X e Y). Solo se acepta si cae donde toca: cerca de un radar bien escrito de la misma
    # carretera, a una distancia acorde con la diferencia de punto kilométrico.
    buenas = [f for f in filas if f[0] and f[1] is not None]
    quitar = set()
    for i, (limpia, km, via, lat, lon, linea) in enumerate(filas):
        if limpia:
            continue
        vecinas = sorted(((abs(km - b[1]), b) for b in buenas if b[2] == via), key=lambda v: v[0]) if km is not None else []
        creible = False
        if vecinas:   # la bien escrita de punto kilométrico más parecido
            salto, b = vecinas[0][0], vecinas[0][1]
            creible = distancia(lat, lon, b[3], b[4]) <= 1300 * salto + 1500
        if not creible:
            quitar.add(i)
            descartados.append(linea)
    salida = [r for i, r in enumerate(salida) if i not in quitar]
    return salida, descartados


# ---------- DGT ----------

def radares_dgt(xml):
    """Cabinas y tramos del inventario de la DGT. Trae la carretera, pero no el límite ni
    un sentido que se pueda usar (solo «kilómetros crecientes o decrecientes»)."""
    def punto(nodo):
        c = nodo.find(f".//{DATEX}pointCoordinates")
        return float(c.find(DATEX + "latitude").text), float(c.find(DATEX + "longitude").text)

    salida = []
    for sitio in ET.fromstring(xml).iter(DATEX + "predefinedLocationSet"):
        for ficha in sitio.findall(DATEX + "predefinedLocation"):
            dentro = ficha.find(DATEX + "predefinedLocation")
            if dentro is None:
                continue
            via = dentro.findtext(f".//{DATEX}roadNumber")
            r = {"id": "dgt-" + ficha.get("id", "").replace("GUID_", ""), "tipo": "fijo",
                 "via": via.strip() if via and via.strip() else None, "fuente": "dgt"}
            try:
                linea = dentro.find(DATEX + "tpeglinearLocation")
                if linea is not None:
                    (r["lat"], r["lon"]), fin = punto(linea.find(DATEX + "from")), punto(linea.find(DATEX + "to"))
                    if 300 <= distancia(r["lat"], r["lon"], *fin) <= 40000:
                        r["tipo"], r["finLat"], r["finLon"] = "tramo", fin[0], fin[1]
                else:
                    r["lat"], r["lon"] = punto(dentro)
            except (AttributeError, TypeError, ValueError):
                continue   # ficha sin coordenadas
            if 27 <= r["lat"] <= 44.5 and -18.5 <= r["lon"] <= 4.6:
                salida.append(r)
    return salida


# ---------- mezcla ----------

def mezclar(osm, transit):
    """Trànsit manda en Cataluña (trae carretera y límite); de OSM se quita el que coincide."""
    radares = list(transit)
    usados = 0
    for r in osm:
        radio = 400 if r["tipo"] == "tramo" else 150
        gemelo = next((t for t in transit
                       if abs(t["lat"] - r["lat"]) < 0.01 and abs(t["lon"] - r["lon"]) < 0.01
                       and distancia(t["lat"], t["lon"], r["lat"], r["lon"]) <= radio), None)
        if gemelo is None:
            radares.append(r)
            usados += 1
        else:
            # Lo que OSM sabe y Trànsit no: el punto final del tramo. El sentido no se copia:
            # una fila de Trànsit puede valer para los dos sentidos y quedaría muda en uno.
            if gemelo["tipo"] == r["tipo"] == "tramo" and "finLat" in r and "finLat" not in gemelo:
                gemelo["finLat"], gemelo["finLon"] = r["finLat"], r["finLon"]
    return radares, usados


def sumar_dgt(radares, dgt):
    """Añade los radares de la DGT que no estaban. Al que ya estaba (OSM suele traer el límite
    y a veces el sentido) solo le pone la carretera y, si es un tramo sin final, el final."""
    nuevos = []
    for d in dgt:
        radio = 400 if d["tipo"] == "tramo" else 150
        gemelos = [r for r in radares
                   if abs(r["lat"] - d["lat"]) < 0.01 and abs(r["lon"] - d["lon"]) < 0.01
                   and distancia(r["lat"], r["lon"], d["lat"], d["lon"]) <= radio]
        if not gemelos:
            nuevos.append(d)
            continue
        for g in gemelos:
            if not g.get("via") and d.get("via"):
                g["via"] = d["via"]
            if g["tipo"] == d["tipo"] == "tramo" and "finLat" not in g and "longitud" not in g:
                g["finLat"], g["finLon"] = d["finLat"], d["finLon"]
    return radares + nuevos, len(nuevos)


def limpiar(r):
    r = {k: v for k, v in r.items() if v is not None}
    for clave in ("lat", "lon", "finLat", "finLon"):
        if clave in r:
            r[clave] = round(r[clave], 6)
    return r


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--osm", help="archivo JSON de Overpass ya descargado")
    p.add_argument("--transit", help="radars.txt ya descargado")
    p.add_argument("--dgt", help="content.xml de la DGT ya descargado")
    p.add_argument("--salida", default=str(SALIDA))
    a = p.parse_args()

    print("Trànsit")
    texto = pathlib.Path(a.transit).read_text("latin-1") if a.transit else descargar(TRANSIT, espera=60).decode("latin-1")
    transit, descartados = radares_transit(texto)
    print(f"  {len(transit)} radares; {len(descartados)} filas descartadas por coordenadas imposibles")
    for linea in descartados:
        print("    ·", linea)

    print("OpenStreetMap")
    datos = json.loads(pathlib.Path(a.osm).read_text()) if a.osm else bajar_osm()
    osm = radares_osm(datos)
    print(f"  {len(osm)} radares")

    radares, de_osm = mezclar(osm, transit)

    print("DGT")
    try:
        xml = pathlib.Path(a.dgt).read_bytes() if a.dgt else descargar(DGT, espera=90)
        dgt = radares_dgt(xml)
        if len(dgt) < 300:
            raise ValueError(f"solo {len(dgt)} radares")
        radares, de_dgt = sumar_dgt(radares, dgt)
        print(f"  {len(dgt)} radares; {de_dgt} no estaban en las otras fuentes")
    except Exception as error:   # la DGT falla a menudo: se sigue sin ella y se dice
        de_dgt = 0
        print(f"  NO INCLUIDA: {error}")
    radares = sorted((limpiar(r) for r in radares), key=lambda r: r["id"])
    ids = [r["id"] for r in radares]
    if len(set(ids)) != len(ids):
        repetidos = sorted({i for i in ids if ids.count(i) > 1})
        raise SystemExit(f"Identificadores repetidos: {repetidos[:10]}")
    if len(radares) < 500:
        raise SystemExit(f"Solo {len(radares)} radares: alguna fuente ha fallado; no se escribe nada")

    base = {"generado": datetime.date.today().isoformat(),
            "fuentes": {"transit": len(transit), "osm": de_osm, "dgt": de_dgt},
            "radares": radares}
    salida = pathlib.Path(a.salida)
    salida.parent.mkdir(parents=True, exist_ok=True)
    salida.write_text(json.dumps(base, ensure_ascii=False, separators=(",", ":")) + "\n", "utf-8")
    tramos = sum(r["tipo"] == "tramo" for r in radares)
    print(f"Escritos {len(radares)} radares ({tramos} de tramo, {sum('limite' in r for r in radares)} con límite, "
          f"{sum('rumbo' in r for r in radares)} con sentido) en {salida} ({salida.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    sys.exit(main())
