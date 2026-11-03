import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
import yfinance as yf

st.set_page_config(page_title="Monitor IPSA / IGPA", layout="wide")

st.title("Monitor de acciones chilenas")
st.caption(
    "Datos de Yahoo Finance (pueden venir con retraso). "
    "Esto es informativo y no es una recomendación de inversión."
)


# ---------- 1. Cargar datos ----------
@st.cache_data(ttl=900)  # guarda los datos 15 minutos para no pedirlos a cada rato
def cargar_empresas():
    return pd.read_csv("empresas.csv")


@st.cache_data(ttl=900)
def cargar_precios(tickers):
    inicio = (pd.Timestamp.today() - pd.DateOffset(years=1, months=2)).strftime("%Y-%m-%d")
    datos = yf.download(list(tickers), start=inicio, auto_adjust=False, progress=False)
    cierre = datos["Close"]
    ajustado = datos["Adj Close"]  # precio que incluye dividendos
    cierre.index = pd.to_datetime(cierre.index).tz_localize(None)
    ajustado.index = pd.to_datetime(ajustado.index).tz_localize(None)
    return cierre, ajustado


def retorno(serie, fecha_ref):
    """Cambio porcentual entre el cierre de fecha_ref y el último precio."""
    previo = serie.loc[:fecha_ref]
    if previo.empty:
        return None
    return (serie.iloc[-1] / previo.iloc[-1] - 1) * 100


empresas = cargar_empresas()

with st.spinner("Descargando precios..."):
    cierre, ajustado = cargar_precios(tuple(empresas["ticker"]))

# ---------- 2. Calcular la tabla ----------
hoy = pd.Timestamp.today().normalize()
fechas = {
    "Var. semana": hoy - pd.Timedelta(days=7),
    "MTD": hoy.replace(day=1) - pd.Timedelta(days=1),
    "1 mes": hoy - pd.DateOffset(months=1),
    "3 meses": hoy - pd.DateOffset(months=3),
    "YTD (precio)": pd.Timestamp(year=hoy.year - 1, month=12, day=31),
    "1 año": hoy - pd.DateOffset(years=1),
}

filas = []
for _, e in empresas.iterrows():
    t = e["ticker"]
    if t not in cierre.columns:
        continue
    c = cierre[t].dropna()
    a = ajustado[t].dropna()
    if len(c) < 2:
        continue
    fila = {
        "Empresa": e["nombre"],
        "Ticker": t,
        "Sector": e["sector"],
        "Índice": e["indice"],
        "Precio": c.iloc[-1],
        "Var. hoy": (c.iloc[-1] / c.iloc[-2] - 1) * 100,
    }
    for nombre, fecha in fechas.items():
        fila[nombre] = retorno(c, fecha)
    fila["YTD (c/div.)"] = retorno(a, fechas["YTD (precio)"])
    filas.append(fila)

tabla = pd.DataFrame(filas)

if tabla.empty:
    st.error("No se pudieron cargar datos. Revisa tu conexión o los tickers en empresas.csv.")
    st.stop()

# ---------- 3. Filtros ----------
col1, col2 = st.columns(2)
indice = col1.selectbox("Índice", ["IPSA", "IGPA (todas)"])
sectores = sorted(tabla["Sector"].unique())
sector_sel = col2.multiselect("Sector (opcional)", sectores)

if indice == "IPSA":
    tabla = tabla[tabla["Índice"] == "IPSA"]
if sector_sel:
    tabla = tabla[tabla["Sector"].isin(sector_sel)]

tabla = tabla.sort_values("YTD (c/div.)", ascending=False).reset_index(drop=True)

# ---------- 4. Mejor y peor del año ----------
if not tabla.empty:
    mejor = tabla.iloc[0]
    peor = tabla.iloc[-1]
    m1, m2 = st.columns(2)
    m1.metric("Mejor del año (c/div.)", mejor["Empresa"], f"{mejor['YTD (c/div.)']:+.1f}%")
    m2.metric("Peor del año (c/div.)", peor["Empresa"], f"{peor['YTD (c/div.)']:+.1f}%")

# ---------- 5. Tabla con colores ----------
columnas_pct = [
    "Var. hoy", "Var. semana", "MTD", "1 mes", "3 meses",
    "YTD (precio)", "YTD (c/div.)", "1 año",
]


def color(valor):
    if pd.isna(valor):
        return ""
    return "color: #1a7f4b" if valor > 0 else "color: #c0392b" if valor < 0 else ""


estilo = (
    tabla.style
    .map(color, subset=columnas_pct)
    .format({c: "{:+.1f}%" for c in columnas_pct}, na_rep="—")
    .format({"Precio": "${:,.2f}"})
)
st.dataframe(estilo, use_container_width=True, hide_index=True, height=520)

# ---------- 6. Gráfico de barras ----------
st.subheader("Rendimiento en el año (con dividendos)")
st.bar_chart(tabla.set_index("Empresa")["YTD (c/div.)"])

# ---------- 7. Gráfico TradingView ----------
st.subheader("Gráfico de una acción")
nombre_sel = st.selectbox("Elige una empresa", tabla["Empresa"])
ticker_sel = tabla.loc[tabla["Empresa"] == nombre_sel, "Ticker"].iloc[0]
simbolo_tv = empresas.loc[empresas["ticker"] == ticker_sel, "tv"].iloc[0]

html = f"""
<div id="grafico" style="height:520px;"></div>
<script src="https://s3.tradingview.com/tv.js"></script>
<script>
new TradingView.widget({{
  "autosize": true,
  "symbol": "{simbolo_tv}",
  "interval": "D",
  "timezone": "America/Santiago",
  "theme": "light",
  "style": "1",
  "locale": "es",
  "container_id": "grafico"
}});
</script>
"""
components.html(html, height=540)
