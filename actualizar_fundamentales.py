"""Guarda en fundamentales.csv las cifras base de cada empresa (según Yahoo) para la pestaña Ratios.
Lo ejecuta GitHub Actions una vez por semana (los balances cambian solo cada trimestre).
Si Yahoo falla para una acción, se conserva su fila anterior."""
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

ARCHIVO = "fundamentales.csv"
CAMPOS = {
    "moneda": "financialCurrency",
    "utilidad_neta": "netIncomeToCommon",
    "acciones": "sharesOutstanding",
    "eps": "trailingEps",
    "valor_libro": "bookValue",
    "roe": "returnOnEquity",
    "margen_neto": "profitMargins",
    "ventas": "totalRevenue",
    "ebitda": "ebitda",
    "deuda": "totalDebt",
    "caja": "totalCash",
}
CLAVES = ["utilidad_neta", "eps", "valor_libro", "roe", "margen_neto", "ventas", "ebitda", "deuda"]
COLUMNAS = ["ticker", "fecha_dato"] + list(CAMPOS) + ["div_12m"]


def leer(ticker):
    tk = yf.Ticker(ticker)
    info = None
    for intento in range(3):
        try:
            info = tk.info
            break
        except Exception as e:
            print(f"{ticker}: intento {intento + 1} falló ({type(e).__name__})")
            time.sleep(3 * (intento + 1))
    if not info:
        return None
    fila = {col: info.get(campo) for col, campo in CAMPOS.items()}
    if all(fila[c] is None for c in CLAVES):
        return None  # Yahoo no tiene fundamentales de esta acción
    try:  # dividendos pagados (fecha ex-dividendo) en los últimos 12 meses
        d = tk.dividends
        if d is None or d.empty:
            fila["div_12m"] = 0.0
        else:
            d.index = pd.to_datetime(d.index).tz_localize(None)
            desde = pd.Timestamp(datetime.now(ZoneInfo("America/Santiago")).date()) - pd.DateOffset(years=1)
            fila["div_12m"] = float(d[d.index >= desde].sum())
    except Exception:
        fila["div_12m"] = None
    return fila


def main():
    empresas = pd.read_csv("empresas.csv")
    previas = {}
    if os.path.exists(ARCHIVO):
        previo = pd.read_csv(ARCHIVO)
        previas = {r["ticker"]: r.to_dict() for _, r in previo.iterrows()}

    hoy = datetime.now(ZoneInfo("America/Santiago")).date().isoformat()
    filas, nuevas, conservadas, sin_datos = [], 0, 0, 0
    for t in empresas["ticker"]:
        fila = leer(t)
        if fila:
            fila.update({"ticker": t, "fecha_dato": hoy})
            filas.append(fila)
            nuevas += 1
        elif t in previas:
            filas.append(previas[t])
            conservadas += 1
        else:
            sin_datos += 1
        time.sleep(0.7)  # para no saturar a Yahoo
    pd.DataFrame(filas).reindex(columns=COLUMNAS).to_csv(ARCHIVO, index=False)
    print(f"Actualizadas: {nuevas} | conservadas de antes: {conservadas} | sin datos: {sin_datos}")


if __name__ == "__main__":
    main()
