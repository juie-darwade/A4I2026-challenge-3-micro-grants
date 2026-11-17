"""Custom function tools for the agent.

Each tool runs a real multi-hop GQL traversal through bq.py, then applies the
decision rules in scoring.py. The tools return structured facts plus the
assumptions behind them; Gemini turns that into prose and the grant memo.
"""
from __future__ import annotations

from . import bq, scoring

MODELED_NOTE = ("Supply and footfall edges are MODELED: BEA industry purchase rates "
                "and LEHD job counts are real, but which business pairs with which is "
                "the event's allocation rule (industry match + walking distance).")


def _resolve(name: str) -> tuple[str | None, list[str]]:
    matches = bq.find_business(name)
    if not matches:
        return None, [f"No business matching '{name}' in this corridor."]
    c = bq.corridor()
    # Prefer the record that actually supplies someone; registries hold duplicates.
    matches.sort(key=lambda b: c.supplier_out_total.get(b["business_id"], 0), reverse=True)
    notes = []
    if len(matches) > 1:
        notes.append("Several registry records match: "
                     + "; ".join(f"{m['name']} ({m['address']})" for m in matches[:5])
                     + f". Using {matches[0]['name']} at {matches[0]['address']}. "
                       "These may be one business registered twice, which would split "
                       "its connections across records.")
    return matches[0]["business_id"], notes


def _analyze(business_id: str, threshold: float, use_replaceability: bool = True) -> dict:
    c = bq.corridor()
    h1, h2 = bq.hop1(business_id), bq.hop2(business_id)
    ex = scoring.cascade(business_id, h1, h2, c.buyer_in_total, c.alternatives,
                         use_replaceability=use_replaceability)
    s = scoring.summarize(ex, threshold)
    s["hop_rows"] = len(h1) + len(h2)
    return s


def find_grant_candidates(min_reach: int = 5, max_reach: int = 25, max_hops: int = 2) -> dict:
    """Find businesses whose closure would ripple to a meaningful number of others.

    Runs a quantified multi-hop GQL traversal (Supplies, 1..max_hops) and keeps
    businesses that reach between min_reach and max_reach others. Very large
    reach usually means a robust hub, not the best grant; tiny reach means a leaf.

    Args:
        min_reach: Smallest number of downstream businesses to keep.
        max_reach: Largest number of downstream businesses to keep.
        max_hops: Traversal depth. 2 is recommended; 3 saturates most corridors.
    """
    c = bq.corridor()
    rows = bq.candidates(min_reach, max_reach, max_hops)
    out = []
    for r in rows[:30]:
        bid = r["business_id"]
        out.append({**c.label(bid), "reach": r["reach"],
                    "same_industry_alternatives": c.alternatives(bid),
                    "has_sba_loan": bid in c.loans})
    return {"candidates": out, "count": len(rows), "max_hops": max_hops,
            "graph_query": bq.sql_candidates(max_hops).strip(),
            "caution": "Reach is a count, not a recommendation. Run analyze_closure_cascade "
                       "or recommend_grants to weigh link strength and replaceability."}


def analyze_closure_cascade(business_name: str, material_threshold: float = 0.10) -> dict:
    """If this business closed, which other businesses are exposed, and how strongly?

    Traverses the property graph two hops downstream along Supplies (GQL), weights
    every path by dependence and replaceability, and also traverses upstream to
    show what the business itself depends on.

    Args:
        business_name: Business name as registered (partial names work).
        material_threshold: Exposure (0-1) at which a business counts as materially exposed.
    """
    bid, notes = _resolve(business_name)
    if not bid:
        return {"error": notes[0]}
    c = bq.corridor()
    s = _analyze(bid, material_threshold)
    tract = c.tract_for(bid)
    up = bq.upstream(bid)

    def row(e):
        return {**c.label(e.business_id), "hops": e.hops,
                "via": c.label(e.via)["name"] if e.via else None,
                "exposure": round(e.exposure, 3),
                "last_link_intensity": round(e.link_strength, 4)}

    return {
        "business": c.label(bid),
        "notes": notes,
        "downstream_reach": s["reach"],
        "direct_buyers": s["reach_1hop"],
        "protected_value_business_equivalents": s["protected_value"],
        "materially_exposed_count": len(s["materially_exposed"]),
        "most_exposed": [row(e) for e in s["ranked"][:15]],
        "same_industry_alternatives_in_corridor": c.alternatives(bid),
        "upstream_dependencies": [c.label(u)["name"] for u in up[:10]],
        "upstream_count": len(up),
        "sba_loans": c.loans.get(bid),
        "neighborhood": tract,
        "neighborhood_note": "Tract chosen by nearest tract centre (approximate).",
        "evidence": MODELED_NOTE,
        "method": scoring.__doc__.strip(),
        "graph_queries": [bq.SQL_HOP1.strip(), bq.SQL_HOP2.strip(), bq.SQL_UPSTREAM.strip()],
    }


