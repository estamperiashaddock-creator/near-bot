# -*- coding: utf-8 -*-
"""Bot de valoración de NEAR -> mail semanal (GitHub Actions o local).
Uso local: python near_valuacion.py --force   (sin credenciales solo imprime y guarda ultimo_mail.html)
Variables: EMAIL_USER, EMAIL_PASS (contraseña de aplicación de Gmail), EMAIL_TO, FORCE=1
"""
import os, sys, json, time, re, hashlib, smtplib, statistics
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from html import escape
import datetime as dt
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import requests

# ============================ CONFIG ============================
NEAR_ID, NEAR_CHAIN = "near", "Near"
PEERS = {"solana": "Solana", "avalanche-2": "Avalanche", "aptos": "Aptos", "sui": "Sui"}
NOMBRE = "Roberto"
ESTADO = "estado.json"
ND = "no disponible"

INFLACION_PCT = 2.5  # VERIFICAR: dato de Gemini, sin confirmar con fuente oficial

# Completar a mano con fuente oficial. None = "no disponible"
# (etiqueta, % supply en insiders, % liberado en el año, inflación % anual)
TABLA = [
    ("2024", None, None, None),
    ("2025", None, None, None),
    ("2026 (hoy)", None, None, INFLACION_PCT),
    ("Fin 2026 (estimado)", None, None, INFLACION_PCT),
    ("2027 (estimado)", None, None, INFLACION_PCT),
]
PROX_UNLOCK = None  # ej: "15/11/2026 (1.2% del circulante)"

# Palabras clave para clasificar titulares (automático, puede equivocarse)
BUENAS = [r"\betf", r"\bapprov", r"\baprob", r"\baprueba", r"\bpartner", r"\balianza", r"\blaunch",
          r"\blanz", r"\blist(s|ed|ing)\b", r"\blistad", r"\bintegra", r"\bupgrade", r"\brecord",
          r"\bsurge", r"\brally", r"\badopt", r"\binvest", r"\bgrant", r"\bmainnet", r"\bbullish",
          r"\bsube\b", r"\bsuben\b"]
MALAS = [r"\bhack", r"\bexploit", r"\battack", r"\bataque", r"\blawsuit", r"\bdemanda", r"\bsec\b",
         r"\bdelist", r"\bdeslist", r"\boutage", r"\bhalt", r"\bfraud", r"\bestafa", r"\bban(s|ned)?\b",
         r"\bprohib", r"\bcrash", r"\bplunge", r"\bdrop", r"\bsell-?off", r"\bbearish", r"\breject",
         r"\brechaz", r"\binvestigat", r"\bcae\b", r"\bcaen\b", r"\bcaída"]
# Noticias "clave": las que justifican un mail aparte
CLAVE = [r"\betf", r"\bhack", r"\bexploit", r"\bsec\b", r"\blawsuit", r"\bdemanda", r"\bdelist",
         r"\bdeslist", r"\blist(s|ed|ing)\b", r"\blistad", r"\boutage", r"\bhalt", r"\bpartner",
         r"\balianza", r"\bapprov", r"\baprob", r"\baprueba"]
# ================================================================


def get(url, params=None):
    for _ in range(3):
        try:
            r = requests.get(url, params=params, timeout=30, headers={"User-Agent": "near-bot"})
            r.raise_for_status()
            return r.json()
        except Exception:
            time.sleep(3)
    return None


def serie(chain, tipo):
    d = get("https://api.llama.fi/overview/fees/" + chain,
            {"excludeTotalDataChartBreakdown": "true", "dataType": tipo})
    try:
        return [float(v or 0) for _, v in d["totalDataChart"]]
    except Exception:
        return None


def suma(s, n, skip=0):
    if not s or len(s) < n + skip:
        return None
    end = len(s) - skip
    return sum(s[end - n:end])


def chg(a, b):
    return (a / b - 1) * 100 if (a is not None and b) else None


def cuartil(vals, q):
    v = sorted(vals)
    pos = q * (len(v) - 1)
    i = int(pos)
    return v[i] + (v[min(i + 1, len(v) - 1)] - v[i]) * (pos - i)


def stables():
    d = get("https://stablecoins.llama.fi/stablecoincharts/" + NEAR_CHAIN)
    try:
        v = [float((x.get("totalCirculatingUSD") or x.get("totalCirculating"))["peggedUSD"]) for x in d]
        return v[-1], (v[-31] if len(v) > 31 else None)
    except Exception:
        return None, None


