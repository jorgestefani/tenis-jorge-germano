"""Diagnóstico: compara o que o Google devolve com parâmetros diferentes.

A busca no servidor do GitHub (fora do Brasil) vinha sem as tarifas mais baratas
(Copa, Avianca). Testa pedir o mercado do Brasil (gl=BR) e a lista completa (tfu).
"""
import json, time
from pathlib import Path
import fast_flights
from fast_flights import FlightQuery, Passengers, create_query, get_flights

AQUI = Path(__file__).parent
cfg = json.loads((AQUI / "config.json").read_text(encoding="utf-8"))
F = lambda d, a, b: FlightQuery(date=d, from_airport=a, to_airport=b)


def consulta(extra):
    q = create_query(
        flights=[F(cfg["ida"], cfg["origem"], cfg["destino"]), F(cfg["volta"], cfg["destino"], cfg["origem"])],
        trip="round-trip", passengers=Passengers(adults=1), language="pt-BR", currency="BRL")
    base = q.params()
    q.params = lambda: {**base, **extra}
    return q, sorted(base)


out = {"quando_utc": time.strftime("%d/%m/%Y %H:%M", time.gmtime()),
       "versao": getattr(fast_flights, "__version__", "?"), "testes": {}}
VARIANTES = {"padrao": {}, "gl_BR": {"gl": "BR"}, "tfu": {"tfu": "EgQIABABIgA"},
             "gl_BR_tfu": {"gl": "BR", "tfu": "EgQIABABIgA"}}
for nome, extra in VARIANTES.items():
    q, chaves = consulta(extra)
    out["parametros_padrao"] = chaves
    res = []
    for i in range(2):
        try:
            r = get_flights(q)
            voos = sorted(
                [[f.price or 0, "+".join(f.airlines), "%02d:%02d" % tuple(f.flights[0].departure.time), len(f.flights) - 1]
                 for f in r], key=lambda v: v[0])
            res.append({"voos": len(voos), "menor": voos[0][0] if voos else None, "lista": voos})
        except Exception as e:
            res.append({"erro": f"{type(e).__name__}: {str(e)[:120]}"})
        time.sleep(6)
    out["testes"][nome] = res
(AQUI / "diag.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
