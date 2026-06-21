# Claim Adjudication Engine — SecureHealth-style claims

A deterministic, auditable engine that turns **any** SecureHealth-style policy and **any** claim sheet into per-claim settlements: what the insurer pays, what the member owes, and *why* — every number with its derivation.

> **Try it live:** add your own claim scenario PDF in the application and you'll get the answers back with full detail — every calculation and every question answered with its steps. → **https://caspianagent99-securehealth-adjudication-uistreamlit-app-7ipnmr.streamlit.app/**

The design principle is one line: **the policy is data, the claims are data, the engine is logic — and they never mix.** Nothing about a specific member, plan year, or amount lives in engine code. The repo ships with one sample policy and one sample claim sheet so it runs out of the box, but it is built to be re-run on a new, unseen member or a new policy with zero code change.

- **How it's built and why** → [`DESIGN.md`](DESIGN.md)
- **Why a naive vector-search / RAG approach breaks here** → [`WHY_NOT_RAG.md`](WHY_NOT_RAG.md)

This README is the operator guide.

---

## 1 · Quick start

```bash
cd securehealth-adjudication

# 1. environment + install
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt          # or: pip install -e ".[dev]"

# 2. configure the LLM key (needed for extracting a new policy / new claim)
cp .env.example .env                      # then put a real ANTHROPIC_API_KEY in .env

# 3. run the test suite (fully offline; uses a mock LLM — no key needed)
pytest -q

# 4. run the engine from the CLI on the bundled sample claim
python -m adjudication.cli --format table          # human-readable table
python -m adjudication.cli --format json           # machine-readable JSON
#   point at a different claim PDF / policy:
python -m adjudication.cli --claims path/to/other_member.pdf --policy data/policy/securehealth_plan_b.json

# 5. start the app (single process — UI runs the engine in-process)
streamlit run ui/streamlit_app.py
```

That's it for the app: `streamlit run …` is enough because the UI mounts the FastAPI backend **in-process**, so there's no separate server to start. The bundled, already-approved policy and a saved sample claim run are loaded on open, so you see a full result immediately.

**Optional — run the API as its own service** (e.g. for a separate frontend):

```bash
uvicorn api.main:app --reload            # serves http://127.0.0.1:8000  (docs at /docs)
API_BASE_URL=http://127.0.0.1:8000 streamlit run ui/streamlit_app.py   # UI talks to it over HTTP
```

> **What needs an API key:** the deterministic engine, the test suite, and the bundled (already-enriched) sample run need **no** key. A live `ANTHROPIC_API_KEY` is only used when you extract a **new policy PDF** or adjudicate a **new claim** whose semantic fields haven't been classified yet. Those paths fail with a clear message if the key is missing.

---

## 2 · How the application works

The app is a deliberate human-in-the-loop progression. The state machine (`empty → draft → locked → claims`) is driven entirely by the backend.

1. **Upload a policy** PDF (or text). The LLM proposes a structured config from the wording, with citations back to the source sections; the schema validator checks its structure automatically.
2. **Edit (HITL).** Review the proposed benefits, endorsements, and exclusion rules and fix anything. Edits persist as a draft.
3. **Lock.** Locking **is the human approval** — the draft is frozen to `data/policy/securehealth_plan_b.json`, and the engine is now ready for claims.
4. **Upload a claim** PDF. The system extracts it, validates it, classifies the two semantic fields (pre-existing link, §4.1 category), adjudicates, and returns the settlement table, per-claim reasoning chains, any human-review flags, and the Q1–Q6 audit cards. The result is saved, so refreshing doesn't re-spend LLM calls.

### Trying new, unseen data

The whole point is generalisation, and you can exercise it from the UI:

- **New claim, same policy** → click **Remove claim**, then upload a different claim PDF.
- **New policy** → upload a new policy PDF and go through **Edit → Lock** again.

As long as the new document is the same *type* — a selectable-text PDF in the expected table shape — it works with no code change. (From the CLI: `python -m adjudication.cli --claims path/to/<member>.pdf`.)

