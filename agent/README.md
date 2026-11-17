# Corridor Grant Advisor (ADK agent)

Ask in plain English ("What happens if Merchant Roots closes?", "We have $50k, compare
these three applicants", "How many restaurants are in the corridor?"). The agent runs
the BigQuery and graph queries in the background and answers, ending with a grant memo
when you ask for a recommendation.

## How it meets the challenge requirements

| Requirement | Where |
|---|---|
| ADK agent in Python | `grant_advisor/agent.py` (`root_agent`) |
| Gemini | `GEMINI_MODEL` (default `gemini-3.6-flash`, via Vertex AI) |
| Managed MCP server, consumed | Google's BigQuery MCP server `https://bigquery.googleapis.com/mcp` (`execute_sql`, `get_table_info`, `list_table_ids`) for open questions |
| Custom tools with real logic | `grant_advisor/tools.py` + `scoring.py`: exposure weighting, replaceability, equity tilt, budget allocation, sensitivity checks |
| Genuine multi-hop GQL | `grant_advisor/bq.py`: quantified `-[:Supplies]->{1,N}` shortlist, explicit 2-hop `MATCH (s)-[e1]->(m)-[e2]->(x)`, backward `{1,2}` upstream, and footfall-forward / supply-backward anchor traversal |
| Deployed to Google Cloud | `deploy.sh` -> Cloud Run |

## The decision rules (what judges will ask about)

- **Dependence** of buyer B on supplier S = S's intensity / all modeled intensity into B.
- **Replaceability**: if k other corridor businesses share S's 4-digit industry, the link counts 1/(1+k).
- **Propagation**: 2-hop exposure = exposure of the middle business x its dependence x its replaceability. Strongest path wins (no double counting).
- **Protected value** = sum of exposures, in business-equivalents (not jobs; per-business jobs are not public).
- **Priority** = protected value x (1 + tract poverty rate).
- **Amount**: budget shared by priority, clamped to $5k–$25k, in $500 steps.
- **Assumption**: a grant cuts closure risk by 25% (`risk_reduction`, change it to test).
- **Sensitivity**: the tool reports whether ranking by raw reach (centrality) or ignoring replaceability would pick a different winner.

## Files

```
agent/
  grant_advisor/
    __init__.py
    agent.py      # root agent, instructions, BigQuery MCP toolset
    tools.py      # 4 custom tools the agent calls
    scoring.py    # pure decision logic (tested offline)
    bq.py         # all BigQuery / GQL queries
    .env.example
  tests/test_tools.py   # offline tests on the notebook's toy block
  requirements.txt  Dockerfile  deploy.sh
```

## Before you start

1. The six tables are in `a4i_econ` and the property graph exists:
   `CREATE OR REPLACE PROPERTY GRAPH a4i_econ.LocalEconomy ...` (Section 13 DDL).
2. Graph queries work in your project (the reservation). Test one in the console first.
3. Know your dataset location: `US`, or `us-central1` if you moved it.

## Run locally in Cloud Shell

```bash
cd agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp grant_advisor/.env.example grant_advisor/.env   # then edit project + BQ_LOCATION
gcloud auth application-default login             # skip if Cloud Shell already has ADC
adk web --allow_origins="*"                        # open the Web Preview on port 8000
```

Pick `grant_advisor` in the top-left dropdown and try:

- "Which businesses would hurt the corridor most if they closed?"
- "What happens if <name from the shortlist> closes?"
- "Compare <A>, <B> and <C> for a $50,000 fund and write the grant memo."
- "A large employer is leaving. Which block has the most jobs, and what's the impact?"
- "How many businesses have an SBA loan that was charged off?"

Offline tests (no BigQuery needed): `python -m pytest -q tests`

## Deploy to Cloud Run

```bash
bash deploy.sh                           # dataset in US
BQ_LOCATION=us-central1 bash deploy.sh   # dataset in us-central1
```

It enables the APIs, creates a service account with `aiplatform.user`,
`bigquery.jobUser`, `bigquery.dataViewer` and `mcp.toolUser`, and deploys with
min-instances 1. If the URL returns Forbidden (lab org policy), use
`gcloud run services proxy grant-advisor --region us-central1 --port 8080`.

The service runs `adk web`, so the same URL serves the chat UI and the REST API
(`POST /run`, `POST /run_sse`) for the front-end lane.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Graph queries require a reservation` | Reservation not set up or still propagating |
| `Not found: Dataset ... was not found in location US` | Set `BQ_LOCATION` to your dataset's location |
| `property graph ... not found` | Run the `CREATE PROPERTY GRAPH` DDL; check `BQ_GRAPH` |
| `404 model not found` | Set `GEMINI_MODEL` to a model your project has (e.g. `gemini-3.6-flash`) |
| MCP tools fail with 403 | `gcloud beta services mcp enable bigquery.googleapis.com`; grant `roles/mcp.toolUser` |
