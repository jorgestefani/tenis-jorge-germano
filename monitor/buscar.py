#!/usr/bin/env python3
"""Busca passagens no Google Flights (fast-flights) em várias datas (config "datas"), filtra esperas longas,
grava monitor/resultado.json e (opcional) envia e-mail com as 5 melhores."""
import json, os, sys, smtplib, traceback, urllib.request
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path

HERE = Path(__file__).parent
BRT = timezone(timedelta(hours=-3))
# Configuração compartilhada (horários, período, rota, datas extras e "Buscar agora"), gravada pela
# página inicial no Apps Script do Jorge; config.json continua como base e reserva.
REMOTO = "https://script.google.com/macros/s/AKfycbxGeD9hMyUXiqVXgS6z4Hrj1FhjaRNbKoaJzaK9SG4lbV98kd98cfi61EsD2Wps_HpV/exec"


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
        "troca_aeroporto": any(a.to_airport.code != b.from_airport.code for a, b in zip(segs, segs[1:])),
        "chegada_hm": fim.hour + fim.minute / 60,
        "chega_outro_dia": fim.date() != ini.date(),
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


def sem_troca(cfg):
    return (cfg.get("filtro") or {}).get("sem_troca_aeroporto", True)


def ida_aceita(cfg, x):
    """Pedido do grupo (23/11/2026): sem troca de aeroporto; ida saindo cedo e chegando
    a Miami às 12h ou depois, no mesmo dia (dormir em casa e chegar perto do check-in).
    config "filtro": {"sem_troca_aeroporto": true, "ida_saida_de": 5, "ida_saida_ate": 10, "ida_chegada_min": 12}"""
    f = cfg.get("filtro") or {}
    if sem_troca(cfg) and x["troca_aeroporto"]:
        return False
    if not (f.get("ida_saida_de", 5) <= x["hora_saida"] < f.get("ida_saida_ate", 10)):   # nada de madrugada
        return False
    if x["chega_outro_dia"] or x["chegada_hm"] < f.get("ida_chegada_min", 12):
        return False
    return True


def buscar(cfg, ida_d=None, volta_d=None, com_volta=True, tentativas_max=6):
    import time
    ida_d, volta_d = ida_d or cfg["ida"], volta_d or cfg["volta"]
    from fast_flights import FlightQuery, Passengers, create_query
    from fast_flights.fetcher import fetch_flights_html

    def get_flights(q, tentativas=tentativas_max):
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
        flights=[FlightQuery(date=ida_d, from_airport=o, to_airport=d),
                 FlightQuery(date=volta_d, from_airport=d, to_airport=o)],
        trip="round-trip", passengers=Passengers(adults=1),
        language="pt-BR", currency="BRL")
    ida = sem_repetir([fmt(f) for f in get_flights(q)])
    # voos de volta (só para checar horários e esperas)
    qv = create_query(
        flights=[FlightQuery(date=volta_d, from_airport=d, to_airport=o)],
        trip="one-way", passengers=Passengers(adults=1),
        language="pt-BR", currency="BRL")
    volta = sem_repetir([fmt(f) for f in get_flights(qv)]) if com_volta else []
    lim = cfg["espera_max_horas"]
    ida_ok = sorted([x for x in ida if x["espera_max_h"] < lim and ida_aceita(cfg, x)], key=lambda x: x["preco"])
    volta_ok = sorted([x for x in volta if x["espera_max_h"] < lim and not (sem_troca(cfg) and x["troca_aeroporto"])],
                      key=lambda x: x["preco"])
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
        link = f"https://www.google.com/travel/flights?hl=pt-BR&curr=BRL&q=Voos%20{o}%20para%20{d}%20{ida_d}%20{volta_d}"
    return {"ida_data": ida_d, "volta_data": volta_d,
            "total_ida": len(ida), "total_volta": len(volta),
            "link_google_flights": link,
            "ida": top, "volta": volta_ok[:3]}


DIAS = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]


def flex_ativo(cfg):
    """config "flex": {"noites": 7, "de": "AAAA-MM-DD", "ate": "AAAA-MM-DD"} (pedido de 23/11/2026):
    pesquisa todas as idas do período com volta N noites depois; a volta também fica dentro do período."""
    f = cfg.get("flex") or {}
    try:
        n, de, ate = int(f["noites"]), datetime.strptime(f["de"], "%Y-%m-%d"), datetime.strptime(f["ate"], "%Y-%m-%d")
    except Exception:
        return None
    hoje = datetime.now(BRT).replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0)
    de = max(de, hoje + timedelta(days=1))
    if not (1 <= n <= 30) or de + timedelta(days=n) > ate:
        return None
    return n, de, ate