---

## 3 · Generalisation: rules-as-data

Re-running on a second, unseen member or a different policy is a data operation, not a code change:

1. **Same policy, new member?** Drop the new claim PDF on disk and pass `--claims …pdf`, or upload it in the UI / `POST /claims/run`.
2. **Different policy?** Upload the new policy PDF (`POST /policy/draft/from-pdf`), review the LLM's proposal against its citations, edit if needed, and lock it (`POST /policy/lock`) — or hand-edit `data/policy/securehealth_plan_b.json` and re-validate.

New benefits, new endorsements, new exclusion rules, new waiting-period flags — all are expressed as data in the policy config; the engine handles them without modification.

---

## 4 · What's where

| Path | Purpose |
|---|---|
| `data/policy/securehealth_plan_b.json` | **Frozen, human-approved policy config** — the artifact the engine reads. |
| `data/claims/03_Claim_Scenario_Main.pdf` | The bundled sample claim sheet (PDF — the only supported claim format). |
| `data/clinical_kb/` | The **curated dataset** that grounds and verifies the LLM classifiers (§4.2 chronic conditions, §4.1 exclusion categories). |
| `src/adjudication/models/` | Pydantic models = typed contracts between layers. |
| `src/adjudication/extraction/` | Format-aware ingestion (PDF claims, LLM-proposes-policy). The only layer that touches file formats. |
| `src/adjudication/validation/` | Structural validation — the trust boundary made concrete. |
| `src/adjudication/services/` | LLM facade, the clinical-KB filter, and the Q1–Q6 derivation service. |
| `src/adjudication/engine/` | Pure, deterministic core: exclusions → calculation → accumulator → adjudicator. |
| `src/adjudication/reporting/` | JSON + human-readable table — both derived from the same `SettlementReport`. |
| `api/` | FastAPI app — the single source of truth for the UI. |
| `ui/streamlit_app.py` | Thin client; runs the engine in-process for single-process deploys. |
| `tests/` | 36 offline, deterministic tests (mock LLM via `tests/_fakes.py`). |

---

## 5 · Auditing how a number was reached

Every settlement carries an ordered `reasoning` list. Each step has a label (e.g. `apply_deductible`), a value, a source (`rule:WP-PREEX-6MO`, `benefit:physiotherapy`, `endorsement:E1:in_network_coinsurance`, `engine`), and a note. The reasoning is emitted *as the engine computes* — it is not narrated after the fact.

```bash
# inspect one claim's full derivation
python -m adjudication.cli --format json | jq '.settlements[] | select(.claim_id=="C5")'
```

Or via the API: `GET /report/json` (full settlement), `GET /report/table` (human-readable), `GET /questions/q1 … /q6` (the six answers with derivations). In the UI, expand the per-claim reasoning chain and the Q1–Q6 cards.

---

## 6 · LLM use — constrained and verified

The LLM is allowed a deliberately narrow surface, and every output is checked before it reaches a number:

- **Policy extraction** — the LLM *proposes* a strictly-shaped JSON config with citations; a schema validator checks structure; a human reviews semantics and locks. Once per policy, never per claim.
- **Two claim fields** — "is this diagnosis linked to the declared chronic condition?" (§4.2) and "is it an excluded category?" (§4.1). The classifier is **grounded in the curated KB** (only the rows for the declared condition / configured categories are put in context), must **cite KB row ids** as evidence, and any low-confidence verdict that would change the outcome is **flagged for human review**.

The LLM never sees a claim amount, never computes a payment, never decides an exclusion outcome, and never touches the accumulator. All arithmetic and all decisions are deterministic code. Tests use an in-repo stub (`tests/_fakes.py`), so the suite is offline and reproducible. See [`DESIGN.md`](DESIGN.md) §3 for the full methodology and [`WHY_NOT_RAG.md`](WHY_NOT_RAG.md) for why retrieval-augmented generation is the wrong tool for the reasoning here.
