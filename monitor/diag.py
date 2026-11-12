import json, traceback
from pathlib import Path
from fast_flights import FlightQuery, Passengers, create_query, get_flights
out = {}
def tent(nome, **kw):
    try:
        legs = kw.pop("legs")
        q = create_query(flights=legs, **kw)
        r = get_flights(q)
        out[nome] = {"ok": len(r), "exemplo": str(r[0])[:300] if len(r) else None}
    except Exception as e:
        out[nome] = {"erro": f"{type(e).__name__}: {str(e)[:200]}"}
F = lambda d, a, b: FlightQuery(date=d, from_airport=a, to_airport=b)
tent("oneway_FLN_MIA_basico", legs=[F("2027-02-14","FLN","MIA")], trip="one-way", passengers=Passengers(adults=1))
tent("oneway_FLN_MIA_brl", legs=[F("2027-02-14","FLN","MIA")], trip="one-way", passengers=Passengers(adults=1), language="pt-BR", currency="BRL")
tent("roundtrip_basico", legs=[F("2027-02-14","FLN","MIA"),F("2027-02-24","MIA","FLN")], trip="round-trip", passengers=Passengers(adults=1))
tent("roundtrip_brl", legs=[F("2027-02-14","FLN","MIA"),F("2027-02-24","MIA","FLN")], trip="round-trip", passengers=Passengers(adults=1), language="pt-BR", currency="BRL")
tent("oneway_GRU_MIA_marco", legs=[F("2027-03-10","GRU","MIA")], trip="one-way", passengers=Passengers(adults=1))
tent("oneway_JFK_LAX_controle", legs=[F("2027-01-20","JFK","LAX")], trip="one-way", passengers=Passengers(adults=1))
Path(__file__).with_name("diag.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
