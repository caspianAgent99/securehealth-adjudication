# Claim Adjudication Engine — System Design

**Project:** Data Extraction & Reasoning Engine — SecureHealth claim adjudication
**Companion docs:** [`README.md`](README.md) (how to run, app walkthrough) · [`WHY_NOT_RAG.md`](WHY_NOT_RAG.md) (why naive vector-search/RAG fails here)

This document explains *how the system is built and why*: the methodology behind each pipeline, how the rules and claims are represented as data, how settlements are calculated, how the LLM is constrained and verified, and how the pieces fit together. It is the architectural reference; the README is the operator guide.

---

## 1. The governing principle

The task is not "compute one member's six claims." It is "build a machine that turns *any* SecureHealth-style policy and *any* claim sheet into auditable, correct settlements," because the solution is re-run on a second, unseen member with different dates, providers and amounts. One rule governs every decision here:

> **The policy is data. The claim sheet is data. The engine is logic. The three never mix.**

If a design choice would bake a specific member, date, benefit, or amount into logic, it is wrong. Nothing about the bundled member, the 2025 plan year, or the AED amounts lives in engine code — they are all expressed as data in `data/policy/securehealth_plan_b.json` and the claim PDF.

### Two cross-cutting commitments

1. **Explainable** — every number carries the chain of reasoning that produced it. Reasoning is *attached as the claim passes through each stage*, not reconstructed after the fact.
2. **Deterministic core** — no LLM, no clock, no I/O inside the calculation path. Same input → same output, always. The LLM lives only at the slow-changing ingestion edge, behind validation and a human gate.

---

## 2. Architecture at a glance

There are two pipelines. The **policy pipeline** runs rarely (once per policy) and is the only place unconstrained LLM generation happens — behind validation and a human gate. The **claim pipeline** runs per claim and is deterministic, except for two narrow, KB-grounded LLM lookups. Each arrow is a typed Pydantic contract.

**Pipeline A — Policy ingestion (rare, human-approved):**

```
  Policy PDF
     │  pdfplumber (text)
     ▼
  LLM proposes config + citations        ← the only free-generation step
     │
     ▼
  Schema validation (automatic)
     │
     ▼
  Human review / edit / LOCK             ← approval gate, once per policy
     │
     ▼
  securehealth_plan_b.json  (frozen artifact the engine runs on)
```

**Pipeline B — Claim adjudication (per claim, deterministic core):**

```
  Claim PDF
     │  pdfplumber tables → normalize → schema validation
     ▼
  typed Claim[]
     │  LLM enrichment (KB-grounded) adds two fields only:
     │     • pre_existing_link   (§4.2)
     │     • category_flags      (§4.1)
     ▼
  ADJUDICATION ENGINE  (pure, deterministic)
     gate  →  calculation  →  accumulator
     ▼
  SettlementReport   (+ Q1–Q6 cards)
```

Both pipelines cross a **trust boundary** via validation:

- **Policy path** — the LLM *proposes*; a schema validator checks structure automatically; a human reviews semantics once and locks. Reviewed once per policy, never per claim.
- **Claim path** — a deterministic parser *transcribes* the table. The only judgment calls — "is this diagnosis linked to the declared chronic condition?" (§4.2) and "is this an excluded category?" (§4.1) — are made by an LLM classifier **grounded in a curated knowledge base that we built for exactly this purpose** (`data/clinical_kb/`); any low-confidence verdict that would change the outcome is flagged for human review. **The dataset is meant to improve continuously: whenever a datapoint trips the human-review flag, the human's resolution becomes a new KB row, so the same case is grounded (and decided with high confidence) next time.** Over time the share of claims needing review shrinks as the KB absorbs the cases it once couldn't ground.

---

## 3. Methodology

### 3.1 Policy extraction — pdfplumber + LLM + human-in-the-loop

The policy wording is dense, cross-referential prose (a Table of Benefits in §2, General Conditions in §3, Exclusions in §4, an Endorsement in §5). The right answer for a benefit often lives at the *intersection* of two sections — e.g. physiotherapy coinsurance is 20% in §2 but Endorsement E1 in §5 overrides it to 10%. Turning that into reliable structured data is exactly the kind of task an LLM is good at and deterministic parsing is bad at — **but an LLM's output cannot be trusted blindly**. The pipeline therefore constrains and verifies it at every step:

