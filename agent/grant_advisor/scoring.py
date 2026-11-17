"""Decision logic. Pure Python with no BigQuery, so it can be unit-tested and argued about.

The graph says WHO is downstream. This module decides HOW MUCH that matters:

1. Dependence on one link = the supplier's share of the buyer's local (modeled)
   inputs: intensity(s->b) / sum of all intensities into b. A buyer with one
   local supplier depends on it fully; a buyer with five splits its risk.
2. Replaceability: if the corridor has k other businesses in the supplier's
   industry, the buyer can switch, so the link counts 1/(1+k).
3. Exposure travels: a 2-hop business is hit only as hard as the business in the
   middle was hit, times its own dependence on that middle business.
   exposure(x via m) = exposure(m) * dependence(m->x) * replace(m)
   Several paths to x: take the strongest path, not the sum (no double counting).
4. Protected value = the sum of exposures, measured in "business-equivalents"
   (1.0 = one business fully dependent with no alternative). It counts
   businesses, not jobs, because per-business employment is not public.
"""
from __future__ import annotations

from dataclasses import dataclass

DEFAULTS = {
    "material_threshold": 0.10,   # exposure at which we call a business "materially exposed"
    "risk_reduction": 0.25,       # ASSUMPTION: a grant cuts the chance of closure by this much
    "min_grant": 5_000,
    "max_grant": 25_000,
}


@dataclass
class Exposure:
    business_id: str
    hops: int
    exposure: float
    via: str | None        # middle business on the strongest path (2-hop only)
    link_strength: float   # raw BEA intensity on the last link


def cascade(seed: str, hop1_rows: list[dict], hop2_rows: list[dict],
            buyer_in_total: dict[str, float], alternatives: callable,
            use_replaceability: bool = True) -> dict[str, Exposure]:
    """Turn traversal rows into exposure per downstream business."""
    def dep(buyer: str, w: float) -> float:
        total = buyer_in_total.get(buyer) or w or 1.0
        return min(w / total, 1.0) if total else 0.0

    def rep(supplier: str) -> float:
        return 1.0 / (1 + alternatives(supplier)) if use_replaceability else 1.0

    out: dict[str, Exposure] = {}
    first: dict[str, float] = {}
    for r in hop1_rows:
        x, w = r["hop1"], float(r["w1"] or 0)
        e = dep(x, w) * rep(seed)
        first[x] = max(first.get(x, 0.0), e)
        if e > out.get(x, Exposure(x, 1, -1, None, 0)).exposure:
            out[x] = Exposure(x, 1, e, None, w)

    for r in hop2_rows:
        m, x = r["hop1"], r["hop2"]
        if x == seed:
            continue
        w1, w2 = float(r["w1"] or 0), float(r["w2"] or 0)
        em = first.get(m, dep(m, w1) * rep(seed))
        e = em * dep(x, w2) * rep(m)
        if x not in out or e > out[x].exposure:
            out[x] = Exposure(x, 2 if x not in first else out[x].hops, e,
                              m if x not in first else out[x].via, w2)
    return out


def summarize(exposures: dict[str, Exposure], threshold: float) -> dict:
    vals = sorted(exposures.values(), key=lambda e: e.exposure, reverse=True)
    return {
        "reach": len(vals),
        "reach_1hop": sum(1 for e in vals if e.hops == 1),
        "protected_value": round(sum(e.exposure for e in vals), 3),
        "materially_exposed": [e for e in vals if e.exposure >= threshold],
        "ranked": vals,
    }


def need_weight(poverty_rate: float | None) -> float:
    """Modest equity tilt: 22% poverty -> x1.22. Unknown -> x1.0 (and we say so)."""
    return 1.0 + float(poverty_rate) if poverty_rate is not None else 1.0


def allocate(priorities: dict[str, float], budget: float,
             min_grant: float, max_grant: float) -> dict[str, float]:
    """Fund in priority order; each grant ~ its share of priority, clamped, rounded to $500."""
    total = sum(p for p in priorities.values() if p > 0) or 1.0
    remaining = budget
    out = {}
    for bid, p in sorted(priorities.items(), key=lambda kv: kv[1], reverse=True):
        if p <= 0 or remaining < min_grant:
            out[bid] = 0.0
            continue
        amt = max(min_grant, min(max_grant, budget * p / total, remaining))
        amt = float(int(amt // 500) * 500)
        out[bid] = amt
        remaining -= amt
    return out


def rank_by(values: dict[str, float]) -> list[str]:
    return [k for k, _ in sorted(values.items(), key=lambda kv: kv[1], reverse=True)]
