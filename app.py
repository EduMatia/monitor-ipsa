import io
import re

import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components
import yfinance as yf

st.set_page_config(page_title="Monitor IPSA / IGPA", layout="wide")

st.title("Monitor de acciones chilenas")
st.caption(
    "Precios de Yahoo Finance (pueden venir con retraso) y fechas de la CMF. "
    "Esto es informativo y no es una recomendación de inversión."
)

URL_CMF = "https://www.cmfchile.cl/institucional/mercados/novedades_envio_fechas_eeff.php"


# ---------- Formato chileno: 1.234,56 ----------
def num_cl(v, dec=2):
    if pd.isna(v):
        return "—"
    s = f"{v:,.{dec}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def pct_cl(v):
    if pd.isna(v):
        return "—"
    return ("+" if v > 0 else "") + num_cl(v, 2) + "%"


# ---------- Cargar datos ----------
@st.cache_data(ttl=900)  # guarda los datos 15 minutos
def cargar_empresas():
    df = pd.read_csv("empresas.csv", dtype={"rut": str})
    df["rut"] = df["rut"].fillna("")
    return df


@st.cache_data(ttl=900)
def cargar_precios(tickers):
    inicio = (pd.Timestamp.today() - pd.DateOffset(years=1, months=2)).strftime("%Y-%m-%d")
    datos = yf.download(list(tickers), start=inicio, auto_adjust=False, progress=False)
    cierre = datos["Close"]
    ajustado = datos["Adj Close"]  # precio que incluye dividendos
    cierre.index = pd.to_datetime(cierre.index).tz_localize(None)
    ajustado.index = pd.to_datetime(ajustado.index).tz_localize(None)
    return cierre, ajustado


@st.cache_data(ttl=21600)  # 6 horas
def cargar_dividendos_yahoo(ticker):
    d = yf.Ticker(ticker).dividends
    if d is None or d.empty:
        return pd.Series(dtype=float)
    d.index = pd.to_datetime(d.index).tz_localize(None)
    return d


def norm_rut(x):
    return re.sub(r"[^0-9Kk]", "", str(x)).upper()