1. **Extract text deterministically.** `pdfplumber` pulls the policy text layer (no OCR — see §8 scope).
2. **LLM proposes a strictly-shaped config.** `AnthropicProvider.propose_policy()` sends the text with a system prompt that specifies the **exact JSON schema** (benefits, endorsements stored separately, parameterized exclusion rules, calculation order) and asks for **per-field citations** back to the source section. The LLM *structures* rules; it never computes anything.
3. **Schema validation (structural, automatic).** `validate_policy_config()` rejects anything that doesn't fit the Pydantic contract — coinsurance outside [0,1], an endorsement targeting a non-existent benefit, an unknown calculation step, a non-positive aggregate limit. Structural correctness needs no human.
4. **Human-in-the-loop review (semantic, once).** What passes structural validation is surfaced in the UI with its citations, so a reviewer can confirm the things a schema can't — e.g. "did it catch that §5 E1 overrides §2?" The reviewer can edit any field, then **locks** the policy. *Locking is the human approval act.*
5. **Freeze.** The approved config is written to `data/policy/securehealth_plan_b.json`. That frozen file — not the LLM — is what the engine runs against.

This puts the only unconstrained generation step at the **slowest-changing layer** (the policy, which changes maybe once a year), behind a structural gate and a human gate. The fast-changing per-claim path never re-invokes it.

### 3.2 Claim extraction — deterministic, no LLM for the table

Structured input does not need a language model. `PDFClaimExtractor` reads two tables from the claim PDF with `pdfplumber`: the member header (member id, inception date, declared chronic condition → `MemberContext`) and the claim events table (→ normalized rows). Header aliasing and cell-normalization handle PDF quirks like mid-word wrapping; money/date/enum fields are normalized deterministically. Using an LLM here would add nondeterminism for no benefit. Every row then passes `validate_claim_rows()` → typed `Claim` objects; any bad cell becomes a precise per-row error (e.g. "benefit 'Physio' not found in policy"), never a silently-wrong number.

### 3.3 The two semantic claim fields — LLM constrained by a curated dataset

Two questions about a claim genuinely require clinical judgment, and only these two touch the LLM on the claim path:

- **§4.2 pre-existing link** — is the diagnosis related to the member's *declared* chronic condition? (Drives the 6-month waiting-period exclusion.)
- **§4.1 general-exclusion category** — is the diagnosis cosmetic / self-inflicted / experimental? (Drives the §4.1 hard exclusions.)

We **built a dataset to constrain and verify these calls** — two curated knowledge bases under `data/clinical_kb/`:

- `chronic_conditions.json` — ~100 rows mapping chronic conditions (asthma, hypertension, T2DM, COPD, …) to their clinical indicators (symptoms, treatments, monitoring, acute complications), each with a stable id like `ASTHMA-007`.
- `exclusion_categories.json` — indicators for the §4.1 categories (cosmetic, self-inflicted, experimental), each with an id like `COSMETIC-004`.

How the dataset constrains and verifies the LLM:

1. **Grounding, not free recall.** The classifier prompt includes **only the KB rows relevant to the question** — the rows for the declared condition, or for the requested categories. The model is asked to decide *and cite the row ids it used as evidence*.
2. **Evidence is validated against the KB.** Cited ids that don't exist in the KB are dropped — a direct guard against hallucinated citations (`LLMService.classify_*`).
3. **Two-level confidence + review gate.** Confidence collapses to `high` or `low`; anything not strongly evidenced is `low` and sets `requires_review`.
4. **Materiality check (the verification step that matters most).** A `requires_review` flag is only surfaced to the human if flipping the verdict would actually change the claim's payable/excluded outcome (`preexisting_verdict_is_material`, `category_verdict_is_material`). An uncertain LLM verdict on a claim that is excluded anyway (or already past the waiting period) is correctly treated as moot — the human's attention is spent only where it changes money.

> **From RAG to a filter.** The grounding step in (1) was the design decision that started as "use RAG." The first instinct was to embed the clinical corpus and vector-search for chunks related to a diagnosis to put in the LLM's context. On reflection, the relevant slice is fully determined by a *known key* — the declared condition, or the policy's configured §4.1 categories — so a **deterministic filter** (`ClinicalKB.for_condition()`, `ExclusionCategoryKB.for_categories()`) selects exactly the right rows. It is simpler, has no embedding index to build or drift, is fully reproducible, and is *more* precise than nearest-neighbour scoring for this shape of problem. (The broader argument against RAG for the adjudication math itself is in [`WHY_NOT_RAG.md`](WHY_NOT_RAG.md).)

---

## 4. The data model — rules as data

Every boundary passes a typed Pydantic model, never a loose dict. The models *are* the contracts.

