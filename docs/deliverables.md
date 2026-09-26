# Deliverables: documents from the Command Centre

Built 26 September 2026 (plan: "Command Centre Deliverables Uplift Plan").

The Deliverables view (`library` in `ui/command_centre.html`) drafts, renders,
reviews and approves client documents. Nothing reaches Client-ready without
Aaron's approval, and the system never sends a document to anyone.

## What it makes

| Type | Output | Drafted by | Reviewed by |
| --- | --- | --- | --- |
| Client briefing | DOCX, then PDF | research_intelligence / sector-intelligence-analyst | operations / quality-reviewer |
| Decision paper | DOCX, then PDF | research_intelligence / insights-strategist | operations / quality-reviewer |
| Proposal (no fees) | DOCX, then PDF | marketing_sales / sales-manager, polished by copywriter | operations / quality-reviewer |
| Slide deck (from an existing deliverable) | PPTX, then PDF | marketing_sales / content-creator | operations / quality-reviewer |

The registry is `orchestrator/doc_types.py`.

## Where things live

- Record and source of truth: `<vault>/13_Command Centre/Deliverables/<id>.md`.
  The frontmatter is the record; the body is the document. Anything after the
  line `%% wijerco:working-notes %%` is working notes (brief, evidence gaps,
  quality reviews, drawer agent output) and is never rendered.
- Files: `Deliverables/_files/<id>/<id>-v<N>.docx|pptx|pdf`. Every render is a
  new version; nothing is overwritten. Edit the note, not the Word file.
- Archived notes: `Deliverables/_archive/`.
- Code: `orchestrator/deliverables.py` (pipeline and endpoints),
  `media/doc_render.py` (DOCX and PPTX), `media/office_convert.py` (PDF through
  Word), `templates/docs/wijerco-reference.docx` (styles, Open Sans).

## How a document moves

A generated deliverable is a production with a document format. It uses the
production state machine and skips `asset_plan`:

idea -> brief -> research -> outline -> draft -> render (DOCX v1) -> review
(quality review) -> **client_sensitive gate** -> publish (final render, PDF,
Client-ready) -> measure

- `POST /deliverables` creates the note and production and runs to review in
  the background.
- The draft is written one section at a time, in order. Each section gets a
  word budget from the type's target (`words` in doc_types.py, weighted so
  Options or Findings get more than a Summary) and sees the sections already
  written, so it builds on them instead of repeating them. A section under 60%
  of its budget gets one expansion pass. The Sources section is built from the
  research step's source list and keeps only the sources the text cites. `production.advance()` delegates document formats to
  `deliverables.advance_document()`.
- The daemon does not mint tasks for documents (`operating.sync_production_tasks`
  skips them), so nothing advances a document twice.
- Review to publish requires only `client_sensitive` for documents
  (`governance.required_for_transition`). Approve from the drawer, Operating's
  approvals, or Telegram `/pending`. Every path goes through
  `governance.approve()`, which calls `deliverables.on_gate_decision()`: an
  approval finalises the document, a rejection records "Changes requested" in
  the note.
- Readiness is computed on the server from the production state and the latest
  gate decision. Client-ready means approved and finalised, nothing else.

## Endpoints

| Method and path | Role | Purpose |
| --- | --- | --- |
| GET /deliverables | api key | Real notes only. `?demo=1` returns sample items |
| GET /deliverables/types | api key | Type registry for the composer |
| POST /deliverables | admin | Create and draft to review |
| GET /deliverables/{id} | api key | Body, working notes, versions, production, approvals |
| POST /deliverables/{id}/render | admin | Re-render the note as a new version (locked once approved) |
| GET /deliverables/{id}/file/{v}?fmt=docx\|pptx\|pdf | api key | Download a version |
| POST /deliverables/{id}/action | admin | summary, quality, evidence, reuse; output saved to working notes |
| POST /deliverables/{id}/request-approval | admin | Re-notify after changes |
| POST /deliverables/{id}/run | admin | Continue drafting after a failure or restart |
| POST /deliverables/{id}/archive | admin | Move the note to `_archive`, cancel an unfinished production |

## Settings

| Variable | Default | Effect |
| --- | --- | --- |
| DELIVERABLES_PDF | word | `off` disables PDF export (Windows only in any case) |
| DELIVERABLES_PDF_TIMEOUT | 180 | Seconds before a hung Word conversion is killed |
| DELIVERABLES_RETRIEVAL | on | `off` skips knowledge-base retrieval in the research step |
| DELIVERABLES_NOTIFY | on | `off` stops "ready for approval" notifications |
| DOC_TEMPLATE_DOCX | templates/docs/wijerco-reference.docx | Word styles template |
| DELIVERABLES_DRAFT_MODEL | unset | Pin the model that writes sections, e.g. `google/gemini-2.5-flash` or `anthropic/claude-sonnet-4-6`. Unset uses the normal cost-ordered routing |

## Deploying to wijerco

1. Double-click `deploy\wijerco-update.bat` (pulls the code; the import check
   passes because the Office libraries load lazily).
2. Double-click `deploy\wijerco-deliverables-setup.bat` once. It installs
   python-pptx and markdown-it-py, installs Open Sans for the user if missing,
   and probes Word with a real conversion. If the probe fails, open Word once,
   clear any first-run or sign-in prompt, and run it again.

## Measured on wijwork, 27 September 2026 (section-by-section drafting)

| Document | Target words | Before | After | Agent cost |
| --- | --- | --- | --- | --- |
| Client briefing | 1,400 to 2,200 | 537, then 878 | 1,756 | US$0.015 |
| Decision paper | 1,400 to 2,800 | about 1,200 | 2,718 | US$0.018 |
| Proposal | 1,800 to 3,200 | not measured | 2,480 | US$0.019 |

The draft step takes 20 to 50 seconds. Section drafting roughly doubles the
agent cost of a document, still under two cents on the default routing.

## Measured on wijwork, 26 September 2026

- Decision paper from a four-sentence brief: review reached in about 5.5
  minutes, most of it a one-off reranker model load in research; about 1,200
  words, US$0.008 in agent calls.
- Slide deck from that paper: 11 slides with speaker notes in about 50 seconds.
- Approval to Client-ready with PDF: about 20 seconds, most of it Word. The
  final DOCX is about 2.7 MB because Open Sans is embedded in full.