def usuarios():
    d = get("https://api.nearblocks.io/v1/charts")
    try:
        rows = sorted(d["charts"], key=lambda r: r["date"])
        k = next(k for k in rows[-1] if "active" in k.lower())
        v = [float(r[k]) for r in rows if r.get(k) is not None]
        return v[-1], (v[-8] if len(v) > 8 else None)
    except Exception:
        return None, None


def get_text(url, params=None):
    for _ in range(3):
        try:
            r = requests.get(url, params=params, timeout=30, headers={"User-Agent": "Mozilla/5.0 near-bot"})
            r.raise_for_status()
            return r.content
        except Exception:
            time.sleep(3)
    return None


def clasificar(t):
    s = sum(1 for k in BUENAS if re.search(k, t, re.I)) - sum(1 for k in MALAS if re.search(k, t, re.I))
    clave = any(re.search(k, t, re.I) for k in CLAVE)
    return ("🟢" if s > 0 else "🔴" if s < 0 else "⚪"), clave


def buscar_noticias(ahora):
    consultas = [
        {"q": '"NEAR Protocol" when:7d', "hl": "es-419", "gl": "AR", "ceid": "AR:es-419"},
        {"q": '"NEAR Protocol" OR "NEAR token" when:7d', "hl": "en-US", "gl": "US", "ceid": "US:en"},
    ]
    vistos, out = set(), []
    for q in consultas:
        raw = get_text("https://news.google.com/rss/search", q)
        if not raw:
            continue
        try:
            root = ET.fromstring(raw)
        except Exception:
            continue
        for it in root.iter("item"):
            titulo = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            fuente = (it.findtext("source") or "").strip()
            if not titulo or not link or not re.search(r"\bNEAR\b|Near Protocol", titulo):
                continue
            try:
                d = dt.datetime.utcfromtimestamp(parsedate_to_datetime(it.findtext("pubDate")).timestamp())
            except Exception:
                d = None
            if d and (ahora - d).days > 7:
                continue
            if fuente and titulo.endswith(" - " + fuente):
                titulo = titulo[:-len(fuente) - 3]
            nid = hashlib.md5(titulo.lower().encode("utf-8")).hexdigest()[:12]
            if nid in vistos:
                continue
            vistos.add(nid)
            emo, clave = clasificar(titulo)
            out.append({"id": nid, "titulo": titulo, "link": link, "fuente": fuente or "s/f",
                        "fecha": d.strftime("%d/%m") if d else "", "ts": d.timestamp() if d else 0,
                        "emo": emo, "clave": clave})
    out.sort(key=lambda n: (not n["clave"], -n["ts"]))
    return out


def html_noticias(items, nota=True):
    if not items:
        return "<p>No se encontraron noticias de NEAR en los últimos 7 días.</p>"
    out = ""
    for n in items[:5]:
        out += ('<p style="margin:6px 0">%s <a href="%s">%s</a> <small style="color:#666">(%s, %s%s)</small></p>'
                % (n["emo"], escape(n["link"], quote=True), escape(n["titulo"]), escape(n["fuente"]), n["fecha"],
                   " · IMPORTANTE" if n["clave"] else ""))
    if nota:
        out += ('<p style="font-size:12px;color:#666">(🟢 suena positiva, 🔴 suena negativa, ⚪ neutra o dudosa. '
                'La clasificación es automática por palabras clave y puede equivocarse: leé la nota antes de decidir.)</p>')
    return out