### Policy side (`models/policy.py`)
- **`Benefit`** — one Table-of-Benefits row: `key` (stable join id), name, `annual_sub_limit` (nullable — inpatient has none of its own), `in_network_coinsurance`, `out_of_network_coinsurance` (nullable — `None` means *OON not covered*, which is distinct from 0%), `deductible`, `requires_preauth`.
- **`Endorsement`** — a layered override: which `benefit_key` it targets, an `overrides` map of field→new value, and a `source` citation. **Stored separately, never merged into the base benefit**, so the engine can record "base 20% → E1 10% → applied."
- **`ExclusionRule`** — a named, parameterized rule with a `type` (`waiting_period` / `preauth_penalty` / `not_covered_oon` / `not_covered_condition`), `params`, an optional `applies_to_benefits` scope, and a `reason_template`. New exclusions are new data, not new code.
- **`PolicyConfig`** — the whole plan: aggregate limit, benefits, endorsements, exclusion rules, `calculation_order` (GC-1 as data), and approval metadata. Self-validates (endorsements/rules must reference real benefits; calculation steps must be known).

### Claim side (`models/claim.py`, `models/member.py`)
- **`PreAuthStatus`** — three states, not a boolean: `NOT_APPLICABLE` vs `OBTAINED` vs `NOT_OBTAINED`. Collapsing "n/a" and "no" would break the penalty logic.
- **`NetworkStatus`** — `IN_NETWORK` / `OUT_OF_NETWORK`.
- **`Claim`** — raw fields (id, service date, benefit key, network, billed amount, pre-auth, diagnosis) plus **derived** fields that never overwrite the raw ones: `pre_existing_link` (with reasoning, confidence, KB evidence ids, `requires_review`) and the `category_flags*` family.
- **`MemberContext`** — per-member, kept separate from the per-plan policy: member id, `inception_date` (anchors §4.2), declared chronic conditions.

### Settlement side (`models/settlement.py`)
- **`ReasoningStep`** — one audit link: label, value, source (`rule:…`, `benefit:…`, `endorsement:…`, `engine`), note.
- **`ClaimSettlement`** — per claim: billed, eligible, deductible, member/insurer coinsurance, penalty, insurer-paid, member-paid, `decision` (`PAYABLE` / `EXCLUDED` / `PAYABLE_WITH_PENALTY`), reason, the ordered `reasoning` list, and `requires_review`.
- **`SettlementReport`** — all settlements + year totals + aggregate remaining + an accumulator snapshot.

---

## 5. How a settlement is calculated

The engine (`engine/`) is pure: no I/O, no LLM, no clock, no global state. Given the same `(claims, policy, member)`, it always produces the same report. The orchestrator (`adjudicator.py`) runs three stages per claim, in service-date order.