def combinacoes(cfg):
    """Datas a pesquisar. config "datas": {"idas": [...], "volta": "AAAA-MM-DD", "semanas": [0, 1, 2],
    "extras": [{"ida": "AAAA-MM-DD", "volta": "AAAA-MM-DD"}]}
    -> cada ida com a mesma volta, repetido nas semanas seguintes (+7 dias por semana)."""
    dd = cfg.get("datas") or {}
    idas, volta = dd.get("idas") or [cfg["ida"]], dd.get("volta") or cfg["volta"]
    out = []
    fx = flex_ativo(cfg)
    if fx:                                   # período: troca as datas fixas por todas as idas do período
        n, de, ate = fx
        d = de
        while d + timedelta(days=n) <= ate and len(out) < 62:
            v = d + timedelta(days=n)
            out.append({"semana": 0, "ida": d.strftime("%Y-%m-%d"), "volta": v.strftime("%Y-%m-%d"),
                        "dia_ida": DIAS[d.weekday()], "dia_volta": DIAS[v.weekday()], "noites": n, "flex": True})
            d += timedelta(days=1)
    for sem in ([] if fx else (dd.get("semanas") or [0])):
        for ida in idas:
            mais = lambda x: (datetime.strptime(x, "%Y-%m-%d") + timedelta(days=7 * sem)).strftime("%Y-%m-%d")
            i, v = mais(ida), mais(volta)
            out.append({"semana": sem, "ida": i, "volta": v,
                        "dia_ida": DIAS[datetime.strptime(i, "%Y-%m-%d").weekday()],
                        "dia_volta": DIAS[datetime.strptime(v, "%Y-%m-%d").weekday()],
                        "noites": (datetime.strptime(v, "%Y-%m-%d") - datetime.strptime(i, "%Y-%m-%d")).days})
    # combinações avulsas fora da regra (ex.: domingo 14/02 -> terça 23/02, pedido de 16/11/2026); contam como semana original
    for ex in ([] if fx else (dd.get("extras") or [])):
        i, v = ex["ida"], ex["volta"]
        if any(o["ida"] == i and o["volta"] == v for o in out):
            continue
        out.append({"semana": ex.get("semana", 0), "ida": i, "volta": v,
                    "dia_ida": DIAS[datetime.strptime(i, "%Y-%m-%d").weekday()],
                    "dia_volta": DIAS[datetime.strptime(v, "%Y-%m-%d").weekday()],
                    "noites": (datetime.strptime(v, "%Y-%m-%d") - datetime.strptime(i, "%Y-%m-%d")).days})
    out.sort(key=lambda o: (o["ida"], o["volta"]))
    return out


