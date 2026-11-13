#!/usr/bin/env python3
"""Busca passagens no Google Flights (fast-flights), filtra esperas longas,
grava monitor/resultado.json e (opcional) envia e-mail com as 5 melhores."""
import json, os, sys, smtplib, traceback
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path

HERE = Path(__file__).parent
BRT = timezone(timedelta(hours=-3))


def dt(sf):
    d, t = sf.departure, sf.arrival
    return (datetime(*d.date, *d.time), datetime(*t.date, *t.time))


def esperas(segs):
    out = []
    for a, b in zip(segs, segs[1:]):
        out.append((dt(b)[0] - dt(a)[1]).total_seconds() / 3600)
    return out


def r(n):
    return f"{n:,.0f}".replace(",", ".")


def hm(h):
    m = int(round(h * 60))
    return f"{m // 60}h{m % 60:02d}"


def detalhe_escalas(segs):
    out = []
    for a, b in zip(segs, segs[1:]):
        chega, sai = dt(a)[1], dt(b)[0]
        h = (sai - chega).total_seconds() / 3600
        ap = a.to_airport
        out.append(f"{ap.code} – {ap.name}: chega {chega.strftime('%d/%m %H:%M')}, "
                   f"sai {sai.strftime('%d/%m %H:%M')} (horário local) – espera {hm(h)}")
    return out


SITES = {
    "latam": "https://www.latamairlines.com/br/pt",
    "gol": "https://www.voegol.com.br",
    "azul": "https://www.voeazul.com.br",
    "american": "https://www.aa.com.br",
    "avianca": "https://www.avianca.com/br/pt",
    "copa": "https://www.copaair.com/pt-br",
    "united": "https://www.united.com/pt/br",
    "delta": "https://pt.delta.com",
    "tap": "https://www.flytap.com/pt-br",
}


def sites(airlines):
    return [f"{a}: {SITES[k]}" for a in airlines for k in SITES if k in a.lower()]


def fmt(f):
    segs = f.flights
    ini, fim = dt(segs[0])[0], dt(segs[-1])[1]
    w = esperas(segs)
    return {
        "escalas_detalhe": detalhe_escalas(segs),
        "sites": sites(f.airlines),
        "companhias": f.airlines,
        "saida": ini.strftime("%d/%m %H:%M"),
        "chegada": fim.strftime("%d/%m %H:%M"),
        "hora_saida": ini.hour,
        "paradas": len(segs) - 1,
        "escalas": [f"{s.to_airport.code}: {h:.1f}h" for s, h in zip(segs, w)],
        "espera_max_h": round(max(w), 1) if w else 0,
        "duracao_h": round((fim - ini).total_seconds() / 3600, 1),
        "preco": f.price,
    }


def periodo(h):
    return "madrugada" if h < 6 else "manhã" if h < 12 else "tarde" if h < 18 else "noite"


def ler_voos(html):
    """Lê os voos da página do Google Flights: as DUAS listas ("melhores" e "outros").

    O leitor da biblioteca só devolvia a segunda lista e deixava de fora justamente
    os mais baratos (ex.: Copa), então o "menor preço" saía bem acima do real.
    """
    from types import SimpleNamespace as NS
    from selectolax.lexbor import LexborHTMLParser
    no = LexborHTMLParser(html).css_first(r"script.ds\:1")
    if no is None:
        raise RuntimeError("página do Google sem os dados dos voos")
    data = no.text().split("data:", 1)[1].rsplit(",", 1)[0]
    if data.endswith("errorHasStatus: true"):
        raise RuntimeError("o Google devolveu erro")
    p = json.loads(data)

    def hora(v):
        v = list(v or []) + [None, None]
        return (v[0] or 0, v[1] or 0)

    voos = []
    for sec in p[2:4]:
        for k in ((sec[0] if sec else None) or []):
            tarifa = k[1][0] if k[1] else []
            preco = tarifa[1] if tarifa and len(tarifa) > 1 else None
            if not preco:
                continue  # itinerário sem tarifa disponível
            v = k[0]
            segs = [NS(from_airport=NS(code=s[3], name=s[4]), to_airport=NS(code=s[6], name=s[5]),
                       departure=NS(date=tuple(s[20]), time=hora(s[8])),
                       arrival=NS(date=tuple(s[21]), time=hora(s[10]))) for s in v[2]]
            voos.append(NS(price=preco, airlines=list(v[1]), flights=segs))
    return voos


def sem_repetir(voos):
    """Mesmo voo nas duas listas: fica o de menor preço."""
    m = {}
    for x in voos:
        k = (x["saida"], x["chegada"], tuple(x["companhias"]))
        if k not in m or x["preco"] < m[k]["preco"]:
            m[k] = x
    return list(m.values())


