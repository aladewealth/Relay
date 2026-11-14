---
title: Relay Support
emoji: 📨
colorFrom: indigo
colorTo: gray
sdk: gradio
app_file: app.py
pinned: false
license: apache-2.0
short_description: AI customer-support triage, routing and drafting
---

# Relay

AI customer-support automation. Every incoming message is classified, prioritised,
mined for structured fields, routed to a department, escalated to humans when
needed, and answered with a grounded draft that is checked by guardrails before
an agent sends it. Nothing is hardcoded: decisions come from **SubQ Snap**, drafts
from a real language model.

- Dashboard: `/` · API: `/api/*` · OpenAPI docs: `/api/docs` · Gradio console: `/gradio`
- Source of truth for decisions: SubQ `subq-snap-1.2` (`POST /v1/systemone`)
- Default drafter: `Qwen/Qwen2.5-1.5B-Instruct` (GGUF Q4_K_M) on CPU via `llama-cpp-python`

## Architecture

```
                         ┌───────────────────────────────────────────────────────────┐
  customer message ─────▶│ POST /api/messages                                        │
  (email/chat/form)      │                                                           │
                         │  1. persist ──▶ SQLite (conversations, messages, events)  │
                         │                                                           │
                         │  2. extraction.py  regex candidates                       │
                         │     refs (#A-4471, ORD-123, INV-…, TKT-…), amounts,       │
                         │     dates, emails, capitalised product n-grams            │
                         │                     │                                     │
                         │  3. classifier.py   ▼  ONE SubQ /systemone request        │
                         │     ┌────────────────────────────────────────────┐        │
                         │     │ category (choice)   intent (choice)        │        │
                         │     │ priority (score)    frustrated (noul)      │◀─ SubQ │
                         │     │ time_sensitive      requested_action       │  Snap  │
                         │     │ needs_human (noul)                         │        │
                         │     │ order_reference / product_name /           │        │
                         │     │ amount_in_dispute (choice over candidates  │        │
                         │     │ + "none")                                  │        │
                         │     └────────────────────────────────────────────┘        │
                         │        │ SubQ down / no key ─▶ keyword fallback +escalate │
                         │        ▼                                                  │
                         │  4. routing.py (pure code)                                │
                         │     category ─▶ department; escalation rules + reasons    │
                         │        ▼                                                  │
                         │  5. drafting.py  ── single draft worker (queue) ──┐       │
                         │     prompt = rules + KB (constant, KV-cached)     │       │
                         │            + case facts + history                 ▼       │
                         │     Generator ─┬─ local llama.cpp (Qwen 1.5B)  default    │
                         │                ├─ OpenAI-compatible (LLM_BASE_URL)        │
                         │                └─ SubQ Savant (SUBQ_SAVANT=1)             │
                         │        ▼                                                  │
                         │  6. guardrails: ONE SubQ request (3 nouls) on the draft   │
                         │     fail ─▶ regenerate once in STRICT mode                │
                         │     fail again ─▶ status "needs_review"                   │
                         │     generator error ─▶ no draft, escalate to human        │
                         └───────────────────────────────────────────────────────────┘
                                   │
                                   ▼
             static dashboard (vanilla JS): inbox · conversation · escalations ·
             analytics (Chart.js) · simulate · settings
```

Code layout:

| File | Role |
|---|---|
| `app.py` | Space entry: FastAPI app + Gradio console mounted at `/gradio`, served by uvicorn on :7860 |
| `relay/subq.py` | SubQ Decisions client (retries 429/5xx with backoff, raises on 4xx) |
| `relay/extraction.py` | Regex/heuristic candidate generation |
| `relay/classifier.py` | Builds the single SubQ request, parses answers, keyword fallback |
| `relay/routing.py` | Department map + escalation rules (code, not model) |
| `relay/generators.py` | `Generator` interface and providers |
| `relay/drafting.py` | Grounded prompt, guardrail check, strict regeneration |
| `relay/pipeline.py` | Orchestration, draft worker queue, escalation bookkeeping |
| `relay/db.py` | SQLite persistence |
| `relay/api.py` | REST API + static frontend |
| `relay/samples.py` | 12 sample messages sent through the real pipeline |
| `static/` | Dashboard (HTML/CSS/JS, no build step) |
| `tests/` | pytest: routing, extraction, fallbacks (mocked SubQ), API contracts |