**Stage 1 — exclusion gate (`exclusions.py`), runs first.** Evaluates the policy's `ExclusionRule`s and emits a reason-bearing `GateOutcome`: payable, excluded (hard), or payable-with-modifier. This is where:
- **§4.2 waiting period** excludes a claim if its `pre_existing_link.is_related` and the service date is before `inception_date + waiting_months` (anchored on the *member's* inception date when present, else `policy_start_date`).
- **OON not covered** excludes an out-of-network claim for a benefit whose `out_of_network_coinsurance` is `None` (e.g. pharmacy) — enforced both by an explicit rule and as a defensive fallback.
- **§4.1 categories** exclude when a configured `condition_flag` matches the claim's `category_flags`.
- **Pre-auth penalty** attaches a 20% modifier (not an exclusion) when a benefit `requires_preauth` and pre-auth is `NOT_OBTAINED` — so the claim is reduced, not discarded.

**Stage 2 — calculation core (`calculation.py`), pure per-claim.** Resolves the endorsement override (recording base→override as reasoning), then executes `calculation_order` step by step: `cap_to_eligible` (min of billed and R&C-eligible) → `apply_deductible` → `apply_coinsurance` (split into member and insurer shares). Any pre-auth penalty from Stage 1 reduces the insurer share and is added to the member's liability. Every intermediate is rounded to two AED decimals and attached as a `ReasoningStep`.

**Stage 3 — limit accumulator (`accumulator.py`), the one stateful component.** Because the annual aggregate (AED 250,000) and each per-benefit sub-limit are *running balances*, claims must be processed in order. The accumulator decrements per insurer payment, **clips** each payment to what remains in the benefit sub-limit and the aggregate, shifts any clipped overage to the member, and records when a limit bites. This is why claims can't be mapped over independently — they share an order-dependent budget.

The adjudicator assembles each `ClaimSettlement` (concatenating gate + calc + clip reasoning), accumulates year totals, and decides `requires_review` via the materiality check. `QuestionsService` then derives the Q1–Q6 audit cards from the same report.

---

## 6. How the application works

The UI (`ui/streamlit_app.py`) is a thin client over the FastAPI app (`api/`); `GET /policy/state` is the single source of truth for which screen to show. The flow is a deliberate human-in-the-loop progression:

1. **Upload a policy** PDF (or text). The LLM proposes a config; the schema validator checks it.
2. **Edit (HITL)** — review the proposed benefits, endorsements, and exclusion rules with their citations, and fix anything. Edits persist as a draft on disk.
3. **Lock** — locking **is the human approval**: the draft is frozen to `securehealth_plan_b.json` and the engine is now ready for claims.
4. **Upload a claim** PDF → the system extracts, validates, enriches the two semantic fields, adjudicates, computes Q1–Q6, and returns everything (settlement table, per-claim reasoning chains, review flags, Q1–Q6 cards). The result is persisted, so refreshing doesn't re-spend LLM calls.

**Defaults and trying unseen data.** On first open the application already has a locked policy *and* a saved claim run, so the bundled scenario is visible immediately. To try **new, unseen data**: click **Remove claim** and upload a different claim PDF; or upload a **new policy** PDF and go through edit → lock again. As long as the new document is the same *type* (a selectable-text PDF in the expected table shape), it works with zero code change — that is the generalisation the brief asks for, demonstrated end-to-end.

The backend can run two ways from the identical route code: as a standalone API (`uvicorn api.main:app`), or **in-process inside the Streamlit app** via a synchronous ASGI test client when no `API_BASE_URL` is set — which is what allows the whole thing to deploy as a single Streamlit Community Cloud app.

---

## 7. Codebase tree

```
claim-adjudication-clean/
├── README.md                         # approach + how to run + app walkthrough
├── DESIGN.md                         # this document
├── WHY_NOT_RAG.md                    # the ½-page RAG note
├── pyproject.toml / requirements.txt # deps, tooling, pytest config
├── .env.example                      # ANTHROPIC_API_KEY, model, paths (no secrets)
│
├── data/
│   ├── policy/securehealth_plan_b.json   # FROZEN, human-approved policy config (the artifact the engine runs on)
│   ├── claims/03_Claim_Scenario_Main.pdf # the bundled claim sheet (PDF — the only supported claim format)
│   └── clinical_kb/
│       ├── chronic_conditions.json       # curated KB grounding the §4.2 pre-existing classifier
│       └── exclusion_categories.json     # curated KB grounding the §4.1 category classifier
│
├── src/adjudication/
│   ├── models/          # Pydantic contracts between layers
│   │   ├── policy.py        # Benefit, Endorsement, ExclusionRule, PolicyConfig
│   │   ├── claim.py         # Claim, PreExistingLink, PreAuthStatus, NetworkStatus
│   │   ├── member.py        # MemberContext (per-member, anchors §4.2)
│   │   └── settlement.py    # ReasoningStep, ClaimSettlement, SettlementReport, Decision
│   │
│   ├── extraction/      # the format-aware ingestion edge (quarantined)
│   │   ├── base.py          # Extractor protocol
│   │   ├── claim_pdf.py     # deterministic pdfplumber claim-table → rows + member context
│   │   ├── policy_llm.py    # LLM-proposes-config → validate (never trusted directly)
│   │   └── row_schema.py    # normalized row contract + field normalizers
│   │
│   ├── llm/             # pluggable LLM transport
│   │   ├── provider.py          # LLMProvider protocol (narrow surface)
│   │   ├── anthropic_provider.py# real Claude; strict-JSON prompts
│   │   └── types.py             # proposal / classification result types
│   │
│   ├── services/        # app-facing facades
│   │   ├── llm_service.py        # single LLM entry point; prompt assembly, KB filtering, confidence/evidence normalization
│   │   ├── clinical_kb.py        # the deterministic KB filter (for_condition / for_categories)
│   │   └── questions_service.py  # derives Q1–Q6 cards from a SettlementReport
│   │
│   ├── validation/      # the trust boundary, made concrete
│   │   ├── policy_validator.py   # structural checks on proposed config
│   │   └── claim_validator.py    # rows → typed Claim, loud failures
│   │
│   ├── engine/          # THE DETERMINISTIC CORE — no I/O, no LLM, no clock
│   │   ├── exclusions.py    # the gate (+ materiality checks)
│   │   ├── calculation.py   # GC-1 ordered math, endorsement resolution
│   │   ├── accumulator.py   # stateful sub-limit + aggregate ledger
│   │   └── adjudicator.py   # orchestrates gate → calc → accumulate
│   │
│   ├── enrichment.py    # attaches pre_existing_link + category_flags via LLMService
│   ├── reporting/       # json_report.py + table_report.py (both from one SettlementReport)
│   ├── config.py        # settings (provider, paths, env)
│   └── cli.py           # `python -m adjudication.cli` — run the engine, print table/JSON
│
├── api/                 # FastAPI — single source of truth for the UI
│   ├── main.py / routes.py / dependencies.py
│
├── ui/streamlit_app.py  # thin client; runs the API in-process for single-process deploys
│
└── tests/               # 36 offline, deterministic tests (mock LLM via tests/_fakes.py)
    ├── test_exclusions.py / test_calculation.py / test_accumulator.py
    ├── test_adjudicator.py        # full scenario → expected outcomes + year totals
    ├── test_extraction.py / test_validation.py / test_llm_service.py
```

---

## 8. Scope and assumptions

These are deliberate, time-boxed boundaries — stated so they can be challenged.

- **Question set is fixed.** The system is built to answer the assessment's six question *types* (extraction, single-rule calc, exclusions, full calc, structured statement) and to generalise across *members and policies*. It assumes the **question set itself does not change**, since nothing in the brief indicated otherwise. New question types would be new `QuestionsService` derivations over the same report.
- **Document type is fixed: selectable-text PDF.** Both source documents carry a real text layer, so `pdfplumber` (text/table extraction, no OCR) is the **most straightforward and most reliable** choice — it is deterministic and exact, where OCR introduces a recognition-error surface. The claim parser assumes the table shape and that **dates and amounts are well-formed**; malformed cells fail loudly at validation rather than producing wrong numbers.
- **If the document type changes, the technique should change with it.** The extraction edge is isolated behind the row contract precisely so a different input is *one new extractor*, zero engine change. A scanned/image policy or claim (e.g. a PNG, or a photographed form) would need a different technique — **OCR, or a vision model** — chosen by document type. That is out of scope for this task: **the current solution assumes text-layer PDFs and will not handle a completely different format (e.g. an image) as-is.**
- **Currency & rounding.** AED throughout; two-decimal rounding at every monetary step.

---

## 9. Determinism, audit, and testing

- **Determinism.** The entire calculation path is LLM-free and clock-free. The LLM's outputs enter only as *data on the claim* (the two semantic fields), after KB-grounding, evidence validation, and a materiality-gated review — and once enriched, the same inputs always yield the same settlement.
- **Audit.** Every settlement carries an ordered `reasoning` list whose steps name their source (`rule:WP-PREEX-6MO`, `endorsement:E1:in_network_coinsurance`, `benefit:physiotherapy`, `engine`). The reasoning is emitted by the engine as it computes — it is not narrated after the fact. Inspect via `GET /report/json`, the CLI, or the per-claim expanders in the UI.
- **Testing.** 36 tests run fully offline against a mock LLM (`tests/_fakes.py`): the gate, the calculation core, and the accumulator are tested in isolation; the adjudicator is tested end-to-end against the expected per-claim outcomes and year totals; validation tests assert malformed config/rows fail loudly. No network, fully reproducible.

---

## 10. How the design answers the brief

The brief weights **generalisation** and **correct exclusions** most heavily, and demands auditability. Mapping each evaluation axis to a mechanism above:

| Brief expectation | Where the design delivers it |
|---|---|
| **Rules as data, not hardcoded** | `PolicyConfig` + frozen JSON; engine reads benefits, endorsements, exclusion rules, and the GC-1 calculation order as data (§4–5). |
| **Generalises to an unseen member/policy** | No member/date/amount in engine code; new claim PDF or new policy PDF runs with zero code change (§1, §6). |
| **Cross-section override (§5 over §2)** | Endorsements stored separately and resolved at runtime, recording base→override (§4–5). |
| **Every exclusion handled, with the clause** | Parameterized exclusion gate emits the policy's own `reason_template` per rule; waiting period, OON-not-covered, §4.1 categories, and the pre-auth penalty modifier all covered (§5). |
| **Auditable derivations** | `ReasoningStep` chain on every settlement, sourced and ordered (§9). |
| **Structured output (JSON + table)** | Both rendered from one `SettlementReport`, so they cannot disagree (`reporting/`). |
| **Constrain & verify the LLM** | Narrow two-method surface, strict-JSON prompts, schema validation, human lock gate, KB grounding, evidence validation, confidence + materiality review (§3). |
| **Where naive RAG breaks** | [`WHY_NOT_RAG.md`](WHY_NOT_RAG.md). |
