# Why a naive RAG approach breaks here

> The brief asks for a short note on where a naive vector-search / RAG pipeline would fail on this task, and how this design avoids it. This is that note.

A naive RAG pipeline — embed the policy and claims, retrieve the top-k chunks most similar to a question, and let the LLM answer from them — fails this task in three concrete ways. The failures are not tuning problems; they are structural. Adjudication is **rule application over shared state with exact arithmetic**, and similarity search addresses none of those.

### 1. It retrieves chunks and answers locally — so it misses cross-section overrides

The correct physiotherapy coinsurance lives at the *intersection* of two sections: §2 (Table of Benefits) says **20%**, and §5 (Endorsement E1) says **10% and prevails**. A retriever scoring chunks by lexical/embedding similarity has no notion of "§5 overrides §2." Whichever chunk wins the similarity contest, the other is dropped, and the model answers from a partial view — often confidently wrong.

**This design** resolves overrides as data: §2 produces a base `Benefit`, §5 produces a separate `Endorsement`, both are stored independently, and the engine resolves the override at runtime **recording both values** ("base 20% → E1 10% → applied"). The relationship is encoded, not inferred from retrieval scores.

### 2. It is stateless across claims — so it can't carry a running balance

Even if RAG answered each claim correctly in isolation, six independent answers don't share a budget. The Annual Aggregate Limit (AED 250,000) and each per-benefit sub-limit are **running balances** that depend on the *order* claims are processed in. Top-k retrieval has no memory of what earlier claims already consumed; it cannot clip the sixth claim because the sub-limit was exhausted by the fourth.

**This design** has a purpose-built `LimitAccumulator`: claims are sorted by service date, the ledger decrements per insurer payment, each payment is clipped to what remains in the benefit sub-limit and the aggregate, and any clip is recorded as a reasoning step. Order-dependent state is modelled explicitly.

### 3. It can't do reliable arithmetic — so the numbers aren't auditable

Deductibles, coinsurance splits, the 20% pre-auth penalty, limit clipping, two-decimal AED rounding — LLM arithmetic is brittle and, worse, *unauditable*: you get a number with no defensible derivation.

**This design** executes the policy's GC-1 calculation order step by step in deterministic code, attaches each intermediate value as a sourced `ReasoningStep`, and produces the same output for the same input every time. Every figure can be traced to the rule that produced it.

---

## Where retrieval-*shaped* grounding does appear — and why a filter beat RAG

Two claim questions genuinely need clinical judgment — "is this diagnosis linked to the declared chronic condition?" (§4.2) and "is it an excluded category like cosmetic/self-inflicted/experimental?" (§4.1) — and an LLM classifier handles them. To keep the model grounded rather than free-recalling, it is given a relevant slice of a curated knowledge base as context.

The first instinct was RAG: embed the clinical corpus and vector-search for rows similar to the diagnosis. On reflection, that was the wrong tool. **The relevant slice is fully determined by a key we already know** — the member's *declared* chronic condition (read from the claim header) and the policy's *configured* §4.1 categories — not by anything fuzzy in the diagnosis text. So a **deterministic filter** (`ClinicalKB.for_condition()`, `ExclusionCategoryKB.for_categories()`) selects exactly the right rows by exact key.

A filter wins here on every axis that matters:

- **More precise** — it returns *all and only* the rows for the known key; nearest-neighbour scoring can miss a relevant row or pull in a similar-but-wrong one.
- **Simpler** — no embedding model, no vector index to build, persist, or keep from drifting.
- **Reproducible** — the same diagnosis always gets the same context, which keeps the whole classification auditable.
- **Verifiable** — the model must cite KB row ids, and cited ids are validated against the filtered set, so a hallucinated citation is dropped.

In short: RAG is for when *what to retrieve* is itself a fuzzy similarity question. Here it never was — the right context is addressable by an exact key — so a filter is both simpler and stronger. And the part RAG is genuinely bad at (cross-section override resolution, order-dependent limits, exact arithmetic) is handled by deterministic code, not a language model at all.