## How SubQ is used

SubQ Snap answers typed questions about a `state`. Relay keeps code in charge and
asks the model only for judgments.

**Per customer message: exactly one `/systemone` request** (7 to 10 questions):

| Question | Type | Used for |
|---|---|---|
| `category` | choice (8 options with descriptions) | department routing; confidence < 0.55 escalates |
| `intent` | choice (15 fine-grained intents) | draft grounding, analytics |
| `priority` | score: low, medium, high, urgent | `round(score)` gives the label; ≥ 2.5 escalates |
| `frustrated` | noul | frustration rate; frustrated + high escalates |
| `time_sensitive` | noul | shown to agents |
| `requested_action` | choice (10 actions) | draft grounding |
| `needs_human` | noul (legal, safety, chargeback, VIP, fraud, repeat contact, complex) | ≥ 0.5 escalates |
| `order_reference` | choice over regex candidates + `none` | extraction with confidence |
| `product_name` | choice over n-gram candidates + `none` | extraction with confidence |
| `amount_in_dispute` | choice over currency-amount candidates + `none` | extraction with confidence |

Extraction questions are only added when code found candidates. Options are
keyed `c0…cN` with the candidate text as the description and mapped back in code.
The state includes the last 6 conversation turns and the count of unresolved
prior customer messages, so SubQ can recognise repeated contact.

**Per draft: one more `/systemone` request** with state `{KNOWLEDGE_BASE, CUSTOMER_MESSAGE, DRAFT}`:
`promises_refund_or_compensation`, `states_policy_not_in_kb`, `tone_professional` (nouls).
Any fail regenerates once with stricter instructions; a second fail sets `needs_review`.

Cost: ~600–1,000 input tokens per classification and ~900 per guardrail check at
$0.04 / 1M tokens, so about $0.00008 per message.

## Escalation rules (in `routing.py`, thresholds editable in Settings)

A conversation goes to the human queue, with every reason stored, when any of these hold:

- `needs_human ≥ 0.5`
- priority score ≥ 2.5 (urgent)
- category confidence < 0.55
- frustrated ≥ 0.5 **and** priority score in [1.5, 2.5) (high)
- the 3rd customer message since the conversation was last resolved
- SubQ unavailable (keyword fallback was used)
- the generator failed to produce a draft

Category → department: billing → Billing; technical_support → Technical Support;
account_access → Account Security; complaint → Customer Success; refund and
shipping_delivery → Returns & Refunds; general_inquiry and other → General.

## API

All responses are JSON and carry `source`, `model`, `latency_ms`.

| Method & path | Body / query | Returns |
|---|---|---|
| `POST /api/messages` | `{customer_name, email?, channel, text, conversation_id?}`; `?wait_draft=true\|false` | analysis, routing, escalation, draft (or `{status:"pending"}`) |
| `GET /api/conversations` | `?status=open\|escalated\|resolved&department=…&escalated=true` | `{items, count}` |
| `GET /api/conversations/{id}` | | `{conversation, messages (with analysis), events}` |
| `POST /api/conversations/{id}/reply` | `{text, send=true, author}`; `send:false` saves the edited draft | updated conversation |
| `POST /api/conversations/{id}/regenerate` | `?wait=true\|false` | new draft |
| `POST /api/conversations/{id}/escalate` | `{reason}` | updated conversation |
| `POST /api/conversations/{id}/resolve` | | updated conversation |
| `POST /api/conversations/{id}/assign` | `{assignee}` | updated conversation |
| `GET /api/analytics` | `?days=14` | volume by day and hour, category, department and priority distributions, escalation, resolution and frustration rates, average first response, latencies |
| `GET /api/health` | `?force=true` bypasses the 60 s cache | SubQ status (`ok` / `missing_key` / `error`), generator status |
| `GET /api/settings`, `PUT /api/settings` | `{company_name, kb_text, needs_human_threshold, category_confidence_min, frustrated_threshold, high_priority_score, urgent_priority_score, max_unresolved_messages, guardrail_threshold}` | settings |
| `POST /api/seed`, `GET /api/seed` | | starts / reports the sample inbox run |