def buscar_todas(cfg):
    """Busca cada combinação; a melhor (menor preço) vira o resultado principal."""
    import time
    datas, melhor = [], None
    fx = flex_ativo(cfg)
    for c in combinacoes(cfg):
        item = dict(c)
        try:
            r_ = buscar(cfg, c["ida"], c["volta"], com_volta=not fx, tentativas_max=3 if fx else 6)
            if not r_["ida"]:
                raise RuntimeError("nenhum voo dentro do filtro (sem troca de aeroporto, saída cedo e chegada às 12h ou depois)")
            item.update({"status": "ok", "menor": min((x["preco"] for x in r_["ida"]), default=None),
                         "opcoes": r_["ida"], "volta_opcoes": r_["volta"],
                         "link_google_flights": r_["link_google_flights"]})
            if item["menor"] is not None and (melhor is None or item["menor"] < melhor[0]["menor"]):
                melhor = (item, r_)
        except Exception as e:
            item.update({"status": "erro", "erro": f"{type(e).__name__}: {str(e)[:200]}"})
        datas.append(item)
        time.sleep(3)  # não martelar o Google
    if melhor is None:
        raise RuntimeError("nenhuma data trouxe preço: " + "; ".join(d.get("erro", "") for d in datas)[:300])
    m, r_ = melhor
    if fx:                                   # completa os voos de volta só da data mais barata
        try:
            r_ = buscar(cfg, m["ida"], m["volta"])
            m["volta_opcoes"] = r_["volta"]
        except Exception as e:
            print(f"volta da melhor data: {type(e).__name__}")
    x = min(m["opcoes"], key=lambda y: y["preco"])
    res = dict(r_)
    res["datas"] = datas
    if fx:
        res["flex"] = {"noites": fx[0], "de": fx[1].strftime("%Y-%m-%d"), "ate": fx[2].strftime("%Y-%m-%d"),
                       "pesquisadas": len(datas), "com_preco": sum(1 for d in datas if d.get("status") == "ok")}
    res["melhor"] = {"ida": m["ida"], "volta": m["volta"], "dia_ida": m["dia_ida"], "dia_volta": m["dia_volta"],
                     "semana": m["semana"], "noites": m["noites"], "preco": x["preco"], "companhias": x["companhias"],
                     "saida": x["saida"], "chegada": x["chegada"], "paradas": x["paradas"], "duracao_h": x["duracao_h"]}
    return res


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
    return "e-mail desligado (pedido do Jorge, 27/11/2026)"
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
    m = res.get("melhor") or {}
    hist.append({"t": res["atualizado"], "menor": x["preco"], "cia": " + ".join(x["companhias"]),
                 "ida": m.get("ida"), "volta": m.get("volta"),
                 "rota": f"{c['origem']}-{c['destino']} {c['ida']} {c['volta']}"})
    arq.write_text(json.dumps(hist[-500:], ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def busca_ativa(cfg):
    """ativo=false -> cancelada; ate='AAAA-MM-DD' -> busca até esse dia; ate vazio -> sem prazo."""
    if cfg.get("ativo") is False:
        return False
    ate = (cfg.get("ate") or "").strip()
    return not ate or datetime.now(BRT).strftime("%Y-%m-%d") <= ate


def config_remota(cfg):
    """Sobrepõe ao config.json o que foi salvo pela página. Devolve (cfg, pedido de busca em ISO UTC)."""
    try:
        d = json.loads(urllib.request.urlopen(REMOTO, timeout=25).read().decode("utf-8"))
    except Exception as e:
        print(f"config remota indisponível ({type(e).__name__}); usando config.json"); return cfg, ""
    r = d.get("cfg") or {}
    for k in ("intervalo_horas", "hora_inicio", "hora_fim", "ate", "ativo", "origem", "destino"):
        if k in r:
            cfg[k] = r[k]
    if "flex" in r:
        cfg["flex"] = r["flex"]
    if isinstance(r.get("extras"), list):
        cfg.setdefault("datas", {})["extras"] = r["extras"]
    return cfg, d.get("pedido") or ""


def ultima_busca():
    try:
        a = json.loads((HERE / "resultado.json").read_text(encoding="utf-8")).get("atualizado", "")
        return datetime.strptime(a, "%d/%m/%Y %H:%M").replace(tzinfo=BRT)
    except Exception:
        return None


def deve_rodar(cfg, pedido=""):
    if os.getenv("GITHUB_EVENT_NAME") != "schedule":
        return True
    agora, ult = datetime.now(BRT), ultima_busca()
    # "Buscar agora" da página: atende se o pedido é mais novo que a última busca e tem menos de 6 h
    try:
        ped = datetime.fromisoformat(pedido.replace("Z", "+00:00")) if pedido else None
    except ValueError:
        ped = None
    if ped and (ult is None or ped > ult) and agora - ped < timedelta(hours=6):
        print("Busca pedida pela página."); return True
    if not busca_ativa(cfg):
        return False
    h = agora.hour
    ini, fim, passo = cfg["hora_inicio"], cfg["hora_fim"], max(1, cfg["intervalo_horas"])
    no_horario = ini <= h <= fim and (h - ini) % passo == 0
    # o cron roda 2x por hora (:07 e :37); só uma busca por hora programada
    ja_feita = ult is not None and ult.strftime("%Y%m%d%H") == agora.strftime("%Y%m%d%H")
    return no_horario and not ja_feita


def main():
    cfg = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
    cfg, pedido = config_remota(cfg)
    if not deve_rodar(cfg, pedido):
        print("Busca cancelada, prazo encerrado ou fora do horário; nada a fazer."); return
    res = {"atualizado": datetime.now(BRT).strftime("%d/%m/%Y %H:%M"), "config": cfg}
    try:
        res.update(buscar_todas(cfg))
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
