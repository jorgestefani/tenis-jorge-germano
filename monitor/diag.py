"""Diagnóstico: repete a consulta de ida e volta e anota o que o Google devolve em cada vez.

Serve para ver se a resposta vem incompleta (poucos voos, sem os mais baratos).
"""
import json, time
from pathlib import Path
from fast_flights import FlightQuery, Passengers, create_query, get_flights

AQUI = Path(__file__).parent
cfg = json.loads((AQUI / "config.json").read_text(encoding="utf-8"))
F = lambda d, a, b: FlightQuery(date=d, from_airport=a, to_airport=b)
q = create_query(
    flights=[F(cfg["ida"], cfg["origem"], cfg["destino"]), F(cfg["volta"], cfg["destino"], cfg["origem"])],
    trip="round-trip", passengers=Passengers(adults=1), language="pt-BR", currency="BRL")

out = {"quando_utc": time.strftime("%d/%m/%Y %H:%M", time.gmtime()), "tentativas": []}
for i in range(6):
    try:
        r = get_flights(q)
        voos = sorted(
            [[f.price or 0, "+".join(f.airlines), "%02d:%02d" % tuple(f.flights[0].departure.time), len(f.flights) - 1]
             for f in r], key=lambda v: v[0])
        out["tentativas"].append({"voos": len(voos), "menor": voos[0][0] if voos else None, "lista": voos})
    except Exception as e:
        out["tentativas"].append({"erro": f"{type(e).__name__}: {str(e)[:120]}"})
    time.sleep(8)
(AQUI / "diag.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
