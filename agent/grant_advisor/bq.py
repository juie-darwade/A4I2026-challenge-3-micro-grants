"""Thin BigQuery layer: the only module that talks to BigQuery.

Every multi-hop question goes through the property graph with GQL
(GRAPH ... MATCH, or GRAPH_TABLE inside SQL). Small lookup tables are read once
per process and cached, because a corridor is only a few hundred rows.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from functools import lru_cache

from google.cloud import bigquery

PROJECT = os.environ.get("BQ_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT")
DATASET = os.environ.get("BQ_DATASET", "a4i_econ")
LOCATION = os.environ.get("BQ_LOCATION", "US")
GRAPH = os.environ.get("BQ_GRAPH", f"{DATASET}.LocalEconomy")


@lru_cache(maxsize=1)
def client() -> bigquery.Client:
    return bigquery.Client(project=PROJECT, location=LOCATION)


def run(sql: str, **params) -> list[dict]:
    """Run a query with named scalar parameters and return plain dict rows."""
    qp = []
    for name, value in params.items():
        if isinstance(value, bool):
            kind = "BOOL"
        elif isinstance(value, int):
            kind = "INT64"
        elif isinstance(value, float):
            kind = "FLOAT64"
        else:
            kind = "STRING"
        qp.append(bigquery.ScalarQueryParameter(name, kind, value))
    job = client().query(sql, job_config=bigquery.QueryJobConfig(query_parameters=qp))
    return [dict(r.items()) for r in job.result()]


# ---------------------------------------------------------------------------
# Graph queries (GQL). These are the multi-hop traversals the challenge requires.
# ---------------------------------------------------------------------------

def sql_candidates(max_hops: int) -> str:
    # Quantified path {1,N}: every business reachable within N supply hops.
    return f"""
GRAPH {GRAPH}
MATCH (seed:Business)-[:Supplies]->{{1,{int(max_hops)}}}(exposed:Business)
WHERE exposed.business_id != seed.business_id
RETURN seed.business_id AS business_id,
       COUNT(DISTINCT exposed.business_id) AS reach