Example:

```bash
curl -s https://aladewealth-relay-support.hf.space/api/messages -H 'content-type: application/json' \
  -d '{"customer_name":"Ada","email":"ada@x.com","channel":"email","text":"Charged twice for INV-20931 ($99). Please fix."}'
```

## Environment variables

| Var | Default | Meaning |
|---|---|---|
| `SUBQ_API_KEY` | none | SubQ key (Space **secret**). Without it the app runs, health says `missing_key`, and every message uses the fallback and is escalated. |
| `SUBQ_MODEL` | `subq-snap-1.2` | Decision model (`subq-snap-latest` also works) |
| `SUBQ_BASE_URL` | `https://api.subq.ai/v1` | |
| `RELAY_DB_PATH` | `/tmp/relay.db` | SQLite path (ephemeral on a free Space; use `/data/relay.db` with persistent storage) |
| `GENERATOR` | auto | `none` disables drafting; `openai` / `subq_savant` force a provider |
| `SUBQ_SAVANT` | unset | `1` uses SubQ Savant (`subq-savant-1.4`) via chat/completions |
| `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` | unset | any OpenAI-compatible endpoint (OpenAI, Groq, Together, vLLM, Ollama…) |
| `LOCAL_MODEL_REPO`, `LOCAL_MODEL_FILE` | `Qwen/Qwen2.5-1.5B-Instruct-GGUF`, `qwen2.5-1.5b-instruct-q4_k_m.gguf` | local GGUF |
| `LOCAL_MODEL_PATH` | unset | use a GGUF already on disk |
| `LOCAL_MODEL_THREADS` | CPU count | llama.cpp threads |
| `RELAY_PRELOAD_MODEL` | `1` | download, load and KV-warm the local model at startup |

Provider precedence: `GENERATOR=none` → `SUBQ_SAVANT=1` → `LLM_BASE_URL` → local.

## Adding a model

1. Subclass `Generator` in `relay/generators.py`:
   ```python
   class MyGenerator(Generator):
       name, model = "my_provider", "my-model"
       def status(self): return {"provider": self.name, "model": self.model, "ready": True, "error": None}
       def generate(self, messages, max_tokens=320):  # OpenAI-style messages -> str
           ...  # raise GeneratorError on failure
   ```
2. Return it from `make_generator()` behind an env switch.
3. Any OpenAI-compatible server needs no code: set `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`.
4. A different local model needs no code either: set `LOCAL_MODEL_REPO` / `LOCAL_MODEL_FILE` to any chat GGUF
   (for example `Qwen/Qwen2.5-0.5B-Instruct-GGUF` + `qwen2.5-0.5b-instruct-q4_k_m.gguf` for roughly 3× faster, weaker drafts).

Guardrails and routing do not depend on the generator, so every provider gets the same checks.

## Performance notes (2 vCPU)

- Classification: one SubQ call, ~0.4–0.6 s.
- Local draft (Qwen2.5-1.5B Q4_K_M, 2 threads): ~25 s once the rules + KB prefix is in the KV cache
  (the system prompt is byte-identical across calls, so llama.cpp only evaluates the case-specific turn).
  The first draft after the KB changes re-evaluates the prefix (~60 s). The startup warm-up pays that cost before traffic arrives.
- Drafts are generated by one background worker. The UI shows them as pending and polls.

## Run locally

```bash
pip install -r requirements.txt fastapi gradio pytest
export SUBQ_API_KEY=...            # optional
python app.py                      # http://localhost:7860
python -m pytest -q tests
```