def buscar(cfg):
    import time
    from fast_flights import FlightQuery, Passengers, create_query
    from fast_flights.fetcher import fetch_flights_html

    def get_flights(q, tentativas=6):
        # o Google responde com erro de forma intermitente; tenta de novo
        for i in range(tentativas):
            try:
                voos = ler_voos(fetch_flights_html(q))
                if not voos:
                    raise RuntimeError("nenhum voo com preço")
                return voos
            except Exception:
                if i == tentativas - 1:
                    raise
                time.sleep(4 + 3 * i)
    o, d = cfg["origem"], cfg["destino"]
    # preço por pessoa: consulta com 1 adulto (ida e volta)
    q = create_query(
        flights=[FlightQuery(date=cfg["ida"], from_airport=o, to_airport=d),
                 FlightQuery(date=cfg["volta"], from_airport=d, to_airport=o)],
        trip="round-trip", passengers=Passengers(adults=1),
        language="pt-BR", currency="BRL")
    ida = sem_repetir([fmt(f) for f in get_flights(q)])
    # voos de volta (só para checar horários e esperas)
    qv = create_query(
        flights=[FlightQuery(date=cfg["volta"], from_airport=d, to_airport=o)],
        trip="one-way", passengers=Passengers(adults=1),
        language="pt-BR", currency="BRL")
    volta = sem_repetir([fmt(f) for f in get_flights(qv)])
    lim = cfg["espera_max_horas"]
    ida_ok = sorted([x for x in ida if x["espera_max_h"] < lim], key=lambda x: x["preco"])
    volta_ok = sorted([x for x in volta if x["espera_max_h"] < lim], key=lambda x: x["preco"])
    # 5 melhores com horários variados: 1 por período, depois completa pelo preço
    top, usados = [], set()
    for x in ida_ok:
        p = periodo(x["hora_saida"])
        if p not in usados:
            top.append(x); usados.add(p)
    for x in ida_ok:
        if len(top) >= 5: break
        if x not in top: top.append(x)
    top = sorted(top[:5], key=lambda x: x["preco"])
    try:
        link = q.url()
    except Exception:
        link = f"https://www.google.com/travel/flights?hl=pt-BR&curr=BRL&q=Voos%20{o}%20para%20{d}%20{cfg['ida']}%20{cfg['volta']}"
    return {"total_ida": len(ida), "total_volta": len(volta),
            "link_google_flights": link,
            "ida": top, "volta": volta_ok[:3]}


def corpo(cfg, res):
    pax = cfg["adultos"]
    L = [f"Busca {cfg['origem']}→{cfg['destino']} {cfg['ida']} a {cfg['volta']} · {pax} adultos",
         f"Atualizado: {res['atualizado']} (Brasília)", ""]
    na_meta = [x for x in res["ida"] if x["preco"] <= cfg["meta_por_pessoa"]]
    if na_meta:
        L.append(f"*** {len(na_meta)} opção(ões) NA META (até R$ {r(cfg['meta_por_pessoa'])} por pessoa) ***\n")
    L.append("IDA (preço de ida e volta por pessoa, segundo o Google Flights):")
    for i, x in enumerate(res["ida"], 1):
        L.append(f"{i}. R$ {r(x['preco']*2)} por casal (R$ {r(x['preco'])}/pessoa · total {pax}: R$ {r(x['preco']*pax)}) · {', '.join(x['companhias'])}\n"
                 f"   sai {x['saida']} chega {x['chegada']} · {x['paradas']} parada(s)")
        for e in x["escalas_detalhe"] or ["voo direto"]:
            L.append(f"   • {e}")
        for s_ in x["sites"]:
            L.append(f"   Conferir: {s_}")
    L.append("\nVOLTA (opções com espera dentro do limite):")
    for x in res["volta"]:
        L.append(f"- {', '.join(x['companhias'])} · sai {x['saida']} chega {x['chegada']}")
        for e in x["escalas_detalhe"] or ["voo direto"]:
            L.append(f"   • {e}")
    L.append("\nANTES DE COMPRAR, CONFIRME DIRETO NA COMPANHIA AÉREA E NO BOOKING:")
    L.append("- bagagem despachada incluída? taxa de bagagem e de marcação de assento")
    L.append("- preço final com taxas para os " + str(pax) + " adultos")
    L.append("- regras de remarcação/cancelamento e se as conexões são no mesmo bilhete")
    L.append("Google Flights: " + res.get("link_google_flights", ""))
    L.append("Booking.com voos: https://www.booking.com/flights")
    return "\n".join(L)


