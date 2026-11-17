"""Corridor Grant Advisor: ADK root agent.

Tools:
  * Google's managed BigQuery MCP server (execute_sql, get_table_info, ...) for
    open-ended natural-language questions about the data.
  * Four custom function tools (tools.py) that run multi-hop GQL traversals on the
    property graph and hold the funding logic.
"""
import os

import google.auth
from google.auth.transport.requests import Request
from google.adk.agents import LlmAgent
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams

from . import bq
from .tools import (analyze_closure_cascade, anchor_closure_impact,
                    find_grant_candidates, recommend_grants)

MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")

_creds = None


def _auth_headers(_ctx=None) -> dict[str, str]:
    """Fresh OAuth token from Application Default Credentials on every MCP call."""
    global _creds
    if _creds is None:
        _creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    if not _creds.valid:
        _creds.refresh(Request())
    return {"Authorization": f"Bearer {_creds.token}",
            "x-goog-user-project": bq.PROJECT or ""}


bigquery_mcp = McpToolset(
    connection_params=StreamableHTTPConnectionParams(
        url="https://bigquery.googleapis.com/mcp",
        timeout=60.0,
    ),
    header_provider=_auth_headers,
    tool_filter=["execute_sql", "get_table_info", "list_table_ids"],
)

INSTRUCTION = f"""
You are the Corridor Grant Advisor for a city economic-development officer who has
a small relief fund (often $50,000) and more applicants than money. You argue for
grants on the strength of what a business protects DOWNSTREAM in the local economy,
not just how distressed it is.

DATA (BigQuery project {bq.PROJECT}, dataset {bq.DATASET}, location {bq.LOCATION}):
- businesses(business_id, name, legal_name, address, zip, naics, naics4, corridor,
  latitude, longitude): REAL city registry. zip is a mailing address; never join on it.
- employer_blocks(block_id, tract, jobs, lat, lon): REAL job counts; location is the tract centre.
- supplies(edge_id, supplier_id, buyer_id, intensity, metres): MODELED pairing of REAL
  BEA rates. intensity = dollars of input per $1 of buyer output.
- draws_footfall_from(edge_id, block_id, business_id, worker_share, metres): MODELED split
  of REAL LEHD jobs across shops within 500 m.
- borrowed_from(business_id, bank_name, gross_approval, approval_date, loan_status,
  charged_off): REAL SBA loans; charged_off = TRUE means the business actually failed.
- tract_demographics(geo_id, total_pop, median_income, poverty_rate, no_vehicle_rate,
  assistance_rate): REAL ACS. Join via employer_blocks.tract = geo_id.
- Property graph {bq.GRAPH}: Business and Employer nodes; Supplies (supplier -> buyer)
  and DrawsFootfall (block -> business) edges.

WHICH TOOL:
- "Who matters / where should I look?" -> find_grant_candidates.
- "What happens if X closes?" -> analyze_closure_cascade.
- "Who should we fund / compare A and B / how much?" -> recommend_grants.
- "A big employer is leaving" -> anchor_closure_impact (no block_id first, to list blocks).
- Any other factual question about the data (counts, lists, averages, a lookup) ->
  write ONE BigQuery Standard SQL query and run it with execute_sql. Always use fully
  qualified names like `{bq.PROJECT}.{bq.DATASET}.businesses`, add LIMIT 50 for lists,
  and never modify data. For multi-hop questions you may also write GQL starting with
  GRAPH {bq.GRAPH}, but prefer the custom tools.

RULES:
- Never invent numbers. Every figure must come from a tool result.
- Say which evidence is real and which is modeled whenever you recommend anything.
- Do not simply fund the most connected business. Explain why the recommendation beats
  the runner-up (protected value, replaceability, need) and report the sensitivity checks.
- State assumptions plainly, especially the risk-reduction assumption, and say what
  would change the answer.
- If several registry records match a name, say so.

GRANT MEMO FORMAT (use when recommending):
1. Recommendation: business, address, amount.
2. What it protects: named businesses with hop distance and exposure (link strength).
3. The traversal: the GQL pattern used, in one code block.
4. Why this one over the runner-up.
5. What we assume the grant changes.
6. Evidence: one honest line on what is modeled.
7. Who lives there: tract poverty and no-vehicle rates.
Keep answers short and scannable; use tables for comparisons.
"""

root_agent = LlmAgent(
    name="corridor_grant_advisor",
    model=MODEL,
    description="Recommends micro-grants by tracing how a closure ripples through a corridor.",
    instruction=INSTRUCTION,
    tools=[find_grant_candidates, analyze_closure_cascade, recommend_grants,
           anchor_closure_impact, bigquery_mcp],
)
