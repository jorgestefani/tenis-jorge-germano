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


def fmt(f):
    segs = f.flights
    ini, fim = dt(segs[0])[0], dt(segs[-1])[1]
    w = esperas(segs)
    return {
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


def buscar(cfg):
    from fast_flights import FlightQuery, Passengers, create_query, get_flights
    o, d = cfg["origem"], cfg["destino"]
    # preço por pessoa: consulta com 1 adulto (ida e volta)
    q = create_query(
        flights=[FlightQuery(date=cfg["ida"], from_airport=o, to_airport=d),
                 FlightQuery(date=cfg["volta"], from_airport=d, to_airport=o)],
        trip="round-trip", passengers=Passengers(adults=1),
        language="pt-BR", currency="BRL")
    ida = [fmt(f) for f in get_flights(q)]
    # voos de volta (só para checar horários e esperas)
    qv = create_query(
        flights=[FlightQuery(date=cfg["volta"], from_airport=d, to_airport=o)],
        trip="one-way", passengers=Passengers(adults=1),
        language="pt-BR", currency="BRL")
    volta = [fmt(f) for f in get_flights(qv)]
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
    return {"total_ida": len(ida), "total_volta": len(volta),
            "ida": top, "volta": volta_ok[:3]}


def corpo(cfg, res):
    pax = cfg["adultos"]
    L = [f"Busca {cfg['origem']}→{cfg['destino']} {cfg['ida']} a {cfg['volta']} · {pax} adultos",
         f"Atualizado: {res['atualizado']} (Brasília)", ""]
    na_meta = [x for x in res["ida"] if x["preco"] <= cfg["meta_por_pessoa"]]
    if na_meta:
        L.append(f"*** {len(na_meta)} opção(ões) NA META (até R$ {cfg['meta_por_pessoa']:,.0f} por pessoa) ***\n")
    L.append("IDA (preço de ida e volta por pessoa):")
    for i, x in enumerate(res["ida"], 1):
        L.append(f"{i}. R$ {x['preco']:,.0f}/pessoa · total {pax}: R$ {x['preco']*pax:,.0f} · {', '.join(x['companhias'])}\n"
                 f"   sai {x['saida']} chega {x['chegada']} · {x['paradas']} parada(s) · esperas: {'; '.join(x['escalas']) or 'voo direto'}")
    L.append("\nVOLTA (opções com espera dentro do limite):")
    for x in res["volta"]:
        L.append(f"- {', '.join(x['companhias'])} · sai {x['saida']} chega {x['chegada']} · esperas: {'; '.join(x['escalas']) or 'direto'}")
    L.append("\nPreço do Google Flights; confirme no site antes de comprar.")
    return "\n".join(L)


def enviar(cfg, res):
    user, pw = os.getenv("GMAIL_USER"), os.getenv("GMAIL_APP_PASSWORD")
    if not (user and pw):
        return "e-mail não configurado (faltam os segredos GMAIL_USER e GMAIL_APP_PASSWORD)"
    menor = min((x["preco"] for x in res["ida"]), default=None)
    na_meta = menor is not None and menor <= cfg["meta_por_pessoa"]
    assunto = ("URGENTE – NA META: " if na_meta else "") + \
        f"Passagens {cfg['origem']}-{cfg['destino']} – menor R$ {menor:,.0f} por pessoa" if menor else "Passagens: nenhuma opção válida"
    m = MIMEText(corpo(cfg, res), "plain", "utf-8")
    m["Subject"], m["From"], m["To"] = assunto, user, cfg["email"]
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pw); s.send_message(m)
    return "e-mail enviado"


def deve_rodar(cfg):
    if os.getenv("GITHUB_EVENT_NAME") != "schedule":
        return True
    h = datetime.now(BRT).hour
    ini, fim, passo = cfg["hora_inicio"], cfg["hora_fim"], max(1, cfg["intervalo_horas"])
    return ini <= h <= fim and (h - ini) % passo == 0


def main():
    cfg = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
    if not deve_rodar(cfg):
        print("Fora do horário programado; nada a fazer."); return
    res = {"atualizado": datetime.now(BRT).strftime("%d/%m/%Y %H:%M"), "config": cfg}
    try:
        res.update(buscar(cfg))
        res["status"] = "ok"
        try: res["email"] = enviar(cfg, res)
        except Exception as e: res["email"] = f"falha no e-mail: {e}"
    except Exception as e:
        res["status"] = "erro"; res["erro"] = f"{type(e).__name__}: {str(e)[:400]}"
        res["detalhe"] = traceback.format_exc()[-1200:]
    (HERE / "resultado.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: res.get(k) for k in ("status", "erro", "email", "total_ida", "total_volta")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
