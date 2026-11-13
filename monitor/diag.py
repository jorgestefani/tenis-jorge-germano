"""Diagnóstico: compara a resposta normal do Google com a lista completa (tfu).

A busca no servidor do GitHub vinha sem as tarifas mais baratas (Copa, Avianca).
Aqui a página é lida com um leitor próprio, que aceita seções vazias.
"""
import json, time
from pathlib import Path
from fast_flights import FlightQuery, Passengers, create_query
from fast_flights.fetcher import fetch_flights_html
from selectolax.lexbor import LexborHTMLParser

AQUI = Path(__file__).parent
cfg = json.loads((AQUI / "config.json").read_text(encoding="utf-8"))
F = lambda d, a, b: FlightQuery(date=d, from_airport=a, to_airport=b)


def consulta(extra):
    q = create_query(
        flights=[F(cfg["ida"], cfg["origem"], cfg["destino"]), F(cfg["volta"], cfg["destino"], cfg["origem"])],
        trip="round-trip", passengers=Passengers(adults=1), language="pt-BR", currency="BRL")
    base = q.params()
    q.params = lambda: {**base, **extra}
    return q


def lista(html):
    js = LexborHTMLParser(html).css_first(r"script.ds\:1").text()
    data = js.split("data:", 1)[1].rsplit(",", 1)[0]
    if data.endswith("errorHasStatus: true"):
        raise RuntimeError("o Google devolveu erro")
    p = json.loads(data)
    forma = [(len(x) if isinstance(x, list) else type(x).__name__) for x in p[:5]]
    v = []
    for i, sec in enumerate(p[2:4]):
        for k in ((sec[0] if sec else None) or []):
            fare = k[1][0] if k[1] else []
            preco = fare[1] if fare and len(fare) > 1 else 0
            voo = k[0]
            t = list(voo[2][0][8] or []) + [None, None]
            v.append([preco or 0, "+".join(voo[1]), "%02d:%02d" % (t[0] or 0, t[1] or 0), len(voo[2]) - 1,
                      "melhores" if i == 0 else "outros"])
    return sorted(v), forma


out = {"quando_utc": time.strftime("%d/%m/%Y %H:%M", time.gmtime()), "testes": {}}
for nome, extra in {"padrao": {}, "tfu": {"tfu": "EgQIABABIgA"}}.items():
    q, res = consulta(extra), []
    for i in range(4):
        try:
            voos, forma = lista(fetch_flights_html(q))
            res.append({"voos": len(voos), "menor": min([x[0] for x in voos if x[0]] or [None]), "forma": forma, "lista": voos})
        except Exception as e:
            res.append({"erro": f"{type(e).__name__}: {str(e)[:120]}"})
        time.sleep(6)
    out["testes"][nome] = res
(AQUI / "diag.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