def enviar_mail(asunto, html):
    user, pw, to = os.environ.get("EMAIL_USER"), os.environ.get("EMAIL_PASS"), os.environ.get("EMAIL_TO")
    if not (user and pw and to):
        print("Sin credenciales: mail guardado en ultimo_mail.html.\nAsunto:", asunto)
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = asunto, user, to
    msg.attach(MIMEText("Abrí este mail en un lector que muestre HTML.", "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    user, pw, to = user.strip(), pw.replace(" ", "").strip(), to.strip()
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as s:
            s.login(user, pw)
            s.sendmail(user, [to], msg.as_string())
    except Exception as e:
        print("Falló el 465 (%s); pruebo el 587 con STARTTLS" % e)
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=60) as s:
            s.ehlo()
            s.starttls()
            s.ehlo()
            s.login(user, pw)
            s.sendmail(user, [to], msg.as_string())
    print("Mail enviado:", asunto)


# ---------- formato ----------
def usd(x):
    if x is None:
        return ND
    if abs(x) >= 1e9:
        return "$%.2f mil M" % (x / 1e9)
    if abs(x) >= 1e6:
        return "$%.1f M" % (x / 1e6)
    return "$%s" % format(x, ",.0f")


def px(x):
    return ND if x is None else "$%.2f" % x


def pct(x, d=1):
    return ND if x is None else ("%+." + str(d) + "f%%") % x


def pln(x, d=2):
    return ND if x is None else ("%." + str(d) + "f%%") % x


def fl(c):
    if c is None:
        return ""
    return "⬆️" if c > 2 else "⬇️" if c < -2 else "➡️"


def tabla_html(head, rows):
    th = "".join('<th style="text-align:left;padding:6px 10px;background:#f1f3f5">%s</th>' % h for h in head)
    tr = ""
    for r in rows:
        tr += "<tr>" + "".join('<td style="padding:6px 10px;border-bottom:1px solid #eee">%s</td>' % c for c in r) + "</tr>"
    return '<table style="border-collapse:collapse;font-size:14px;width:100%%"><tr>%s</tr>%s</table>' % (th, tr)


# ---------- puntaje ----------
def tend(c, m):
    if c is None:
        return m / 2
    return m if c > 10 else m * 0.7 if c > 0 else m * 0.4 if c > -10 else 0


def puntaje(desv, fees_c, stab_c, neta, commits):
    p = 0
    if desv is None:
        p += 20
    else:
        r = 1 + desv / 100
        p += 40 if r <= 0.6 else 32 if r <= 0.8 else 24 if r <= 1.0 else 14 if r <= 1.3 else 6 if r <= 1.6 else 0
    p += tend(fees_c, 20) + tend(stab_c, 15)
    p += 7.5 if neta is None else 15 if neta <= 0 else 10 if neta <= 2 else 5 if neta <= 4 else 0
    p += 5 if commits is None else 10 if commits > 0 else 0
    return int(round(p))


def veredicto(p, desv=None, bull=None, price=None):
    if p >= 65:
        v = ("ACUMULAR", "🟢", "NEAR cotiza con descuento frente a su valor justo estimado y los fundamentos acompañan.")
    elif p >= 40:
        v = ("MANTENER", "🟡", "NEAR está cerca de su valor justo; sin urgencia para comprar ni vender.")
    else:
        v = ("ESPERAR", "🔴", "NEAR cotiza caro o los fundamentos se debilitaron; conviene esperar un mejor momento.")
    if desv is not None and desv > 50:
        extra = " (incluso por encima del escenario optimista)" if (bull and price and price > bull) else ""
        return ("ESPERAR", "🔴", "Ojo: NEAR cotiza %.0f%% más caro que su valor justo estimado%s. No es buen momento para comprar más." % (desv, extra))
    if desv is not None and desv > 30 and v[0] == "ACUMULAR":
        return ("MANTENER", "🟡", "NEAR está algo más caro que su valor justo; no conviene sumar más por ahora.")
    return v


def cargar_estado():
    try:
        with open(ESTADO, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def main():
    ahora = dt.datetime.utcnow()
    force = os.environ.get("FORCE") == "1" or "--force" in sys.argv
    est = cargar_estado()

    # ---- mercado (CoinGecko) ----
    ids = ",".join([NEAR_ID] + list(PEERS))
    mk = get("https://api.coingecko.com/api/v3/coins/markets",
             {"vs_currency": "usd", "ids": ids, "price_change_percentage": "7d"})
    if not mk:
        print("ERROR: CoinGecko no respondió; no se manda mail.")
        sys.exit(1)
    m = {c["id"]: c for c in mk}
    n = m[NEAR_ID]
    price, mc, circ = n["current_price"], n["market_cap"], n["circulating_supply"]
    ch7 = n.get("price_change_percentage_7d_in_currency")

    det = get("https://api.coingecko.com/api/v3/coins/" + NEAR_ID,
              {"localization": "false", "tickers": "false", "market_data": "false",
               "community_data": "false", "developer_data": "true", "sparkline": "false"})
    try:
        commits = det["developer_data"]["commit_count_4_weeks"]
    except Exception:
        commits = None

    # ---- fees / revenue NEAR (DefiLlama) ----
    f = serie(NEAR_CHAIN, "dailyFees")
    rv = serie(NEAR_CHAIN, "dailyRevenue")
    f30, f30p = suma(f, 30), suma(f, 30, 30)
    fees_anual = f30 * 365 / 30 if f30 else None
    fees_c30 = chg(f30, f30p)
    fees_c7 = chg(suma(f, 7), suma(f, 7, 7))
    r30 = suma(rv, 30)
    rev_anual = r30 * 365 / 30 if r30 else None

    # ---- comparables P/S ----
    ps_list, ps_det = [], []
    for cid, chain in PEERS.items():
        s30 = suma(serie(chain, "dailyFees"), 30)
        if s30 and cid in m:
            ps = m[cid]["market_cap"] / (s30 * 365 / 30)
            ps_list.append(ps)
            ps_det.append((chain, ps))
    ps_near = mc / fees_anual if fees_anual else None
    ps_med = statistics.median(ps_list) if ps_list else None

    bear = base = bull = desv = None
    if fees_anual and len(ps_list) >= 2:
        bear, base, bull = [fees_anual * cuartil(ps_list, q) / circ for q in (0.25, 0.5, 0.75)]
        desv = (price / base - 1) * 100

    # ---- stablecoins, usuarios, quema ----
    st_now, st_old = stables()
    st_c = chg(st_now, st_old)
    us_now, us_old = usuarios()
    us_c = chg(us_now, us_old)
    burn = rev_anual / price / circ * 100 if rev_anual else None
    neta_hoy = INFLACION_PCT - burn if burn is not None else None

    # ---- puntaje y veredicto ----
    p = puntaje(desv, fees_c30, st_c, neta_hoy, commits)
    verd, emo, frase = veredicto(p, desv, bull, price)
    cambio = bool(est.get("veredicto")) and est["veredicto"] != verd

    # ---- alertas ----
    al = []
    if desv is not None and desv > 50:
        al.append("Precio más de 50% sobre el valor justo base: posible prima por especulación.")
    if desv is not None and desv < -20:
        al.append("Precio más de 20% bajo el valor justo base: posible oportunidad.")
    if fees_c30 is not None and fees_c30 < -30:
        al.append("Los fees cayeron más de 30% en 30 días.")
    if st_c is not None and st_c < -20:
        al.append("Las stablecoins en la red cayeron más de 20% en 30 días.")
    if commits == 0:
        al.append("Sin actividad en GitHub en las últimas 4 semanas.")
    if cambio:
        al.append("Cambió el veredicto: de %s a %s." % (est["veredicto"], verd))

    # ---- noticias ----
    noticias = buscar_noticias(ahora)
    vistas = est.get("noticias_vistas")
    primera_vez = vistas is None
    vistas = vistas or []
    clave_nuevas = [n for n in noticias if n["clave"] and n["id"] not in vistas]
    n_b = sum(1 for n in noticias if n["emo"] == "🟢")
    n_m = sum(1 for n in noticias if n["emo"] == "🔴")
    news_html = html_noticias(noticias)

    # ---- resumen de la semana ----
    if neta_hoy is None:
        q_txt = ND
    elif est.get("neta") is None:
        q_txt = "primera medición (todavía no hay una medición anterior para comparar)"
    else:
        dq = neta_hoy - est["neta"]
        q_txt = ("mejoró" if dq < -0.1 else "empeoró" if dq > 0.1 else "sin cambios") + " (vs la medición anterior)"
    sm = lambda t: ' <small style="color:#666">(' + t + ")</small>"
    us_extra = " ⚠️ caída muy brusca: puede ser un error de la fuente de datos" if (us_c is not None and us_c < -50) else ""
    resumen = [
        "💲 Precio: <b>%s</b> (%s en los últimos 7 días)" % (px(price), pct(ch7)),
        "📈 Fees: <b>%s</b> en 7 días" % pct(fees_c7) + sm("lo que los usuarios pagaron por usar la red; si sube, hay más uso"),
        "👥 Usuarios activos: <b>%s</b> en 7 días" % pct(us_c) + sm("cuentas que usaron la red; si sube, hay más gente" + us_extra),
        "🔥 Quema neta: <b>%s</b>" % q_txt + sm("monedas nuevas que se crean menos las que se destruyen"),
        "🔓 Unlocks: <b>%s</b>" % (PROX_UNLOCK or "sin fechas cargadas todavía") + sm("fechas en que se liberan monedas bloqueadas de insiders; si se liberan muchas pueden venderlas y presionar el precio"),
        "📰 Noticias: <b>%d</b> en 7 días (🟢 %d, 🔴 %d)" % (len(noticias), n_b, n_m) + sm("titulares de NEAR en medios; abajo están los links"),
        "🚨 Alertas: <b>%s</b>" % (str(len(al)) + " (ver abajo)" if al else "ninguna"),
    ]

    # ---- tablas ----
    ex = lambda t, e: t + '<br><small style="color:#666">(' + e + ")</small>"
    if desv is None:
        d_txt = ND
    elif desv >= 0:
        d_txt = "%s (hoy cotiza %.0f%% MÁS CARO que el valor justo base)" % (pct(desv), desv)
    else:
        d_txt = "%s (hoy cotiza %.0f%% MÁS BARATO que el valor justo base)" % (pct(desv), -desv)
    t_precio = [
        [ex("Precio hoy", "lo que cuesta NEAR ahora"), px(price)],
        [ex("Justo bajista", "escenario pesimista"), px(bear)],
        [ex("Justo base", "escenario normal"), px(base)],
        [ex("Justo alcista", "escenario optimista"), px(bull)],
        [ex("Desvío vs base", "cuánto se aleja el precio de hoy del valor justo normal"), d_txt],
    ]
    t_clave = [
        [ex("Fees anualizados", "lo que los usuarios pagaron por usar la red en los últimos 30 días, proyectado a un año; es como la facturación de NEAR"),
         usd(fees_anual), "%s %s en 30 días" % (fl(fees_c30), pct(fees_c30))],
        [ex("Quema neta", "monedas nuevas que crea la red por año menos las que destruye con las comisiones; si da positivo hay cada vez más monedas y cada una vale un poco menos"),
         pln(neta_hoy), "se destruye %s por año" % pln(burn)],
        [ex("Stablecoins en la red", "dólares digitales dentro de NEAR; más es más uso real"),
         usd(st_now), "%s %s en 30 días" % (fl(st_c), pct(st_c))],
        [ex("Usuarios activos", "cuentas distintas que usaron la red en el día"),
         ND if us_now is None else format(us_now, ",.0f"), "%s %s en 7 días" % (fl(us_c), pct(us_c))],
        [ex("Actividad de desarrolladores", "cantidad de actualizaciones al código en 4 semanas; indica si siguen trabajando en el proyecto"),
         ND if commits is None else str(commits), ""],
    ]
    comp = ("P/S de NEAR: <b>%s</b> vs mediana de %s: <b>%s</b><br><small style=\"color:#666\">(P/S = cuántas veces la facturación anual de la red entra en su valor de mercado. "
            "Más alto = más caro. Si NEAR está por encima de la mediana, el mercado la valora más que a sus competidoras por lo que factura.)</small>") % (
        ND if ps_near is None else "%.0f" % ps_near, "/".join(PEERS_NAMES()),
        ND if ps_med is None else "%.0f" % ps_med)
    filas = []
    for et, ins, lib, inf in TABLA:
        qm = burn if ("hoy" in et or "estimado" in et) else None
        nt = (inf - qm) if (inf is not None and qm is not None) else None
        filas.append([et, pln(ins), pln(lib), pln(inf), pln(qm), pln(nt)])
    t_ins = tabla_html(["Año", "% en insiders", "Liberado", "Inflación", "Quema", "Dilución neta"], filas)

    ahora_txt = ahora.strftime("%d/%m/%Y %H:%M UTC")
    html = """<div style="font-family:Arial,sans-serif;max-width:640px;margin:auto;color:#222;line-height:1.5">
<p>¡Hola %s! Este es tu resumen de NEAR 👋</p>
<div style="background:#fff8e1;border-radius:8px;padding:12px 16px">
<b>⚡ Lo que pasó en los últimos 7 días (de un vistazo)</b><br><small style="color:#666">(son 7 días corridos hasta hoy, no la semana de lunes a viernes)</small><br>%s<br><br><b>👉 En una frase:</b> %s</div>
<h2>%s Veredicto: %s (%d/100)</h2><p>%s</p>
<p style="font-size:12px;color:#666">(El puntaje de 0 a 100 mezcla si NEAR está cara o barata, el uso de la red, la quema de monedas y la actividad de los desarrolladores. Desde 65 = acumular, de 40 a 64 = mantener, menos de 40 = esperar. Si está muy cara frente a su valor justo, el veredicto baja aunque el puntaje sea alto.)</p>
<h3>💰 Precio vs valor justo</h3>%s
<h3>📊 Números clave</h3>%s
<h3>⚖️ Comparación con la competencia</h3><p>%s</p>
<h3>🏢 Insiders y liberación de tokens</h3>%s
<p style="font-size:12px;color:#666">Qué significa cada columna: <b>%% en insiders</b> (parte de todas las monedas que tienen el equipo y los inversores tempranos), <b>Liberado</b> (monedas que se desbloquean ese año y ya pueden vender), <b>Inflación</b> (monedas nuevas que crea la red por año), <b>Quema</b> (monedas que se destruyen con las comisiones), <b>Dilución neta</b> (inflación menos quema: si es positiva hay cada vez más monedas y cada una vale un poco menos). La inflación está cargada a mano (verificar). Lo marcado "estimado" es proyección y "no disponible" quiere decir que falta el dato.</p>
<h3>📰 Noticias de la semana</h3>%s
<h3>🚨 Alertas</h3><p>%s</p>
<p style="font-size:12px;color:#666">Fuentes: CoinGecko, DefiLlama, NearBlocks. Datos al %s. Valor justo = fees anualizados × P/S de SOL, AVAX, APT y SUI (cuartiles: bajista/base/alcista). Es una guía, no una recomendación de inversión.</p>
<p>¡Que tengas una excelente semana!</p></div>""" % (
        NOMBRE, "<br>".join(resumen), frase, emo, verd, p, frase,
        tabla_html(["", ""], t_precio), tabla_html(["Métrica", "Valor", "Tendencia"], t_clave),
        comp, t_ins, news_html,
        "<br>".join("• " + a for a in al) if al else "Esta semana no pasó nada fuera de lo normal ✅",
        ahora_txt)

    asunto = "%s NEAR | %s | Puntaje %d/100" % (emo, verd, p)
    if cambio:
        asunto = "⚠️ Cambió el veredicto – " + asunto

    # ---- guardar estado ----
    est.update({"veredicto": verd, "puntaje": p, "neta": neta_hoy, "fecha": ahora_txt,
                "noticias_vistas": (vistas + [n["id"] for n in noticias if n["id"] not in vistas])[-300:]})
    with open(ESTADO, "w", encoding="utf-8") as fh:
        json.dump(est, fh, ensure_ascii=False, indent=2)
    with open("ultimo_mail.html", "w", encoding="utf-8") as fh:
        fh.write(html)

    # ---- enviar ----
    semanal = force or cambio or ahora.weekday() == 0
    if semanal:
        enviar_mail(asunto, html)
    elif clave_nuevas and not primera_vez:
        n0 = clave_nuevas[0]
        asunto_n = "%s NEAR | Noticia importante: %s" % (n0["emo"] if n0["emo"] != "⚪" else "⚠️", n0["titulo"][:80])
        html_n = ('<div style="font-family:Arial,sans-serif;max-width:640px;margin:auto;color:#222;line-height:1.5">'
                  '<p>¡Hola %s! Salió una noticia importante sobre NEAR 📰</p>%s'
                  '<p style="font-size:12px;color:#666">Aviso automático. Veredicto actual: %s %s (%d/100). '
                  'No es una recomendación de inversión.</p></div>') % (NOMBRE, html_noticias(clave_nuevas), emo, verd, p)
        with open("ultimo_mail_noticia.html", "w", encoding="utf-8") as fh:
            fh.write(html_n)
        enviar_mail(asunto_n, html_n)
    else:
        print("Hoy no toca mail (no es lunes, no cambió el veredicto y no hay noticias clave nuevas). Veredicto:", verd, p)


def PEERS_NAMES():
    return ["SOL", "AVAX", "APT", "SUI"]


if __name__ == "__main__":
    main()
