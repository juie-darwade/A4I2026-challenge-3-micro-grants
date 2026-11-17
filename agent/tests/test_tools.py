"""Offline tests: the notebook's 12-business toy block, with BigQuery mocked out.

Run:  python -m pytest -q tests   (from the agent/ folder)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from grant_advisor import bq, scoring, tools  # noqa: E402

BIZ = {
    "b01": ("Peak Hardware", "4441"), "b02": ("Delgado Lumber", "4233"),
    "b03": ("Ng Contracting", "2361"), "b04": ("Ruiz Remodel", "2361"),
    "b05": ("Baytown Builders", "2361"), "b06": ("Corner Counter", "7225"),
    "b12": ("Kimura Grocery", "4451"), "b13": ("Second Grocery", "4451"),
}
# supplier -> buyer, intensity
EDGES = [("b02", "b01", 0.20), ("b01", "b03", 0.10), ("b01", "b04", 0.10),
         ("b01", "b05", 0.10), ("b03", "b06", 0.05), ("b12", "b06", 0.05),
         ("b13", "b06", 0.05), ("b06", "b03", 0.01)]


def _ctx():
    businesses = {k: {"business_id": k, "name": n, "legal_name": n, "address": f"{k} Main St",
                      "naics4": c, "latitude": 37.78, "longitude": -122.41}
                  for k, (n, c) in BIZ.items()}
    same = {}
    for b in businesses.values():
        same[b["naics4"]] = same.get(b["naics4"], 0) + 1
    bin_, bout = {}, {}
    for s, b, w in EDGES:
        bin_[b] = bin_.get(b, 0) + w
        bout[s] = bout.get(s, 0) + w
    tracts = [{"geo_id": "06075017601", "lat": 37.78, "lon": -122.41,
               "poverty_rate": 0.22, "no_vehicle_rate": 0.6}]
    return bq.Corridor(businesses, bin_, bout, same, {}, {}, tracts, [])


def _hop1(seed):
    return [{"hop1": b, "w1": w} for s, b, w in EDGES if s == seed]


def _hop2(seed):
    return [{"hop1": m, "hop2": x, "w1": w1, "w2": w2}
            for s, m, w1 in EDGES if s == seed
            for s2, x, w2 in EDGES if s2 == m and x != seed]


def _reach(seed, hops=2):
    seen, frontier = set(), {seed}
    for _ in range(hops):
        frontier = {b for s, b, _ in EDGES if s in frontier} - {seed}
        seen |= frontier
    return seen


def setup_module():
    ctx = _ctx()
    bq.corridor = lambda: ctx
    bq.hop1, bq.hop2 = _hop1, _hop2
    bq.upstream = lambda seed: sorted({s for s, b, _ in EDGES if b == seed})
    bq.candidates = lambda lo, hi, h: sorted(
        [{"business_id": k, "reach": len(_reach(k, h))} for k in BIZ
         if lo <= len(_reach(k, h)) <= hi], key=lambda r: -r["reach"])


def test_cascade_weights_and_replaceability():
    ctx = bq.corridor()
    ex = scoring.cascade("b01", _hop1("b01"), _hop2("b01"), ctx.buyer_in_total, ctx.alternatives)
    # Contractors depend fully on Peak Hardware (their only supplier), no alternative hardware store.
    assert ex["b03"].exposure > 0.9 and ex["b03"].hops == 1
    # Corner Counter has three suppliers; one contractor is a third of its inputs,
    # and contractors are replaceable (3 in the industry), so 2-hop exposure is small.
    assert ex["b06"].hops == 2 and ex["b06"].exposure < 0.15


def test_grocery_with_alternative_is_discounted():
    ctx = bq.corridor()
    ex = scoring.cascade("b12", _hop1("b12"), _hop2("b12"), ctx.buyer_in_total, ctx.alternatives)
    no_rep = scoring.cascade("b12", _hop1("b12"), _hop2("b12"), ctx.buyer_in_total,
                             ctx.alternatives, use_replaceability=False)
    assert ex["b06"].exposure == no_rep["b06"].exposure / 2


def test_analyze_tool():
    out = tools.analyze_closure_cascade("peak hardware")
    assert out["business"]["name"] == "Peak Hardware"
    assert out["direct_buyers"] == 3
    assert out["most_exposed"][0]["hops"] == 1
    assert out["neighborhood"]["poverty_rate"] == 0.22
    assert out["upstream_dependencies"] == ["Delgado Lumber"]


def test_recommend_beats_centrality_trap():
    out = tools.recommend_grants(["Peak Hardware", "Delgado Lumber", "Kimura Grocery"])
    names = [r["name"] for r in out["all"]]
    assert out["recommended"]["grant"] >= 5000
    assert out["allocated"] <= 50_000
    assert set(names) == {"Peak Hardware", "Delgado Lumber", "Kimura Grocery"}
    assert "sensitivity" in out and len(out["assumptions"]) >= 5
    print(out["recommended"]["name"], out["sensitivity"])


def test_candidates_and_unknown_name():
    assert tools.find_grant_candidates(1, 25)["count"] >= 1
    assert "error" in tools.analyze_closure_cascade("No Such Shop")


def test_allocate_respects_budget():
    g = scoring.allocate({"a": 3.0, "b": 1.0, "c": 0.5}, 50_000, 5_000, 25_000)
    assert sum(g.values()) <= 50_000 and max(g.values()) <= 25_000