NEXT
FILTER reach >= @min_reach AND reach <= @max_reach
RETURN business_id, reach
ORDER BY reach DESC
"""


SQL_HOP1 = f"""
SELECT * FROM GRAPH_TABLE({GRAPH}
  MATCH (s:Business)-[e1:Supplies]->(x:Business)
  WHERE s.business_id = @seed
  COLUMNS (x.business_id AS hop1, e1.intensity AS w1)
)
"""

# Explicit two-hop pattern so each link's intensity can be weighted separately.
SQL_HOP2 = f"""
SELECT * FROM GRAPH_TABLE({GRAPH}
  MATCH (s:Business)-[e1:Supplies]->(m:Business)-[e2:Supplies]->(x:Business)
  WHERE s.business_id = @seed AND x.business_id != s.business_id
  COLUMNS (m.business_id AS hop1, x.business_id AS hop2,
           e1.intensity AS w1, e2.intensity AS w2)
)
"""

# What the seed depends on: traverse the same edges backwards, two hops.
SQL_UPSTREAM = f"""
GRAPH {GRAPH}
MATCH (up:Business)-[:Supplies]->{{1,2}}(s:Business)
WHERE s.business_id = @seed AND up.business_id != s.business_id
RETURN DISTINCT up.business_id AS business_id
"""

# Anchor closure: forward along footfall, then BACKWARD along supply (<-).
SQL_ANCHOR = f"""
SELECT * FROM GRAPH_TABLE({GRAPH}
  MATCH (e:Employer)-[f:DrawsFootfall]->(shop:Business)<-[s:Supplies]-(sup:Business)
  WHERE e.block_id = @block
  COLUMNS (shop.business_id AS shop_id, sup.business_id AS supplier_id,
           f.worker_share AS worker_share, s.intensity AS intensity)
)
"""

SQL_ANCHOR_SHOPS = f"""
SELECT * FROM GRAPH_TABLE({GRAPH}
  MATCH (e:Employer)-[f:DrawsFootfall]->(shop:Business)
  WHERE e.block_id = @block
  COLUMNS (shop.business_id AS shop_id, f.worker_share AS worker_share)
)
"""


def candidates(min_reach: int, max_reach: int, max_hops: int) -> list[dict]:
    return run(sql_candidates(max_hops), min_reach=min_reach, max_reach=max_reach)


def hop1(seed: str) -> list[dict]:
    return run(SQL_HOP1, seed=seed)


def hop2(seed: str) -> list[dict]:
    return run(SQL_HOP2, seed=seed)


def upstream(seed: str) -> list[str]:
    return [r["business_id"] for r in run(SQL_UPSTREAM, seed=seed)]


def anchor(block_id: str) -> tuple[list[dict], list[dict]]:
    return run(SQL_ANCHOR_SHOPS, block=block_id), run(SQL_ANCHOR, block=block_id)


# ---------------------------------------------------------------------------
# Corridor context: small lookup tables, read once.
# ---------------------------------------------------------------------------

@dataclass
class Corridor:
    businesses: dict[str, dict]                       # business_id -> row
    buyer_in_total: dict[str, float]                  # buyer -> sum of incoming supply intensity
    supplier_out_total: dict[str, float]              # supplier -> sum of outgoing intensity
    same_industry: dict[str, int]                     # naics4 -> number of businesses
    shop_footfall_total: dict[str, float]             # shop -> sum of worker_share
    loans: dict[str, dict]                            # business_id -> {loans, charged_off, banks}
    tracts: list[dict] = field(default_factory=list)  # tract centre + demographics
    blocks: list[dict] = field(default_factory=list)  # employer blocks by jobs

    def alternatives(self, business_id: str) -> int:
        """Other businesses in the corridor in the same 4-digit industry."""
        naics = self.businesses.get(business_id, {}).get("naics4")
        return max(self.same_industry.get(naics, 1) - 1, 0)

    def label(self, business_id: str) -> dict:
        b = self.businesses.get(business_id, {})
        return {"business_id": business_id, "name": b.get("name"),
                "address": b.get("address"), "naics4": b.get("naics4")}

    def tract_for(self, business_id: str) -> dict | None:
        """Nearest tract centre (approximate: tract internal point, not the boundary)."""
        b = self.businesses.get(business_id)
        if not b or b.get("latitude") is None or not self.tracts:
            return None
        def dist(t):
            return _haversine_m(b["latitude"], b["longitude"], t["lat"], t["lon"])
        best = min(self.tracts, key=dist)
        return {**best, "metres_to_tract_centre": round(dist(best))}


def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(a))


@lru_cache(maxsize=1)
def corridor() -> Corridor:
    ds = DATASET
    biz = run(f"SELECT * FROM `{ds}.businesses`")
    businesses = {r["business_id"]: r for r in biz}

    same_industry: dict[str, int] = {}
    for r in biz:
        same_industry[r.get("naics4")] = same_industry.get(r.get("naics4"), 0) + 1

    buyer_in = {r["k"]: float(r["v"]) for r in run(
        f"SELECT buyer_id k, SUM(intensity) v FROM `{ds}.supplies` GROUP BY 1")}
    supplier_out = {r["k"]: float(r["v"]) for r in run(
        f"SELECT supplier_id k, SUM(intensity) v FROM `{ds}.supplies` GROUP BY 1")}

    try:
        shop_total = {r["k"]: float(r["v"]) for r in run(
            f"SELECT business_id k, SUM(worker_share) v FROM `{ds}.draws_footfall_from` GROUP BY 1")}
    except Exception:  # noqa: BLE001 - footfall layer is optional
        shop_total = {}

    try:
        loans = {r["business_id"]: r for r in run(f"""
            SELECT business_id, COUNT(*) AS loans,
                   LOGICAL_OR(charged_off) AS charged_off,
                   ARRAY_AGG(DISTINCT bank_name IGNORE NULLS) AS banks
            FROM `{ds}.borrowed_from` GROUP BY 1""")}
    except Exception:  # noqa: BLE001 - a corridor can have no SBA loans
        loans = {}

    try:
        tracts = run(f"""
            SELECT d.geo_id, ANY_VALUE(e.lat) AS lat, ANY_VALUE(e.lon) AS lon,
                   ANY_VALUE(d.poverty_rate) AS poverty_rate,
                   ANY_VALUE(d.no_vehicle_rate) AS no_vehicle_rate,
                   ANY_VALUE(d.assistance_rate) AS assistance_rate,
                   ANY_VALUE(d.median_income) AS median_income
            FROM `{ds}.tract_demographics` d
            JOIN `{ds}.employer_blocks` e ON e.tract = d.geo_id
            GROUP BY d.geo_id""")
        tracts = [t for t in tracts if t["lat"] is not None]
    except Exception:  # noqa: BLE001
        tracts = []

    try:
        blocks = run(f"""
            SELECT block_id, tract, jobs FROM `{ds}.employer_blocks`
            ORDER BY jobs DESC LIMIT 25""")
    except Exception:  # noqa: BLE001
        blocks = []

    return Corridor(businesses, buyer_in, supplier_out, same_industry,
                    shop_total, loans, tracts, blocks)


def find_business(name: str) -> list[dict]:
    """Case-insensitive name/legal-name match. Can return several (duplicates are real)."""
    needle = name.strip().lower()
    c = corridor()
    exact = [b for b in c.businesses.values()
             if (b.get("name") or "").lower() == needle
             or (b.get("legal_name") or "").lower() == needle]
    if exact:
        return exact
    return [b for b in c.businesses.values()
            if needle in (b.get("name") or "").lower()
            or needle in (b.get("legal_name") or "").lower()]
