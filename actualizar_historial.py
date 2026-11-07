"""Guarda el cierre del día de cada acción en historial.csv.
Lo ejecuta GitHub Actions cada día hábil después del cierre del mercado."""
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

ARCHIVO = "historial.csv"
MAX_SALTO = 0.40  # si el precio cambia más de 40% respecto al último guardado, se descarta y se avisa

hoy = datetime.now(ZoneInfo("America/Santiago")).date()
if hoy.weekday() >= 5:
    print("Fin de semana: no se guarda nada.")
    sys.exit(0)

empresas = pd.read_csv("empresas.csv")
if os.path.exists("indices.csv"):  # los índices (IPSA, IGPA) se guardan igual que las acciones
    empresas = pd.concat([empresas[["ticker"]], pd.read_csv("indices.csv")[["ticker"]]], ignore_index=True)
if os.path.exists(ARCHIVO):
    historial = pd.read_csv(ARCHIVO)
else:
    historial = pd.DataFrame(columns=["fecha", "ticker", "cierre"])

fecha_txt = hoy.isoformat()
ya_guardadas = set(historial.loc[historial["fecha"] == fecha_txt, "ticker"])
ultimo_guardado = (
    historial.sort_values("fecha").groupby("ticker")["cierre"].last().to_dict()
    if not historial.empty else {}
)

def precio_actual(ticker):
    """Precio actual: primero la consulta rápida y, si viene vacía, la ficha completa de Yahoo."""
    tk = yf.Ticker(ticker)
    try:
        v = tk.fast_info["last_price"]
        v = float(v) if v is not None else None
        if v and v > 0:
            return v
    except Exception:
        pass
    try:
        info = tk.info
        v = info.get("regularMarketPrice") or info.get("currentPrice")
        if v and float(v) > 0:
            return float(v)
    except Exception:
        pass
    return None


nuevas = []
for ticker in empresas["ticker"]:
    if ticker in ya_guardadas:
        continue
    precio = precio_actual(ticker)
    if precio is None:
        print(f"Sin precio para {ticker}")
        continue
    previo = ultimo_guardado.get(ticker)
    if previo and abs(precio / previo - 1) > MAX_SALTO:
        print(f"AVISO: {ticker} cambió más de {MAX_SALTO:.0%} ({previo} -> {precio}); no se guarda.")
        continue
    nuevas.append({"fecha": fecha_txt, "ticker": ticker, "cierre": round(precio, 4)})
    time.sleep(0.3)  # para no saturar a Yahoo

if nuevas:
    historial = pd.concat([historial, pd.DataFrame(nuevas)], ignore_index=True)
    historial = historial.sort_values(["fecha", "ticker"])
    historial.to_csv(ARCHIVO, index=False)
print(f"{len(nuevas)} cierres guardados para {fecha_txt}.")