def recommend_grants(business_names: list[str] | None = None, budget: float = 50_000,
                     risk_reduction: float = 0.25, max_grant: float = 25_000,
                     min_grant: float = 5_000) -> dict:
    """Compare applicants and recommend who to fund and how much, with the reasons.

    For each business: weighted 2-hop cascade (GQL), replaceability discount,
    and a modest tilt toward higher-poverty tracts. Then allocates the budget
    and runs sensitivity checks: does the answer change if we rank by raw reach
    (centrality) or ignore replaceability?

    Args:
        business_names: Applicants to compare. Empty means use the graph shortlist.
        budget: Total fund in dollars.
        risk_reduction: ASSUMPTION - share by which a grant lowers closure risk (0-1).
        max_grant: Largest single grant.
        min_grant: Smallest grant worth making.
    """
    c = bq.corridor()
    notes = []
    if not business_names:
        rows = bq.candidates(5, 25, 2)[:8]
        ids = [r["business_id"] for r in rows]
        notes.append("No applicants given: compared the top of the graph shortlist "
                     "(2-hop reach between 5 and 25).")
    else:
        ids = []
        for n in business_names:
            bid, n_notes = _resolve(n)
            notes += n_notes
            if bid and bid not in ids:
                ids.append(bid)
    if not ids:
        return {"error": "No matching businesses to compare.", "notes": notes}

    rows, weighted, reach, unweighted, priority = [], {}, {}, {}, {}
    for bid in ids:
        s = _analyze(bid, scoring.DEFAULTS["material_threshold"])
        s_norep = _analyze(bid, scoring.DEFAULTS["material_threshold"], use_replaceability=False)
        tract = c.tract_for(bid)
        pov = tract.get("poverty_rate") if tract else None
        weighted[bid] = s["protected_value"]
        reach[bid] = s["reach"]
        unweighted[bid] = s_norep["protected_value"]
        priority[bid] = s["protected_value"] * scoring.need_weight(pov)
        rows.append({**c.label(bid), "reach": s["reach"],
                     "protected_value": s["protected_value"],
                     "materially_exposed": [c.label(e.business_id)["name"]
                                            for e in s["materially_exposed"][:8]],
                     "alternatives": c.alternatives(bid),
                     "poverty_rate": pov,
                     "no_vehicle_rate": tract.get("no_vehicle_rate") if tract else None,
                     "sba": c.loans.get(bid),
                     "priority": round(priority[bid], 3)})

    grants = scoring.allocate(priority, budget, min_grant, max_grant)
    for r in rows:
        amt = grants.get(r["business_id"], 0.0)
        r["grant"] = amt
        r["expected_business_equivalents_protected"] = round(r["protected_value"] * risk_reduction, 3)
        r["cost_per_business_equivalent"] = (
            round(amt / r["expected_business_equivalents_protected"])
            if amt and r["expected_business_equivalents_protected"] else None)
    rows.sort(key=lambda r: r["priority"], reverse=True)

    order = scoring.rank_by(priority)
    by_reach, by_norep = scoring.rank_by(reach), scoring.rank_by(unweighted)
    top = order[0]
    sensitivity = {
        "top_if_ranked_by_raw_reach": c.label(by_reach[0])["name"],
        "top_if_replaceability_ignored": c.label(by_norep[0])["name"],
        "centrality_would_change_answer": by_reach[0] != top,
        "replaceability_changes_answer": by_norep[0] != top,
    }
    return {
        "recommended": rows[0],
        "runner_up": rows[1] if len(rows) > 1 else None,
        "all": rows,
        "budget": budget,
        "allocated": sum(grants.values()),
        "sensitivity": sensitivity,
        "assumptions": [
            f"A grant lowers the recipient's closure risk by {risk_reduction:.0%} (not measured; change it to test).",
            "Dependence = supplier's share of the buyer's modeled local inputs.",
            "A link counts 1/(1+k) when the corridor has k other businesses in the supplier's industry.",
            "Exposure is measured in businesses, not jobs (per-business employment is not public).",
            "Equity tilt: priority multiplied by (1 + tract poverty rate), from the nearest tract centre.",
            MODELED_NOTE,
        ],
        "notes": notes,
    }


def anchor_closure_impact(block_id: str = "") -> dict:
    """A large employer leaves: which shops lose customers, and which suppliers lose sales?

    Traverses forward along DrawsFootfall from the workplace block to nearby shops,
    then BACKWARD along Supplies to the shops' suppliers (GQL). With no block_id,
    lists the largest workplace blocks to choose from.

    Args:
        block_id: Census workplace block id from employer_blocks. Empty lists options.
    """
    c = bq.corridor()
    if not block_id:
        return {"largest_workplace_blocks": c.blocks[:10],
                "next_step": "Call again with one block_id."}
    shops, chain = bq.anchor(block_id)
    lost = {}
    for r in shops:
        total = c.shop_footfall_total.get(r["shop_id"]) or r["worker_share"] or 1.0
        lost[r["shop_id"]] = max(lost.get(r["shop_id"], 0.0), float(r["worker_share"]) / total)
    sup = {}
    for r in chain:
        out_total = c.supplier_out_total.get(r["supplier_id"]) or 1.0
        hit = lost.get(r["shop_id"], 0.0) * float(r["intensity"]) / out_total
        sup[r["supplier_id"]] = sup.get(r["supplier_id"], 0.0) + hit
    top_shops = sorted(lost.items(), key=lambda kv: kv[1], reverse=True)[:15]
    top_sup = sorted(sup.items(), key=lambda kv: kv[1], reverse=True)[:15]
    return {
        "block_id": block_id,
        "shops_hit": len(lost),
        "shops_by_share_of_footfall_lost": [{**c.label(b), "share_lost": round(v, 3)}
                                            for b, v in top_shops],
        "suppliers_hit_indirectly": len(sup),
        "suppliers_by_share_of_local_sales_lost": [{**c.label(b), "share_lost": round(v, 3)}
                                                   for b, v in top_sup],
        "method": "Shop loss = this block's worker_share / all worker_share reaching the shop. "
                  "Supplier loss = sum over its buyers of (shop loss x that buyer's share of the "
                  "supplier's modeled local sales).",
        "evidence": MODELED_NOTE + " Block locations are tract centres, so distances are approximate.",
        "graph_queries": [bq.SQL_ANCHOR_SHOPS.strip(), bq.SQL_ANCHOR.strip()],
    }
