import base64
import hashlib
import io
import os
import re
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components
import yfinance as yf

st.set_page_config(page_title="Monitor IPSA / IGPA", layout="wide")

st.title("Monitor de acciones chilenas")
st.caption("Información de carácter referencial; no constituye asesoría ni recomendación de inversión.")

URL_CMF = "https://www.cmfchile.cl/institucional/mercados/novedades_envio_fechas_eeff.php"


# ---------- Formato chileno: 1.234,56 ----------
def num_cl(v, dec=2):
    if pd.isna(v):
        return "—"
    s = f"{v:,.{dec}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def peso_signo_cl(v):
    if pd.isna(v):
        return "—"
    signo = "+" if v > 0 else "-" if v < 0 else ""
    return signo + "$" + num_cl(abs(v), 2)


def cargar_sectores():
    """sector -> (código de 3 letras, color) desde sectores.csv."""
    try:
        d = pd.read_csv("sectores.csv")
        return {r["sector"]: (str(r["codigo"]), str(r["color"])) for _, r in d.iterrows()}
    except Exception:
        return {}


@st.cache_data(ttl=86400)
def logo_de(nemo, codigo, color):
    """Imagen incrustada para cada acción: logos/NEMOTECNICO.png (o .svg/.jpg/.webp) si existe;
    si no, un cuadrado de color con el código de 3 letras de su sector."""
    for ext, mime in [("png", "image/png"), ("svg", "image/svg+xml"), ("jpg", "image/jpeg"),
                      ("jpeg", "image/jpeg"), ("webp", "image/webp")]:
        ruta = f"logos/{nemo}.{ext}"
        if os.path.exists(ruta):
            with open(ruta, "rb") as f:
                return f"data:{mime};base64," + base64.b64encode(f.read()).decode("ascii")
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64">'
        f'<rect x="3" y="3" width="58" height="58" rx="14" fill="{color}"/>'
        '<text x="32" y="40" font-family="Arial, Helvetica, sans-serif" font-size="21" font-weight="700" '
        f'letter-spacing="1" fill="white" text-anchor="middle">{codigo}</text></svg>'
    )
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")