@st.cache_data(ttl=86400)  # 1 día
def cargar_fechas_cmf():
    r = requests.get(URL_CMF, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    for t in pd.read_html(io.StringIO(r.text)):
        if t.shape[1] >= 6 and any("RUT" in str(c).upper() for c in t.columns):
            t = t.iloc[:, :6].copy()
            t.columns = ["razon_social", "rut", "1T", "2T", "3T", "Anual"]
            t["rut_n"] = t["rut"].map(norm_rut)
            t = t[t["rut_n"] != ""].drop_duplicates("rut_n")
            for c in ["1T", "2T", "3T", "Anual"]:
                t[c] = pd.to_datetime(t[c], format="%d/%m/%Y", errors="coerce")
            return t[["rut_n", "1T", "2T", "3T", "Anual"]]
    raise ValueError("No encontré la tabla de fechas en la página de la CMF")


def retorno(serie, fecha_ref):
    """Cambio porcentual entre el cierre de fecha_ref y el último precio."""
    previo = serie.loc[:fecha_ref]
    if previo.empty:
        return None
    return (serie.iloc[-1] / previo.iloc[-1] - 1) * 100


# "hoy" según la hora de Chile
hoy = pd.Timestamp.now(tz="America/Santiago").tz_localize(None).normalize()

empresas = cargar_empresas()

# ---------- Filtros (valen para todas las pestañas) ----------
col1, col2 = st.columns(2)
indice = col1.selectbox("Índice", ["IPSA", "IGPA (todas)"])
sector_sel = col2.multiselect("Sector (opcional)", sorted(empresas["sector"].unique()))

emp_f = empresas.copy()
if indice == "IPSA":
    emp_f = emp_f[emp_f["indice"] == "IPSA"]
if sector_sel:
    emp_f = emp_f[emp_f["sector"].isin(sector_sel)]
if emp_f.empty:
    st.warning("No hay empresas con esos filtros.")
    st.stop()

# ---------- Precios y tabla de rendimientos ----------
try:
    with st.spinner("Descargando precios..."):
        cierre, ajustado = cargar_precios(tuple(empresas["ticker"]))
except Exception as e:
    cierre, ajustado = pd.DataFrame(), pd.DataFrame()
    st.warning(f"No se pudieron descargar los precios de Yahoo: {e}")

fechas = {
    "Var. semana": hoy - pd.Timedelta(days=7),
    "MTD": hoy.replace(day=1) - pd.Timedelta(days=1),
    "1 mes": hoy - pd.DateOffset(months=1),
    "3 meses": hoy - pd.DateOffset(months=3),
    "YTD (precio)": pd.Timestamp(year=hoy.year - 1, month=12, day=31),
    "1 año": hoy - pd.DateOffset(years=1),
}

COLUMNAS = [
    "Empresa", "Ticker", "Sector", "Índice", "Precio", "Var. hoy", "Var. semana",
    "MTD", "1 mes", "3 meses", "YTD (precio)", "YTD (c/div.)", "1 año",
]
columnas_pct = [
    "Var. hoy", "Var. semana", "MTD", "1 mes", "3 meses",
    "YTD (precio)", "YTD (c/div.)", "1 año",
]

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

tabla = pd.DataFrame(filas, columns=COLUMNAS)
tabla = tabla[tabla["Ticker"].isin(emp_f["ticker"])]
tabla[["Precio"] + columnas_pct] = tabla[["Precio"] + columnas_pct].astype(float).round(2)
tabla = tabla.sort_values("YTD (c/div.)", ascending=False).reset_index(drop=True)

# ---------- Pestañas ----------
tab1, tab2, tab3 = st.tabs(["Rendimientos", "Dividendos", "Calendario de resultados"])

# ===== Pestaña 1: Rendimientos =====
with tab1:
    if tabla.empty:
        st.warning("No hay datos de precios para mostrar. Revisa los tickers en empresas.csv.")
    else:
        mejor, peor = tabla.iloc[0], tabla.iloc[-1]
        m1, m2 = st.columns(2)
        m1.metric("Mejor del año (c/div.)", mejor["Empresa"], pct_cl(mejor["YTD (c/div.)"]))
        m2.metric("Peor del año (c/div.)", peor["Empresa"], pct_cl(peor["YTD (c/div.)"]))

        def color(valor):
            if pd.isna(valor):
                return ""
            return "color: #1a7f4b" if valor > 0 else "color: #c0392b" if valor < 0 else ""

        estilo = (
            tabla.style
            .map(color, subset=columnas_pct)
            .format({c: pct_cl for c in columnas_pct})
            .format({"Precio": lambda v: "$" + num_cl(v, 2)})
        )
        st.dataframe(estilo, use_container_width=True, hide_index=True, height=520)

        st.subheader("Rendimiento en el año (con dividendos)")
        st.bar_chart(tabla.set_index("Empresa")["YTD (c/div.)"])

        st.subheader("Gráfico de una acción")
        nombre_sel = st.selectbox("Elige una empresa", tabla["Empresa"], key="graf_emp")
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

# ===== Pestaña 2: Dividendos =====
with tab2:
    st.subheader("Dividendos pagados (según Yahoo Finance)")
    nombre_d = st.selectbox("Empresa", emp_f["nombre"], key="div_emp")
    t_d = emp_f.loc[emp_f["nombre"] == nombre_d, "ticker"].iloc[0]

    try:
        d = cargar_dividendos_yahoo(t_d)
    except Exception as e:
        d = pd.Series(dtype=float)
        st.warning(f"No se pudieron leer los dividendos: {e}")

    if d.empty:
        st.info("Yahoo no tiene dividendos registrados para esta acción.")
    else:
        ult12 = d[d.index >= hoy - pd.DateOffset(years=1)].sum()
        m1, m2 = st.columns(2)
        m1.metric("Dividendos últimos 12 meses ($ por acción)", num_cl(ult12, 2))
        if t_d in cierre.columns and not cierre[t_d].dropna().empty:
            precio = cierre[t_d].dropna().iloc[-1]
            m2.metric("Rentabilidad por dividendo (12 meses)", num_cl(ult12 / precio * 100, 2) + "%")

        recientes = d[d.index >= hoy - pd.DateOffset(years=3)].sort_index(ascending=False)
        vista_d = pd.DataFrame({
            "Fecha": recientes.index.date,
            "Dividendo por acción ($)": recientes.values.round(2),
        })
        st.dataframe(
            vista_d, hide_index=True, use_container_width=True,
            column_config={
                "Fecha": st.column_config.DateColumn("Fecha", format="DD/MM/YYYY"),
                "Dividendo por acción ($)": st.column_config.NumberColumn(format="%.2f"),
            },
        )
        st.caption("Los datos de Yahoo para Chile pueden estar incompletos. Verifica con los hechos esenciales de la empresa.")

    st.subheader("Por repartir")
    try:
        div = pd.read_csv("dividendos.csv")
        for c in ["fecha_corte", "fecha_pago"]:
            div[c] = pd.to_datetime(div[c], errors="coerce")
        div = div.merge(empresas[["ticker", "nombre"]], on="ticker", how="left")
        div = div[div["ticker"].isin(emp_f["ticker"])]
        pendientes = div[(div["fecha_pago"] >= hoy) | (div["fecha_corte"] >= hoy)]
        pendientes = pendientes.sort_values("fecha_corte")
    except Exception:
        pendientes = pd.DataFrame()

    if pendientes.empty:
        st.info(
            "No hay dividendos por repartir cargados. Se agregan a mano en el archivo "
            "dividendos.csv, a partir de los hechos esenciales de cada empresa."
        )
    else:
        vista_p = pd.DataFrame({
            "Empresa": pendientes["nombre"],
            "Tipo": pendientes["tipo"],
            "Monto por acción ($)": pendientes["monto_por_accion"],
            "Fecha de corte": pendientes["fecha_corte"].dt.date,
            "Fecha de pago": pendientes["fecha_pago"].dt.date,
            "Fuente": pendientes["fuente"],
        })
        st.dataframe(
            vista_p, hide_index=True, use_container_width=True,
            column_config={
                "Fecha de corte": st.column_config.DateColumn(format="DD/MM/YYYY"),
                "Fecha de pago": st.column_config.DateColumn(format="DD/MM/YYYY"),
            },
        )

# ===== Pestaña 3: Calendario de resultados =====
with tab3:
    st.subheader("Próximas publicaciones de estados financieros")
    st.caption("Fechas informadas por cada empresa a la CMF (Fechas de divulgación de estados financieros).")

    PERIODOS = {
        "1T": "1T (marzo)",
        "2T": "2T (junio)",
        "3T": "3T (septiembre)",
        "Anual": "Anual (diciembre)",
    }

    try:
        fechas_cmf = cargar_fechas_cmf()
    except Exception as e:
        fechas_cmf = None
        st.warning(
            "No pude leer la página de la CMF en este momento. "
            f"Puedes consultarla directamente aquí: {URL_CMF}  (detalle: {e})"
        )

    if fechas_cmf is not None:
        base = emp_f.assign(rut_n=emp_f["rut"].map(norm_rut)).merge(fechas_cmf, on="rut_n", how="left")
        largo = base.melt(
            id_vars=["nombre", "ticker"],
            value_vars=list(PERIODOS),
            var_name="periodo",
            value_name="fecha",
        ).dropna(subset=["fecha"])
        anio = largo["fecha"].dt.year - (largo["periodo"] == "Anual").astype(int)
        largo["resultados"] = largo["periodo"].map(PERIODOS) + " " + anio.astype(str)

        ventanas = {
            "Próximos 30 días": 30,
            "Próximos 60 días": 60,
            "Próximos 90 días": 90,
            "Todo lo que viene": None,
        }
        sel = st.selectbox("Ventana de tiempo", list(ventanas), key="ventana_cal")

        prox = largo[largo["fecha"] >= hoy].copy()
        if ventanas[sel]:
            prox = prox[prox["fecha"] <= hoy + pd.Timedelta(days=ventanas[sel])]
        prox = prox.sort_values(["fecha", "nombre"])

        if prox.empty:
            st.info("No hay publicaciones en esa ventana de tiempo.")
        else:
            vista_c = pd.DataFrame({
                "Fecha": prox["fecha"].dt.date,
                "Faltan (días)": (prox["fecha"] - hoy).dt.days,
                "Empresa": prox["nombre"],
                "Ticker": prox["ticker"].str.replace(".SN", "", regex=False),
                "Resultados": prox["resultados"],
            })
            st.dataframe(
                vista_c, hide_index=True, use_container_width=True, height=520,
                column_config={"Fecha": st.column_config.DateColumn("Fecha", format="DD/MM/YYYY")},
            )

        sin = base[base[list(PERIODOS)].isna().all(axis=1)]["nombre"].tolist()
        if sin:
            st.caption("Sin fechas en la CMF: " + ", ".join(sin))
