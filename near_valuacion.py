# -*- coding: utf-8 -*-
"""Bot de valoración de NEAR -> mail semanal (GitHub Actions o local).
Uso local: python near_valuacion.py --force   (sin credenciales solo imprime y guarda ultimo_mail.html)
Variables: EMAIL_USER, EMAIL_PASS (contraseña de aplicación de Gmail), EMAIL_TO, FORCE=1
"""
import os, sys, json, time, smtplib, statistics
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


def veredicto(p):
    if p >= 65:
        return "ACUMULAR", "🟢", "NEAR cotiza con descuento frente a su valor justo estimado y los fundamentos acompañan."
    if p >= 40:
        return "MANTENER", "🟡", "NEAR está cerca de su valor justo; sin urgencia para comprar ni vender."
    return "ESPERAR", "🔴", "NEAR cotiza caro o los fundamentos se debilitaron; conviene esperar un mejor momento."


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
    verd, emo, frase = veredicto(p)
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

    # ---- resumen de la semana ----
    if neta_hoy is None or est.get("neta") is None:
        q_txt = "primera medición" if neta_hoy is not None else ND
    else:
        dq = neta_hoy - est["neta"]
        q_txt = "mejoró" if dq < -0.1 else "empeoró" if dq > 0.1 else "sin cambios"
    resumen = [
        "💲 Precio: <b>%s</b> (%s en la semana)" % (px(price), pct(ch7)),
        "📈 Fees (7 días): <b>%s</b>" % pct(fees_c7),
        "👥 Usuarios activos (7 días): <b>%s</b>" % pct(us_c),
        "🔥 Quema neta: <b>%s</b>" % q_txt,
        "🔓 Unlocks: <b>%s</b>" % (PROX_UNLOCK or "sin datos cargados"),
        "🚨 Alertas: <b>%s</b>" % (str(len(al)) + " (ver abajo)" if al else "ninguna"),
    ]

    # ---- tablas ----
    t_precio = [
        ["Precio hoy", px(price)],
        ["Justo bajista", px(bear)],
        ["Justo base", px(base)],
        ["Justo alcista", px(bull)],
        ["Desvío vs base", pct(desv)],
    ]
    t_clave = [
        ["Fees anualizados", usd(fees_anual), "%s %s (30d)" % (fl(fees_c30), pct(fees_c30))],
        ["Quema neta (inflación − quema)", pln(neta_hoy), "quema %s" % pln(burn)],
        ["Stablecoins en la red", usd(st_now), "%s %s (30d)" % (fl(st_c), pct(st_c))],
        ["Usuarios activos", ND if us_now is None else format(us_now, ",.0f"), "%s %s (7d)" % (fl(us_c), pct(us_c))],
        ["Commits GitHub (4 sem)", ND if commits is None else str(commits), ""],
    ]
    comp = "P/S de NEAR: <b>%s</b> vs mediana %s: <b>%s</b>" % (
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
<b>⚡ Lo que pasó esta semana (de un vistazo)</b><br>%s<br><br><b>👉 En una frase:</b> %s</div>
<h2>%s Veredicto: %s (%d/100)</h2><p>%s</p>
<h3>💰 Precio vs valor justo</h3>%s
<h3>📊 Números clave</h3>%s
<h3>⚖️ Comparación con la competencia</h3><p>%s</p>
<h3>🏢 Insiders y liberación de tokens</h3>%s
<p style="font-size:12px;color:#666">Inflación cargada a mano (verificar). Lo marcado "estimado" es proyección. Los datos "no disponible" faltan en la fuente o no se cargaron.</p>
<h3>🚨 Alertas</h3><p>%s</p>
<p style="font-size:12px;color:#666">Fuentes: CoinGecko, DefiLlama, NearBlocks. Datos al %s. Valor justo = fees anualizados × P/S de SOL, AVAX, APT y SUI (cuartiles: bajista/base/alcista). Es una guía, no una recomendación de inversión.</p>
<p>¡Que tengas una excelente semana!</p></div>""" % (
        NOMBRE, "<br>".join(resumen), frase, emo, verd, p, frase,
        tabla_html(["", ""], t_precio), tabla_html(["Métrica", "Valor", "Tendencia"], t_clave),
        comp, t_ins,
        "<br>".join("• " + a for a in al) if al else "Esta semana no pasó nada fuera de lo normal ✅",
        ahora_txt)

    asunto = "%s NEAR | %s | Puntaje %d/100" % (emo, verd, p)
    if cambio:
        asunto = "⚠️ Cambió el veredicto – " + asunto

    # ---- guardar estado ----
    est.update({"veredicto": verd, "puntaje": p, "neta": neta_hoy, "fecha": ahora_txt})
    with open(ESTADO, "w", encoding="utf-8") as fh:
        json.dump(est, fh, ensure_ascii=False, indent=2)
    with open("ultimo_mail.html", "w", encoding="utf-8") as fh:
        fh.write(html)

    # ---- enviar ----
    if not (force or cambio or ahora.weekday() == 0):
        print("Hoy no toca mail (no es lunes y no cambió el veredicto). Veredicto:", verd, p)
        return
    user, pw, to = os.environ.get("EMAIL_USER"), os.environ.get("EMAIL_PASS"), os.environ.get("EMAIL_TO")
    if not (user and pw and to):
        print("Sin credenciales: mail guardado en ultimo_mail.html.\nAsunto:", asunto)
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = asunto, user, to
    msg.attach(MIMEText("Abrí este mail en un lector que muestre HTML.", "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pw)
        s.sendmail(user, [to], msg.as_string())
    print("Mail enviado:", asunto)


def PEERS_NAMES():
    return ["SOL", "AVAX", "APT", "SUI"]


if __name__ == "__main__":
    main()