def enviar(cfg, res):
    user, pw = os.getenv("GMAIL_USER"), os.getenv("GMAIL_APP_PASSWORD")
    if not (user and pw):
        return "e-mail não configurado (faltam os segredos GMAIL_USER e GMAIL_APP_PASSWORD)"
    menor = min((x["preco"] for x in res["ida"]), default=None)
    na_meta = menor is not None and menor <= cfg["meta_por_pessoa"]
    assunto = ("URGENTE – NA META: " if na_meta else "") + \
        f"Passagens {cfg['origem']}-{cfg['destino']} – menor R$ {r(menor * 2)} por casal" if menor else "Passagens: nenhuma opção válida"
    m = MIMEText(corpo(cfg, res), "plain", "utf-8")
    m["Subject"], m["From"], m["To"] = assunto, user, cfg["email"]
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pw); s.send_message(m)
    return "e-mail enviado"


def alerta_telegram(cfg, res, anterior):
    """Avisa no Telegram (iPhone) se o menor preço ficou na meta ou abaixo do menor já visto."""
    tok, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    menor = min((x["preco"] for x in res.get("ida", [])), default=None)
    visto = anterior.get("menor_visto")
    res["menor_visto"] = min(v for v in (menor, visto) if v is not None) if (menor or visto) else None
    if menor is None:
        return "sem preço"
    motivo = []
    if menor <= cfg["meta_por_pessoa"]:
        motivo.append(f"na meta (até R$ {r(cfg['meta_por_pessoa'])} por pessoa)")
    if visto is not None and menor < visto:
        motivo.append(f"menor que o anterior (R$ {r(visto)})")
    if not motivo:
        return "sem queda"
    if not (tok and chat):
        return "alerta não configurado (faltam TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID)"
    x = min(res["ida"], key=lambda y: y["preco"])
    txt = (f"✈️ Preço caiu! {cfg['origem']}→{cfg['destino']} {cfg['ida']} a {cfg['volta']}\n"
           f"R$ {r(menor * 2)} por casal · R$ {r(menor)} por pessoa\n"
           f"{' + '.join(x['companhias'])} · sai {x['saida']}\n"
           f"Motivo: {'; '.join(motivo)}\n"
           "Painel: https://jorgestefani.github.io/tenis-jorge-germano/monitor/")
    import urllib.request, urllib.parse
    dados = urllib.parse.urlencode({"chat_id": chat, "text": txt}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", dados, timeout=20).read()
    return "alerta enviado"


def gravar_historico(res):
    """Guarda o menor preço de cada busca (últimas 500) para o gráfico do painel."""
    if not res.get("ida"):
        return
    arq = HERE / "historico.json"
    try:
        hist = json.loads(arq.read_text(encoding="utf-8"))
    except Exception:
        hist = []
    c, x = res["config"], min(res["ida"], key=lambda y: y["preco"])
    hist.append({"t": res["atualizado"], "menor": x["preco"], "cia": " + ".join(x["companhias"]),
                 "rota": f"{c['origem']}-{c['destino']} {c['ida']} {c['volta']}"})
    arq.write_text(json.dumps(hist[-500:], ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def busca_ativa(cfg):
    """ativo=false -> cancelada; ate='AAAA-MM-DD' -> busca até esse dia; ate vazio -> sem prazo."""
    if cfg.get("ativo") is False:
        return False
    ate = (cfg.get("ate") or "").strip()
    return not ate or datetime.now(BRT).strftime("%Y-%m-%d") <= ate


def deve_rodar(cfg):
    if os.getenv("GITHUB_EVENT_NAME") != "schedule":
        return True
    if not busca_ativa(cfg):
        return False
    h = datetime.now(BRT).hour
    ini, fim, passo = cfg["hora_inicio"], cfg["hora_fim"], max(1, cfg["intervalo_horas"])
    return ini <= h <= fim and (h - ini) % passo == 0


def main():
    cfg = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
    if not deve_rodar(cfg):
        print("Busca cancelada, prazo encerrado ou fora do horário; nada a fazer."); return
    res = {"atualizado": datetime.now(BRT).strftime("%d/%m/%Y %H:%M"), "config": cfg}
    try:
        res.update(buscar(cfg))
        res["status"] = "ok"
        try: res["email"] = enviar(cfg, res)
        except Exception as e: res["email"] = f"falha no e-mail: {e}"
        try:
            ant = json.loads((HERE / "resultado.json").read_text(encoding="utf-8"))
        except Exception:
            ant = {}
        try: res["alerta"] = alerta_telegram(cfg, res, ant)
        except Exception as e: res["alerta"] = f"falha no alerta: {type(e).__name__}"
        try: gravar_historico(res)
        except Exception as e: res["historico"] = f"falha no histórico: {type(e).__name__}"
    except Exception as e:
        res["status"] = "erro"; res["erro"] = f"{type(e).__name__}: {str(e)[:400]}"
        res["detalhe"] = traceback.format_exc()[-1200:]
    (HERE / "resultado.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: res.get(k) for k in ("status", "erro", "email", "total_ida", "total_volta")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
