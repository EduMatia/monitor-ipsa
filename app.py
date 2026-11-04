import io
import re
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components
import yfinance as yf

st.set_page_config(page_title="Monitor IPSA / IGPA", layout="wide")

st.title("Monitor de acciones chilenas")
st.caption(
    "Precio actual: Yahoo Finance (puede venir con retraso). Historial: cierres guardados por este dashboard. "
    "Fechas de resultados: CMF. Esto es informativo y no es una recomendación de inversión."
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


@st.cache_data(ttl=300)
def cargar_historial():
    """Cierres diarios propios.
    historial.csv: lo escribe el proceso automático (GitHub Actions).
    precios_manual.csv: lo editas tú (por ejemplo, el cierre de fin de año). Si coinciden, manda lo manual."""
    partes = []
    for archivo, prioridad in [("historial.csv", 0), ("precios_manual.csv", 1)]:
        try:
            d = pd.read_csv(archivo)
        except Exception:
            continue
        d["fecha"] = pd.to_datetime(d["fecha"], errors="coerce")
        d["cierre"] = pd.to_numeric(d["cierre"], errors="coerce")
        d = d.dropna(subset=["fecha", "ticker", "cierre"])
        d["prioridad"] = prioridad
        partes.append(d)
    if not partes:
        return pd.DataFrame()
    h = pd.concat(partes).sort_values("prioridad")
    h = h.drop_duplicates(["fecha", "ticker"], keep="last")
    return h.pivot(index="fecha", columns="ticker", values="cierre").sort_index()


@st.cache_data(ttl=300)
def cargar_dividendos_csv():
    cols = ["ticker", "tipo", "monto_por_accion", "fecha_corte", "fecha_pago", "fuente"]
    try:
        d = pd.read_csv("dividendos.csv")
    except Exception:
        return pd.DataFrame(columns=cols)
    for c in ["fecha_corte", "fecha_pago"]:
        d[c] = pd.to_datetime(d[c], errors="coerce")
    d["monto_por_accion"] = pd.to_numeric(d["monto_por_accion"], errors="coerce")
    return d


@st.cache_data(ttl=21600)  # 6 horas
def cargar_dividendos_yahoo(ticker):
    d = yf.Ticker(ticker).dividends
    if d is None or d.empty:
        return pd.Series(dtype=float)
    d.index = pd.to_datetime(d.index).tz_localize(None)
    return d


@st.cache_data(ttl=300)  # 5 minutos
def cargar_cotizaciones(tickers):
    """Precio actual y cierre anterior, igual que los muestra Yahoo en su sitio."""

    def uno(t):
        try:
            fi = yf.Ticker(t).fast_info
            ultimo = float(fi["last_price"])
            previo = float(fi["previous_close"])
            if ultimo > 0 and previo > 0:
                return t, (ultimo, previo)
        except Exception:
            pass
        return t, None

    with ThreadPoolExecutor(max_workers=6) as ex:
        return dict(ex.map(uno, tickers))


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


def valor_en(serie, fecha_ref, max_dias=6):
    """Último cierre guardado en fecha_ref o antes, pero solo si no es más antiguo que max_dias."""
    previo = serie.loc[:fecha_ref]
    if previo.empty:
        return None
    if (fecha_ref - previo.index[-1]).days > max_dias:
        return None
    return previo.iloc[-1]


def retorno(serie, fecha_ref, ultimo, max_dias=6):
    """Cambio porcentual entre el cierre de fecha_ref y el precio actual."""
    base = valor_en(serie, fecha_ref, max_dias)
    if base is None or base <= 0:
        return None
    return (ultimo / base - 1) * 100


# "hoy" según la hora de Chile
hoy = pd.Timestamp.now(tz="America/Santiago").tz_localize(None).normalize()

empresas = cargar_empresas()

# ---------- Filtros (valen para todas las pestañas) ----------
col1, col2 = st.columns(2)
indice = col1.selectbox("Acciones", ["IPSA", "Todas (IPSA + otras)"])
sector_sel = col2.multiselect("Sector (opcional)", sorted(empresas["sector"].unique()))

emp_f = empresas.copy()
if indice == "IPSA":
    emp_f = emp_f[emp_f["indice"] == "IPSA"]
if sector_sel:
    emp_f = emp_f[emp_f["sector"].isin(sector_sel)]
if emp_f.empty:
    st.warning("No hay empresas con esos filtros.")
    st.stop()
emp_f = emp_f.assign(nemo=emp_f["ticker"].str.replace(".SN", "", regex=False).str.upper())

# ---------- Precios y tabla de rendimientos ----------
cierre = cargar_historial()

try:
    with st.spinner("Consultando precios actuales..."):
        cotizaciones = cargar_cotizaciones(tuple(empresas["ticker"]))
except Exception:
    cotizaciones = {}

div_csv = cargar_dividendos_csv()
ini_anio = pd.Timestamp(year=hoy.year, month=1, day=1)
div_ytd = (
    div_csv[(div_csv["fecha_corte"] >= ini_anio) & (div_csv["fecha_corte"] <= hoy)]
    .groupby("ticker")["monto_por_accion"].sum()
)

fechas = {
    "Var. semana": hoy - pd.Timedelta(days=7),
    "MTD": hoy.replace(day=1) - pd.Timedelta(days=1),
    "1 mes": hoy - pd.DateOffset(months=1),
    "3 meses": hoy - pd.DateOffset(months=3),
    "YTD (precio)": pd.Timestamp(year=hoy.year - 1, month=12, day=31),
    "1 año": hoy - pd.DateOffset(years=1),
}

COLUMNAS = [
    "Empresa", "Nemotécnico", "Ticker", "Sector", "Índice", "Precio", "Var. hoy", "Var. semana",
    "MTD", "1 mes", "3 meses", "YTD (precio)", "YTD (c/div.)", "1 año",
]
columnas_pct = [
    "Var. hoy", "Var. semana", "MTD", "1 mes", "3 meses",
    "YTD (precio)", "YTD (c/div.)", "1 año",
]

filas = []
for _, e in empresas.iterrows():
    t = e["ticker"]
    c = cierre[t].dropna() if t in cierre.columns else pd.Series(dtype=float)
    cot = cotizaciones.get(t)
    if cot:
        ultimo, cierre_prev = cot  # precio actual y cierre anterior según Yahoo
    elif len(c) >= 2:
        ultimo, cierre_prev = c.iloc[-1], c.iloc[-2]
    else:
        continue
    fila = {
        "Empresa": e["nombre"],
        "Nemotécnico": t.replace(".SN", "").upper(),
        "Ticker": t,
        "Sector": e["sector"],
        "Índice": e["indice"],
        "Precio": ultimo,
        "Var. hoy": (ultimo / cierre_prev - 1) * 100,
    }
    for nombre, fecha in fechas.items():
        fila[nombre] = retorno(c, fecha, ultimo, 10 if nombre == "YTD (precio)" else 6)
    base_ytd = valor_en(c, fechas["YTD (precio)"], 10)
    if base_ytd:
        # rentabilidad total simple: precio actual + dividendos del año (por fecha de corte)
        fila["YTD (c/div.)"] = ((ultimo + div_ytd.get(t, 0.0)) / base_ytd - 1) * 100
    else:
        fila["YTD (c/div.)"] = None
    filas.append(fila)

tabla = pd.DataFrame(filas, columns=COLUMNAS)
tabla = tabla[tabla["Ticker"].isin(emp_f["ticker"])]
tabla[["Precio"] + columnas_pct] = tabla[["Precio"] + columnas_pct].astype(float).round(2)
tabla = tabla.sort_values("YTD (c/div.)", ascending=False).reset_index(drop=True)

# ---------- Pestañas ----------
tab1, tab_g, tab2, tab3 = st.tabs(
    ["Rendimientos", "Gráficos", "Dividendos", "Calendario de resultados"]
)

# ===== Pestaña 1: Rendimientos =====
with tab1:
    if tabla.empty:
        st.warning("No hay datos de precios para mostrar. Revisa los tickers en empresas.csv.")
    else:
        con_ytd = tabla.dropna(subset=["YTD (c/div.)"])
        if con_ytd.empty:
            st.info(
                "Aún no hay cierres de fin de año cargados. Completa la columna 'cierre' "
                "del archivo precios_manual.csv para ver el rendimiento del año."
            )
        else:
            mejor, peor = con_ytd.iloc[0], con_ytd.iloc[-1]
            m1, m2 = st.columns(2)
            m1.metric("Mejor del año (c/div.)", mejor["Nemotécnico"], pct_cl(mejor["YTD (c/div.)"]))
            m2.metric("Peor del año (c/div.)", peor["Nemotécnico"], pct_cl(peor["YTD (c/div.)"]))

        def color(valor):
            if pd.isna(valor):
                return ""
            return "color: #1a7f4b" if valor > 0 else "color: #c0392b" if valor < 0 else ""

        vista = tabla.drop(columns=["Empresa", "Ticker"])  # el nombre y el .SN son internos, no se muestran
        estilo = (
            vista.style
            .map(color, subset=columnas_pct)
            .format({**{c: pct_cl for c in columnas_pct}, "Precio": lambda v: "$" + num_cl(v, 2)})
        )
        st.dataframe(estilo, use_container_width=True, hide_index=True, height=520)

        st.caption(
            "YTD (c/div.) suma los dividendos cargados en dividendos.csv (por fecha de corte), sin reinversión. "
            "Los periodos sin cierres guardados suficientes aparecen como —."
        )
        if not con_ytd.empty:
            st.subheader("Rendimiento en el año (con dividendos)")
            st.bar_chart(con_ytd.set_index("Nemotécnico")["YTD (c/div.)"])

        with st.expander("Diagnóstico (datos crudos)"):
            tk = st.selectbox("Acción", emp_f["ticker"], key="diag_tk", format_func=lambda x: x.replace(".SN", ""))
            st.write("Cotización según Yahoo (precio actual, cierre anterior):", cotizaciones.get(tk))
            if tk in cierre.columns and not cierre[tk].dropna().empty:
                st.write("Últimos cierres guardados (historial propio):")
                st.dataframe(cierre[tk].dropna().tail(8))
            else:
                st.write("Aún no hay cierres guardados para esta acción.")

# ===== Pestaña 2: Dividendos =====
with tab2:
    st.subheader("Dividendos pagados (según Yahoo Finance)")
    nombre_d = st.selectbox("Acción", emp_f["nemo"], key="div_emp")
    t_d = emp_f.loc[emp_f["nemo"] == nombre_d, "ticker"].iloc[0]

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
        cot_d = cotizaciones.get(t_d)
        if cot_d:
            precio = cot_d[0]
        elif t_d in cierre.columns and not cierre[t_d].dropna().empty:
            precio = cierre[t_d].dropna().iloc[-1]
        else:
            precio = None
        if precio:
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
            "Nemotécnico": pendientes["ticker"].str.replace(".SN", "", regex=False).str.upper(),
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
        prox = prox.sort_values(["fecha", "ticker"])

        if prox.empty:
            st.info("No hay publicaciones en esa ventana de tiempo.")
        else:
            vista_c = pd.DataFrame({
                "Fecha": prox["fecha"].dt.date,
                "Faltan (días)": (prox["fecha"] - hoy).dt.days,
                "Nemotécnico": prox["ticker"].str.replace(".SN", "", regex=False).str.upper(),
                "Resultados": prox["resultados"],
            })
            st.dataframe(
                vista_c, hide_index=True, use_container_width=True, height=520,
                column_config={"Fecha": st.column_config.DateColumn("Fecha", format="DD/MM/YYYY")},
            )

        sin = base[base[list(PERIODOS)].isna().all(axis=1)]["ticker"].str.replace(".SN", "", regex=False).str.upper().tolist()
        if sin:
            st.caption("Sin fechas en la CMF: " + ", ".join(sin))

# ===== Pestaña Gráficos =====
with tab_g:
    st.subheader("Gráfico de una acción")
    sel_g = st.selectbox("Acción", emp_f["nemo"], key="graf_emp")
    simbolo_tv = emp_f.loc[emp_f["nemo"] == sel_g, "tv"].iloc[0]
    st.caption("Escala logarítmica: botón 'log' abajo a la derecha del gráfico. Los indicadores se agregan desde la barra superior.")

    html_g = f"""
    <div id="grafico" style="height:640px;"></div>
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
    components.html(html_g, height=660)
    st.caption("Gráficos por [TradingView](https://es.tradingview.com/)")
