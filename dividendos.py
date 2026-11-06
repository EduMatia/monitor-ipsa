"""Dividendos de la Bolsa de Santiago: lee la tabla pegada y convierte a pesos los montos en US$ o euros.
Formato del texto pegado: cada acción empieza con una línea  #NEMOTECNICO  y debajo la tabla copiada tal cual
(Descripción, Fecha límite, Fecha de pago)."""
import re
from datetime import timedelta

import pandas as pd
import requests

from importador import numero

TIPOS = {"DEF": "Definitivo", "PROV": "Provisorio", "ADIC": "Adicional", "EVEN": "Eventual", "EVENT": "Eventual"}
MONEDAS = {"US$": "USD", "USD": "USD", "EURO": "EUR", "EUR": "EUR", "€": "EUR", "$": "CLP", "CLP": "CLP", "PESOS": "CLP"}
COLUMNAS = [
    "ticker", "n_dividendo", "tipo", "moneda", "monto_original", "fecha_corte", "fecha_pago",
    "tipo_cambio", "fecha_tc", "monto_por_accion", "fuente",
]

PATRON = re.compile(
    r"DIVIDENDO\s+(?P<tipo>[A-ZÁÉÍÓÚ]+)\.?\s*(?P<num>\d+)?\s*"
    r"(?P<moneda>US\$|USD|EURO|EUR|€|CLP|PESOS|\$)\s*(?P<monto>\d[\d.,]*)\s+"
    r"(?P<limite>\d{1,2}-\d{1,2}-\d{4})\s+(?P<pago>\d{1,2}-\d{1,2}-\d{4})",
    re.IGNORECASE,
)


def _fecha(txt):
    d, m, a = (int(x) for x in txt.split("-"))
    return pd.Timestamp(year=a, month=m, day=d).strftime("%Y-%m-%d")


def parsear_dividendos(texto, tickers_validos=None):
    """Devuelve (DataFrame con los dividendos leídos, lista de avisos)."""
    bloques, avisos, ticker = {}, [], None
    for linea in texto.splitlines():
        if linea.strip().startswith("#"):
            nemo = linea.strip().lstrip("#").strip().upper()
            ticker = nemo if nemo.endswith(".SN") else nemo + ".SN"
            bloques.setdefault(ticker, [])
        elif ticker is not None:
            bloques[ticker].append(linea)

    filas = []
    for t, lineas in bloques.items():
        if tickers_validos is not None and t not in tickers_validos:
            avisos.append(f"{t.replace('.SN', '')} no está en empresas.csv; se ignoró.")
            continue
        plano = " ".join(" ".join(lineas).split())
        n = 0
        for m in PATRON.finditer(plano):
            monto = numero(m.group("monto"))
            if monto is None or monto <= 0:
                continue
            tipo_txt = m.group("tipo").upper()
            filas.append({
                "ticker": t,
                "n_dividendo": m.group("num") or "",
                "tipo": TIPOS.get(tipo_txt, tipo_txt.capitalize()),
                "moneda": MONEDAS[m.group("moneda").upper()],
                "monto_original": monto,
                "fecha_corte": _fecha(m.group("limite")),
                "fecha_pago": _fecha(m.group("pago")),
            })
            n += 1
        if n == 0:
            avisos.append(f"{t.replace('.SN', '')}: no encontré dividendos en el texto pegado.")
    df = pd.DataFrame(filas, columns=["ticker", "n_dividendo", "tipo", "moneda", "monto_original", "fecha_corte", "fecha_pago"])
    return df.drop_duplicates(), avisos


def quinto_dia_habil_antes(fecha):
    """Quinto día hábil (lunes a viernes, sin considerar feriados) anterior a la fecha."""
    d, n = fecha, 0
    while n < 5:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return d


def fecha_referencia(base, fecha_corte, fecha_pago):
    if base == "pago":
        return fecha_pago
    if base == "pago_5":
        return quinto_dia_habil_antes(fecha_pago)
    return fecha_corte


def tipo_cambio_mindicador(moneda, fecha, max_retroceso=7):
    """Dólar observado o euro (Banco Central, vía mindicador.cl) en la fecha; si no hay dato ese día, el día hábil anterior."""
    codigo = {"USD": "dolar", "EUR": "euro"}[moneda]
    for i in range(max_retroceso + 1):
        f = fecha - timedelta(days=i)
        r = requests.get(f"https://mindicador.cl/api/{codigo}/{f:%d-%m-%Y}", timeout=15)
        r.raise_for_status()
        serie = r.json().get("serie", [])
        if serie:
            return float(serie[0]["valor"]), f
    return None, None


def completar(df, base, hoy, obtener=tipo_cambio_mindicador):
    """Agrega tipo de cambio, fecha del tipo de cambio, monto en pesos y estado a cada dividendo."""
    out = df.copy()
    hoy = pd.Timestamp(hoy).date()
    tcs, fechas_tc, clp, estados = [], [], [], []
    for _, r in out.iterrows():
        if r["moneda"] == "CLP":
            tcs.append(1.0); fechas_tc.append(""); clp.append(round(float(r["monto_original"]), 6)); estados.append("OK")
            continue
        ref = fecha_referencia(base, pd.Timestamp(r["fecha_corte"]).date(), pd.Timestamp(r["fecha_pago"]).date())
        if ref > hoy:
            tcs.append(None); fechas_tc.append(""); clp.append(None); estados.append("tipo de cambio aún no disponible")
            continue
        try:
            valor, usada = obtener(r["moneda"], ref)
        except Exception:
            valor, usada = None, None
        if valor:
            tcs.append(valor); fechas_tc.append(usada.isoformat())
            clp.append(round(float(r["monto_original"]) * valor, 6)); estados.append("OK")
        else:
            tcs.append(None); fechas_tc.append(""); clp.append(None); estados.append("sin tipo de cambio")
    out["tipo_cambio"], out["fecha_tc"], out["monto_por_accion"], out["estado"] = tcs, fechas_tc, clp, estados
    return out


def fusionar_dividendos(existente, nuevos):
    """Une lo ya guardado en dividendos.csv con lo nuevo (lo nuevo manda). Acepta el formato antiguo del archivo."""
    cols = COLUMNAS
    if existente is None or existente.empty:
        base = pd.DataFrame(columns=cols)
    else:
        base = existente.copy()
        if "moneda" not in base:
            base["moneda"] = "CLP"
        if "monto_original" not in base:
            base["monto_original"] = base.get("monto_por_accion")
        for c in cols:
            if c not in base:
                base[c] = ""
        base = base[cols]
    nuevo = nuevos.copy()
    if "fuente" not in nuevo:
        nuevo["fuente"] = "Bolsa de Santiago"
    for c in cols:
        if c not in nuevo:
            nuevo[c] = ""
    todo = pd.concat([base, nuevo[cols]], ignore_index=True)
    todo["n_dividendo"] = todo["n_dividendo"].fillna("").astype(str).str.replace(r"\.0$", "", regex=True)
    clave_con_n = todo["ticker"] + "|" + todo["n_dividendo"] + "|" + todo["fecha_pago"].astype(str)
    clave_sin_n = todo["ticker"] + "|" + todo["tipo"].astype(str) + "|" + todo["fecha_corte"].astype(str) + "|" + todo["monto_original"].astype(str)
    todo["_clave"] = clave_con_n.where(todo["n_dividendo"] != "", clave_sin_n)
    todo = todo.drop_duplicates("_clave", keep="last").drop(columns="_clave")
    return todo.sort_values(["ticker", "fecha_corte"]).reset_index(drop=True)