def texto_monto(moneda, v):
    if pd.isna(v):
        return "—"
    if moneda == "USD":
        return "US$ " + num_cl(v, 8)
    if moneda == "EUR":
        return "€ " + num_cl(v, 8)
    return "$ " + num_cl(v, 2)


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
    precios_manual.csv: lo pegas o editas tú (por ejemplo, el cierre de fin de año).
    Si un mismo día está en los dos, manda el del proceso automático: se guarda después del cierre,
    mientras que lo pegado a mano puede haberse copiado con el mercado abierto."""
    partes = []
    for archivo, prioridad in [("historial.csv", 1), ("precios_manual.csv", 0), ("indices_manual.csv", 0)]:
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
def cargar_indices():
    try:
        return pd.read_csv("indices.csv")
    except Exception:
        return pd.DataFrame(columns=["ticker", "nombre"])


def color_signo(valor):
    if pd.isna(valor):
        return ""
    return "color: #1a7f4b" if valor > 0 else "color: #c0392b" if valor < 0 else ""


@st.cache_data(ttl=300)
def cargar_dividendos_csv():
    cols = ["ticker", "n_dividendo", "tipo", "moneda", "monto_original", "fecha_corte", "fecha_pago",
            "tipo_cambio", "fecha_tc", "monto_por_accion", "fuente"]
    try:
        d = pd.read_csv("dividendos.csv")
    except Exception:
        return pd.DataFrame(columns=cols)
    for c in cols:
        if c not in d:
            d[c] = None
    d["moneda"] = d["moneda"].fillna("CLP")
    for c in ["fecha_corte", "fecha_pago"]:
        d[c] = pd.to_datetime(d[c], errors="coerce")
    for c in ["monto_por_accion", "monto_original", "tipo_cambio"]:
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d["monto_original"] = d["monto_original"].fillna(d["monto_por_accion"])
    return d


@st.cache_data(ttl=21600)  # 6 horas
def cargar_dividendos_yahoo(ticker):
    d = yf.Ticker(ticker).dividends
    if d is None or d.empty:
        return pd.Series(dtype=float)
    d.index = pd.to_datetime(d.index).tz_localize(None)
    return d


@st.cache_data(ttl=21600)  # 6 horas
def cargar_dividendos_anio(tickers, desde):
    """Suma de dividendos por acción desde la fecha 'desde' (AAAA-MM-DD), según Yahoo (fecha ex-dividendo)."""

    def uno(t):
        try:
            d = yf.Ticker(t).dividends
            if d is None or d.empty:
                return t, 0.0
            d.index = pd.to_datetime(d.index).tz_localize(None)
            return t, float(d[d.index >= pd.Timestamp(desde)].sum())
        except Exception:
            return t, None

    with ThreadPoolExecutor(max_workers=6) as ex:
        return dict(ex.map(uno, tickers))


@st.cache_data(ttl=300)  # 5 minutos
def cargar_cotizaciones(tickers):
    """Precio actual y cierre anterior, como los muestra Yahoo en su sitio.
    Devuelve (cotizaciones, motivos): cotizaciones[ticker] = (precio, cierre_anterior o None) o None;
    motivos[ticker] explica por qué no hubo cotización."""

    def uno(t):
        motivos = []
        tk = yf.Ticker(t)
        # 1) cotización rápida
        try:
            fi = tk.fast_info
            ultimo, previo = fi["last_price"], fi["previous_close"]
            ultimo = float(ultimo) if ultimo is not None else None
            previo = float(previo) if previo is not None else None
            if ultimo and ultimo > 0:
                return t, (ultimo, previo if previo and previo > 0 else None), None
            motivos.append("fast_info sin precio")
        except Exception as e:
            motivos.append(f"fast_info: {type(e).__name__}")
        # 2) ficha completa (lo mismo que muestra la web de Yahoo)
        try:
            info = tk.info
            ultimo = info.get("regularMarketPrice") or info.get("currentPrice")
            previo = info.get("regularMarketPreviousClose") or info.get("previousClose")
            if ultimo and float(ultimo) > 0:
                previo = float(previo) if previo and float(previo) > 0 else None
                return t, (float(ultimo), previo), None
            motivos.append("info sin precio")
        except Exception as e:
            motivos.append(f"info: {type(e).__name__}")
        return t, None, "; ".join(motivos)

    with ThreadPoolExecutor(max_workers=6) as ex:
        resultados = list(ex.map(uno, tickers))
    cotizaciones = {t: cot for t, cot, _ in resultados}
    motivos = {t: m for t, _, m in resultados if m}
    return cotizaciones, motivos


def norm_rut(x):
    return re.sub(r"[^0-9Kk]", "", str(x)).upper()


@st.cache_data(ttl=3600)  # 1 hora
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
ahora = pd.Timestamp.now(tz="America/Santiago").tz_localize(None)
hoy = ahora.normalize()
# fin de semana, o lunes antes de abrir el mercado: aún no hay una rueda "de hoy"
sin_rueda_hoy = hoy.weekday() >= 5 or (hoy.weekday() == 0 and ahora.hour < 10)
ultima_rueda = hoy - pd.Timedelta(days=1)  # último día hábil anterior a hoy (en fin de semana, el viernes)
while ultima_rueda.weekday() >= 5:
    ultima_rueda -= pd.Timedelta(days=1)

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
indices = cargar_indices()

try:
    with st.spinner("Consultando precios actuales..."):
        cotizaciones, motivos_cot = cargar_cotizaciones(tuple(empresas["ticker"]) + tuple(indices["ticker"]))
except Exception as e:
    cotizaciones, motivos_cot = {}, {}
    st.warning(f"No se pudieron consultar los precios actuales: {type(e).__name__}")

div_csv = cargar_dividendos_csv()
ini_anio = pd.Timestamp(year=hoy.year, month=1, day=1)
div_ytd = (
    div_csv[(div_csv["fecha_corte"] >= ini_anio) & (div_csv["fecha_corte"] < hoy)]
    .groupby("ticker")["monto_por_accion"].sum()
)
try:
    with st.spinner("Consultando dividendos del año..."):
        div_yahoo = cargar_dividendos_anio(tuple(empresas["ticker"]), ini_anio.strftime("%Y-%m-%d"))
except Exception:
    div_yahoo = {}

fechas = {
    "WTD": hoy - pd.Timedelta(days=hoy.weekday() + 1),  # domingo anterior -> cierre del último día hábil de la semana pasada
    "1 semana": hoy - pd.Timedelta(days=7),
    "MTD": hoy.replace(day=1) - pd.Timedelta(days=1),
    "1 mes": hoy - pd.DateOffset(months=1),
    "YTD (precio)": pd.Timestamp(year=hoy.year - 1, month=12, day=31),
    "1 año": hoy - pd.DateOffset(years=1),
}

COLUMNAS = [
    "Empresa", "Nemotécnico", "Ticker", "Logo", "Precio", "Var. hoy ($)", "Var. hoy", "30 días", "WTD",
    "1 semana", "MTD", "1 mes", "YTD (precio)", "YTD (c/div.)", "1 año", "Sector",
]
columnas_pct = [
    "Var. hoy", "WTD", "1 semana", "MTD", "1 mes",
    "YTD (precio)", "YTD (c/div.)", "1 año",
]
columnas_num = ["Precio", "Var. hoy ($)"] + columnas_pct

filas = []
for _, e in empresas.iterrows():
    t = e["ticker"]
    c = cierre[t].dropna() if t in cierre.columns else pd.Series(dtype=float, index=pd.DatetimeIndex([]))
    cot = cotizaciones.get(t)
    if cot:
        ultimo, cierre_prev = cot  # precio actual y cierre anterior según Yahoo
        if not cierre_prev and len(c):
            anteriores = c[c.index < hoy]
            if not anteriores.empty and (hoy - anteriores.index[-1]).days <= 10:
                cierre_prev = anteriores.iloc[-1]  # Yahoo no lo entrega: último cierre guardado
    elif len(c) >= 2 and (hoy - c.index[-1]).days <= 5:
        ultimo, cierre_prev = c.iloc[-1], c.iloc[-2]  # sin cotización de Yahoo: último cierre guardado si es reciente
    else:
        ultimo = None
    if sin_rueda_hoy and len(c) >= 2 and c.index[-1] >= ultima_rueda:
        # mercado cerrado y ya tenemos el cierre de la última rueda: se usa ese y el anterior.
        # (La cotización de Yahoo en fin de semana puede venir atrasada, por ejemplo con el cierre del jueves.)
        ultimo, cierre_prev = c.iloc[-1], c.iloc[-2]
    elif ultimo is not None and sin_rueda_hoy and len(c) and (hoy - c.index[-1]).days <= 5 \
            and (cierre_prev is None or abs(ultimo - cierre_prev) < 1e-6) and abs(ultimo - c.iloc[-1]) >= 1e-6:
        cierre_prev = c.iloc[-1]  # aún no estaba guardado el cierre de la última rueda: se compara con el último guardado
    fila = {
        "Empresa": e["nombre"],
        "Nemotécnico": t.replace(".SN", "").upper(),
        "Ticker": t,
        "Sector": e["sector"],
    }
    if ultimo is None:
        filas.append(fila)  # la acción aparece igual, sin datos
        continue
    fila["Precio"] = ultimo
    ultimos_30 = c[c.index >= hoy - pd.Timedelta(days=30)]
    puntos = [round(float(x), 2) for x in ultimos_30.tolist()]
    if ultimos_30.empty or (ultimos_30.index[-1] < hoy and abs(float(ultimos_30.iloc[-1]) - float(ultimo)) > 1e-9):
        puntos.append(round(float(ultimo), 2))  # el precio actual cierra la línea
    fila["30 días"] = puntos if len(puntos) >= 2 else None
    if cierre_prev:
        fila["Var. hoy ($)"] = ultimo - cierre_prev
        fila["Var. hoy"] = (ultimo / cierre_prev - 1) * 100
    for nombre, fecha in fechas.items():
        fila[nombre] = retorno(c, fecha, ultimo, 10 if nombre == "YTD (precio)" else 6)
    base_ytd = valor_en(c, fechas["YTD (precio)"], 10)
    d_anio = div_yahoo.get(t)
    if t in div_ytd.index:  # lo cargado a mano en dividendos.csv tiene prioridad
        d_anio = float(div_ytd[t])
    if base_ytd and d_anio is not None:
        # rentabilidad total simple: precio actual + dividendos del año, sin reinversión
        fila["YTD (c/div.)"] = ((ultimo + d_anio) / base_ytd - 1) * 100
    else:
        fila["YTD (c/div.)"] = None
    filas.append(fila)

tabla = pd.DataFrame(filas, columns=COLUMNAS)
tabla = tabla[tabla["Ticker"].isin(emp_f["ticker"])]
sectores_map = cargar_sectores()
tabla["Logo"] = [
    logo_de(n, *sectores_map.get(sec, ("OTR", "#757575")))
    for n, sec in zip(tabla["Nemotécnico"], tabla["Sector"])
]
tabla[columnas_num] = tabla[columnas_num].astype(float).round(2)
tabla = tabla.sort_values("YTD (c/div.)", ascending=False).reset_index(drop=True)

# ---------- Pestañas ----------
es_admin = st.query_params.get("admin") == "importar"  # herramienta privada: agrega ?admin=importar al link
nombres_tabs = ["Rendimientos", "Gráficos", "Dividendos", "Calendario de resultados"]
if es_admin:
    nombres_tabs.append("Importar (admin)")
_tabs = st.tabs(nombres_tabs)
tab1, tab_g, tab2, tab3 = _tabs[:4]
tab_imp = _tabs[4] if es_admin else None

# ===== Pestaña 1: Rendimientos =====
with tab1:
    con_ytd = tabla.dropna(subset=["YTD (c/div.)"])

    # --- fila superior: tabla de índices | mejor del año | peor del año ---
    css_indices = (
        "<style>"
        ".idx-titulo{font-weight:600;margin-bottom:.4rem}"
        "table.idx{width:100%;border-collapse:collapse;font-size:.95rem}"
        "table.idx th{font-weight:500;opacity:.65;font-size:.78rem;text-align:right;padding:0 .4rem .3rem}"
        "table.idx td{padding:.35rem .4rem;text-align:right;white-space:nowrap;border-top:1px solid rgba(128,128,128,.25)}"
        "table.idx th:first-child,table.idx td:first-child{text-align:left}"
        "table.idx td.nom{font-weight:600}"
        "table.idx td.pos{color:#1a7f4b;font-weight:500}"
        "table.idx td.neg{color:#c0392b;font-weight:500}"
        "</style>"
    )

    def celda_pct(v):
        if pd.isna(v):
            return "<td>—</td>"
        clase = "pos" if v > 0 else "neg" if v < 0 else ""
        return f'<td class="{clase}">{pct_cl(v)}</td>'

    filas_html = ""
    for _, r in indices.iterrows():
        ti = r["ticker"]
        ci = cierre[ti].dropna() if ti in cierre.columns else pd.Series(dtype=float, index=pd.DatetimeIndex([]))
        cot_i = cotizaciones.get(ti)
        if sin_rueda_hoy and len(ci) and ci.index[-1] >= ultima_rueda:
            valor_i = ci.iloc[-1]  # mercado cerrado: cierre guardado de la última rueda
        elif cot_i:
            valor_i = cot_i[0]
        elif len(ci) and (hoy - ci.index[-1]).days <= 7:
            valor_i = ci.iloc[-1]  # sin cotización de Yahoo: último cierre guardado si es reciente
        else:
            valor_i = None
        sem_i = anual_i = None
        if valor_i is not None:
            sem_i = retorno(ci, fechas["WTD"], valor_i, 6)
            anual_i = retorno(ci, fechas["YTD (precio)"], valor_i, 10)
        valor_txt = "—" if valor_i is None else num_cl(valor_i, 2)
        filas_html += f'<tr><td class="nom">{r["nombre"]}</td><td>{valor_txt}</td>{celda_pct(sem_i)}{celda_pct(anual_i)}</tr>'
    html_indices = (
        css_indices
        + '<div class="idx-titulo">Principales índices de Chile</div>'
        + '<table class="idx"><thead><tr><th></th><th>Valor</th><th>Var. semanal</th><th>Var. anual</th></tr></thead>'
        + f"<tbody>{filas_html}</tbody></table>"
    )

    c_idx, c_mej, c_peor = st.columns([2.4, 1, 1])
    with c_idx:
        with st.container(border=True):
            if indices.empty:
                st.caption("Sin índices configurados.")
            else:
                st.markdown(html_indices, unsafe_allow_html=True)
    with c_mej:
        with st.container(border=True):
            if con_ytd.empty:
                st.metric("Mejor del año (c/div.)", "—")
            else:
                mejor = con_ytd.iloc[0]
                st.metric("Mejor del año (c/div.)", mejor["Nemotécnico"], pct_cl(mejor["YTD (c/div.)"]))
    with c_peor:
        with st.container(border=True):
            if con_ytd.empty:
                st.metric("Peor del año (c/div.)", "—")
            else:
                peor = con_ytd.iloc[-1]
                st.metric("Peor del año (c/div.)", peor["Nemotécnico"], pct_cl(peor["YTD (c/div.)"]))

    if tabla.empty:
        st.warning("No hay datos de precios para mostrar. Revisa los tickers en empresas.csv.")
    else:
        def color(valor):
            if pd.isna(valor):
                return ""
            return "color: #1a7f4b" if valor > 0 else "color: #c0392b" if valor < 0 else ""

        # el nemotécnico va como índice: queda fijo a la izquierda al desplazar la tabla (útil en el celular)
        vista = tabla.drop(columns=["Empresa", "Ticker"]).set_index("Nemotécnico")
        estilo = (
            vista.style
            .map(color, subset=["Var. hoy ($)"] + columnas_pct)
            .map(lambda v: "background-color: rgba(128, 128, 128, 0.16)", subset=["YTD (c/div.)"])
            .format({
                **{c: pct_cl for c in columnas_pct},
                "Var. hoy ($)": peso_signo_cl,
                "Precio": lambda v: "—" if pd.isna(v) else "$" + num_cl(v, 2),
            })
        )
        try:  # línea verde si el gráfico sube y roja si baja (en versiones antiguas de Streamlit no existe 'color')
            col_linea = st.column_config.LineChartColumn("30 días", width="small", color="auto")
        except TypeError:
            col_linea = st.column_config.LineChartColumn("30 días", width="small")
        st.dataframe(
            estilo, use_container_width=True, hide_index=False, height=520,
            column_config={
                "Logo": st.column_config.ImageColumn(" ", width="small"),
                "30 días": col_linea,
            },
        )

        sin_datos = tabla.loc[tabla["Precio"].isna(), "Nemotécnico"].tolist()
        if sin_datos:
            st.caption("Sin cotización disponible (revisa su ticker en empresas.csv): " + ", ".join(sin_datos))

        if not con_ytd.empty:
            st.subheader("Rendimiento en el año (con dividendos)")
            st.bar_chart(con_ytd.set_index("Nemotécnico")["YTD (c/div.)"])

        if es_admin:  # solo visible con ?admin=importar al final del link
            with st.expander("Diagnóstico (datos crudos)"):
                faltan_codigo = sorted(set(empresas["sector"]) - set(sectores_map))
                st.write(f"sectores.csv: {len(sectores_map)} sectores leídos")
                if faltan_codigo:
                    st.write("Sectores de empresas.csv sin código en sectores.csv (salen como OTR):", faltan_codigo)
                tk = st.selectbox("Acción", emp_f["ticker"], key="diag_tk", format_func=lambda x: x.replace(".SN", ""))
                st.write("Cotización según Yahoo (precio actual, cierre anterior):", cotizaciones.get(tk))
                if not cotizaciones.get(tk):
                    st.write("Motivo de la falta de cotización:", motivos_cot.get(tk, "sin información"))
                st.write("Dividendos del año sumados ($ por acción):", div_yahoo.get(tk))
                for archivo in ["historial.csv", "precios_manual.csv"]:
                    try:
                        n_filas = len(pd.read_csv(archivo).dropna(subset=["cierre"]))
                        st.write(f"{archivo}: {n_filas} filas con precio")
                    except Exception:
                        st.write(f"{archivo}: no se pudo leer (¿está en la raíz del repositorio con ese nombre exacto?)")
                if tk in cierre.columns and not cierre[tk].dropna().empty:
                    serie_tk = cierre[tk].dropna()
                    st.write(
                        f"Cierres guardados de esta acción: {len(serie_tk)} "
                        f"(del {serie_tk.index.min():%d-%m-%Y} al {serie_tk.index.max():%d-%m-%Y})"
                    )
                else:
                    st.write("Aún no hay cierres guardados para esta acción.")

                def leer_cierres(archivo):
                    try:
                        d = pd.read_csv(archivo)
                        d["fecha"] = pd.to_datetime(d["fecha"], errors="coerce")
                        d["cierre"] = pd.to_numeric(d["cierre"], errors="coerce")
                        return d.dropna(subset=["fecha", "ticker", "cierre"])
                    except Exception:
                        return pd.DataFrame(columns=["fecha", "ticker", "cierre"])

                bot_df, man_df = leer_cierres("historial.csv"), leer_cierres("precios_manual.csv")
                origen = pd.DataFrame({
                    "historial.csv (bot)": bot_df[bot_df["ticker"] == tk].set_index("fecha")["cierre"],
                    "precios_manual.csv": man_df[man_df["ticker"] == tk].set_index("fecha")["cierre"],
                })
                origen["Usado por la app"] = cierre[tk] if tk in cierre.columns else None
                st.write("Últimos cierres de esta acción, según cada archivo:")
                st.dataframe(origen.sort_index().tail(10))

                cruce = bot_df.merge(man_df, on=["fecha", "ticker"], suffixes=("_bot", "_manual"))
                cruce["dif %"] = ((cruce["cierre_bot"] / cruce["cierre_manual"] - 1) * 100).round(2)
                cruce = cruce[cruce["dif %"].abs() > 1].sort_values("fecha", ascending=False)
                st.write(f"Días con cierres que difieren más de 1% entre el bot y lo pegado a mano: {len(cruce)}")
                if len(cruce):
                    st.dataframe(cruce.head(20), hide_index=True)

# ===== Pestaña 2: Dividendos =====
with tab2:
    st.subheader("Dividendos pagados")
    nombre_d = st.selectbox("Acción", emp_f["nemo"], key="div_emp")
    t_d = emp_f.loc[emp_f["nemo"] == nombre_d, "ticker"].iloc[0]

    cot_d = cotizaciones.get(t_d)
    if cot_d:
        precio = cot_d[0]
    elif t_d in cierre.columns and not cierre[t_d].dropna().empty:
        precio = cierre[t_d].dropna().iloc[-1]
    else:
        precio = None

    div_t = div_csv[div_csv["ticker"] == t_d]
    if not div_t.empty:
        # dividendos cargados desde la Bolsa de Santiago, ya convertidos a pesos
        ult12 = div_t[
            (div_t["fecha_corte"] >= hoy - pd.DateOffset(years=1)) & (div_t["fecha_corte"] < hoy)
        ]["monto_por_accion"].sum()
        m1, m2 = st.columns(2)
        m1.metric("Dividendos últimos 12 meses ($ por acción)", num_cl(ult12, 2))
        if precio:
            m2.metric("Rentabilidad por dividendo (12 meses)", num_cl(ult12 / precio * 100, 2) + "%")
        pagados = div_t[
            (div_t["fecha_pago"] <= hoy) & (div_t["fecha_pago"] >= hoy - pd.DateOffset(years=3))
        ].sort_values(["fecha_pago", "n_dividendo"], ascending=False)
        vista_d = pd.DataFrame({
            "Fecha límite": pagados["fecha_corte"].dt.date,
            "Fecha de pago": pagados["fecha_pago"].dt.date,
            "Tipo": pagados["tipo"],
            "Monto": [texto_monto(m, v) for m, v in zip(pagados["moneda"], pagados["monto_original"])],
            "Tipo de cambio": pagados["tipo_cambio"].where(pagados["moneda"] != "CLP"),
            "$ por acción": pagados["monto_por_accion"],
        })
        st.dataframe(
            vista_d, hide_index=True, use_container_width=True,
            column_config={
                "Fecha límite": st.column_config.DateColumn(format="DD/MM/YYYY"),
                "Fecha de pago": st.column_config.DateColumn(format="DD/MM/YYYY"),
                "Tipo de cambio": st.column_config.NumberColumn(format="%.2f"),
                "$ por acción": st.column_config.NumberColumn(format="%.2f"),
            },
        )
    else:
        try:
            d = cargar_dividendos_yahoo(t_d)
        except Exception as e:
            d = pd.Series(dtype=float)
            st.warning(f"No se pudieron leer los dividendos: {e}")

        if d.empty:
            st.info("No hay dividendos registrados para esta acción.")
        else:
            ult12 = d[d.index >= hoy - pd.DateOffset(years=1)].sum()
            m1, m2 = st.columns(2)
            m1.metric("Dividendos últimos 12 meses ($ por acción)", num_cl(ult12, 2))
            if precio:
                m2.metric("Rentabilidad por dividendo (12 meses)", num_cl(ult12 / precio * 100, 2) + "%")
            recientes = d[d.index >= hoy - pd.DateOffset(years=3)].sort_index(ascending=False)
            vista_y = pd.DataFrame({
                "Fecha": recientes.index.date,
                "Dividendo por acción ($)": recientes.values.round(2),
            })
            st.dataframe(
                vista_y, hide_index=True, use_container_width=True,
                column_config={
                    "Fecha": st.column_config.DateColumn("Fecha", format="DD/MM/YYYY"),
                    "Dividendo por acción ($)": st.column_config.NumberColumn(format="%.2f"),
                },
            )
            st.caption("Los datos de Yahoo para Chile pueden estar incompletos o ignorar dividendos en dólares o euros.")

    st.subheader("Por repartir")
    pendientes = div_csv[
        div_csv["ticker"].isin(emp_f["ticker"]) & ((div_csv["fecha_pago"] > hoy) | div_csv["fecha_pago"].isna())
    ].sort_values("fecha_corte")
    if pendientes.empty:
        st.info("No hay dividendos por repartir cargados.")
    else:
        vista_p = pd.DataFrame({
            "Nemotécnico": pendientes["ticker"].str.replace(".SN", "", regex=False).str.upper(),
            "Tipo": pendientes["tipo"],
            "Monto": [texto_monto(m, v) for m, v in zip(pendientes["moneda"], pendientes["monto_original"])],
            "$ por acción": pendientes["monto_por_accion"],
            "Fecha límite": pendientes["fecha_corte"].dt.date,
            "Fecha de pago": pendientes["fecha_pago"].dt.date,
        })
        st.dataframe(
            vista_p, hide_index=True, use_container_width=True,
            column_config={
                "$ por acción": st.column_config.NumberColumn(format="%.2f"),
                "Fecha límite": st.column_config.DateColumn(format="DD/MM/YYYY"),
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

# ===== Pestaña privada: importar históricos pegados =====
if tab_imp is not None:
    from dividendos import completar, fusionar_dividendos, parsear_dividendos, tipo_cambio_mindicador
    from importador import fusionar, parsear_pegado

    @st.cache_data(ttl=86400)
    def tc_cacheado(moneda, fecha_iso):
        return tipo_cambio_mindicador(moneda, pd.Timestamp(fecha_iso).date())

    with tab_imp:
        st.subheader("Importar históricos pegados")
        st.caption(
            "Pega aquí las tablas de precios históricos copiadas desde la web. Cada acción empieza con una línea "
            "#NEMOTECNICO (por ejemplo #AGUAS-A) y debajo la tabla pegada tal cual. "
            "Del texto solo se guardan la fecha y el cierre; lo pegado no se almacena en ninguna parte."
        )
        texto = st.text_area("Tablas pegadas", height=320, key="pegado_hist")
        if texto.strip():
            mercado_abierto = hoy.weekday() < 5 and ahora.hour < 18
            nuevo, avisos = parsear_pegado(
                texto, set(empresas["ticker"]),
                ignorar_fecha=hoy.strftime("%Y-%m-%d") if mercado_abierto else None,
            )
            for a in avisos:
                st.warning(a)
            if nuevo.empty:
                st.info("Aún no encontré filas con fecha y precio.")
            else:
                resumen = (
                    nuevo.groupby("ticker")["fecha"]
                    .agg(filas="count", desde="min", hasta="max")
                    .reset_index()
                )
                resumen["ticker"] = resumen["ticker"].str.replace(".SN", "", regex=False)
                resumen = resumen.rename(columns={"ticker": "Nemotécnico"})
                st.dataframe(resumen, hide_index=True, use_container_width=True)
                try:
                    existente = pd.read_csv("precios_manual.csv", dtype={"fecha": str, "ticker": str})
                except Exception:
                    existente = None
                final = fusionar(existente, nuevo)
                st.download_button(
                    "Descargar precios_manual.csv actualizado",
                    final.to_csv(index=False).encode("utf-8"),
                    file_name="precios_manual.csv",
                    mime="text/csv",
                )
                st.caption("Sube ese archivo a GitHub reemplazando el actual (Add file, Upload files, Commit).")

        st.divider()
        st.subheader("Importar dividendos")
        st.caption(
            "Pega la tabla de dividendos de la Bolsa de Santiago bajo una línea #NEMOTECNICO. Los montos en US$ o euros "
            "se convierten a pesos con el dólar observado y el euro del Banco Central (vía mindicador.cl)."
        )
        opciones_tc = {"Fecha de pago": "pago", "5.º día hábil antes del pago": "pago_5", "Fecha límite": "limite"}
        etiqueta_tc = st.radio("Tipo de cambio de la…", list(opciones_tc), horizontal=True, key="base_tc")
        texto_div = st.text_area("Dividendos pegados", height=240, key="pegado_div")
        if texto_div.strip():
            nuevos_div, avisos_div = parsear_dividendos(texto_div, set(empresas["ticker"]))
            for a in avisos_div:
                st.warning(a)
            if nuevos_div.empty:
                st.info("Aún no encontré dividendos en el texto.")
            else:
                completos = completar(
                    nuevos_div, opciones_tc[etiqueta_tc], hoy,
                    lambda moneda, fecha: tc_cacheado(moneda, fecha.isoformat()),
                )
                st.dataframe(
                    pd.DataFrame({
                        "Nemotécnico": completos["ticker"].str.replace(".SN", "", regex=False),
                        "Tipo": completos["tipo"],
                        "Monto": [texto_monto(m, v) for m, v in zip(completos["moneda"], completos["monto_original"])],
                        "Fecha límite": completos["fecha_corte"],
                        "Fecha de pago": completos["fecha_pago"],
                        "Tipo de cambio": completos["tipo_cambio"].where(completos["moneda"] != "CLP"),
                        "Fecha del T.C.": completos["fecha_tc"],
                        "$ por acción": completos["monto_por_accion"],
                        "Estado": completos["estado"],
                    }),
                    hide_index=True, use_container_width=True,
                )
                try:
                    existente_div = pd.read_csv("dividendos.csv")
                except Exception:
                    existente_div = None
                final_div = fusionar_dividendos(existente_div, completos)
                st.download_button(
                    "Descargar dividendos.csv actualizado",
                    final_div.to_csv(index=False).encode("utf-8"),
                    file_name="dividendos.csv",
                    mime="text/csv",
                )
                st.caption("Sube ese archivo a GitHub reemplazando el actual (Add file, Upload files, Commit).")
