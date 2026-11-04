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

nuevas = []
for ticker in empresas["ticker"]:
    if ticker in ya_guardadas:
        continue
    try:
        precio = float(yf.Ticker(ticker).fast_info["last_price"])
    except Exception as e:
        print(f"Sin precio para {ticker}: {e}")
        continue
    if not precio > 0:
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
