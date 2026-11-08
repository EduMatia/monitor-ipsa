"""Convierte tablas de precios históricos pegadas desde una web (fecha y cierre) en filas para precios_manual.csv.
Formato del texto pegado: cada acción empieza con una línea  #NEMOTECNICO  (por ejemplo #AGUAS-A),
seguida de la tabla copiada tal cual. Solo se usan la fecha y el primer número después de ella (el cierre)."""
import re

import pandas as pd

FECHA = re.compile(r"^(\d{1,2})[./-](\d{1,2})[./-](\d{4})$")


def numero(token):
    """Convierte '2.015,00', '188,65' o '2015' en float. Devuelve None si no es un número."""
    t = token.strip()
    if not t or not re.fullmatch(r"[\d.,]+", t):
        return None
    try:
        if "," in t:
            return float(t.replace(".", "").replace(",", "."))
        if re.fullmatch(r"\d{1,3}(\.\d{3})+", t):  # 2.015 -> 2015
            return float(t.replace(".", ""))
        return float(t)
    except ValueError:
        return None


def parsear_pegado(texto, tickers_validos=None, ignorar_fecha=None):
    """Devuelve (DataFrame con fecha, ticker, cierre) y una lista de avisos.
    ignorar_fecha (AAAA-MM-DD): se omiten las filas de esa fecha (por ejemplo, hoy si el mercado aún no cierra,
    porque ahí 'Último' es un precio del momento y no un cierre)."""
    filas, avisos, omitidas = [], [], 0
    ticker = None
    bloques = {}
    for linea in texto.splitlines():
        if linea.strip().startswith("#"):
            nemo = linea.strip().lstrip("#").strip().upper()
            ticker = nemo if nemo.endswith(".SN") else nemo + ".SN"
            bloques.setdefault(ticker, [])
        elif ticker is not None:
            bloques[ticker].append(linea)
        elif linea.strip():
            avisos.append("Hay texto antes de la primera línea #NEMOTECNICO; se ignoró.")
            ticker = None

    for t, lineas in bloques.items():
        if tickers_validos is not None and t not in tickers_validos:
            avisos.append(f"{t.replace('.SN', '')} no está en empresas.csv; se ignoró.")
            continue
        tokens = " ".join(lineas).split()
        n = 0
        i = 0
        while i < len(tokens) - 1:
            m = FECHA.match(tokens[i])
            if m:
                valor = numero(tokens[i + 1])
                if valor is not None and valor > 0:
                    d, mes, a = int(m.group(1)), int(m.group(2)), int(m.group(3))
                    try:
                        fecha = pd.Timestamp(year=a, month=mes, day=d)
                    except ValueError:
                        i += 1
                        continue
                    if ignorar_fecha and fecha.strftime("%Y-%m-%d") == ignorar_fecha:
                        omitidas += 1
                        i += 2
                        continue
                    filas.append({"fecha": fecha.strftime("%Y-%m-%d"), "ticker": t, "cierre": round(valor, 4)})
                    n += 1
                    i += 2
                    continue
            i += 1
        if n == 0:
            avisos.append(f"{t.replace('.SN', '')}: no encontré filas con fecha y precio.")
    if omitidas:
        avisos.append(f"Se omitieron {omitidas} filas de hoy ({ignorar_fecha}): el mercado aún no cierra y ese precio no es un cierre.")
    df = pd.DataFrame(filas, columns=["fecha", "ticker", "cierre"])
    df = df.drop_duplicates(["fecha", "ticker"], keep="last")
    return df, avisos


def fusionar(existente, nuevo):
    """Une lo que ya había en precios_manual.csv con lo nuevo (lo nuevo manda). Ordena por acción y fecha."""
    if existente is None or existente.empty:
        base = pd.DataFrame(columns=["fecha", "ticker", "cierre"])
    else:
        base = existente[["fecha", "ticker", "cierre"]].copy()
        base["fecha"] = pd.to_datetime(base["fecha"], errors="coerce").dt.strftime("%Y-%m-%d")
    todo = pd.concat([base, nuevo], ignore_index=True)
    con_precio = todo[todo["cierre"].notna() & (todo["cierre"].astype(str) != "")]
    sin_precio = todo[todo["cierre"].isna() | (todo["cierre"].astype(str) == "")]
    con_precio = con_precio.drop_duplicates(["fecha", "ticker"], keep="last")
    # las filas vacías de la plantilla solo se conservan si esa acción aún no tiene ningún precio
    tickers_con_datos = set(con_precio["ticker"])
    sin_precio = sin_precio[~sin_precio["ticker"].isin(tickers_con_datos)]
    final = pd.concat([con_precio, sin_precio], ignore_index=True)
    return final.sort_values(["ticker", "fecha"]).reset_index(drop=True)
