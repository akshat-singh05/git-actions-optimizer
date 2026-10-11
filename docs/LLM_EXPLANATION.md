# LLM explanation layer (status: IMPLEMENTED as an opt-in wording layer; NOT verified against the live service)

Module: `gha_optimizer/llm_summary.py`. Entry points: `gha-optimizer analyze --llm-summary`; `llm_summary: true` in the
`POST /api/v1/optimize` body; `run_pipeline(llm_client=...)`.

**What it is.** After the plan, the predictions, the advisory section and the proposed workflow are final, the layer
sends a JSON object of facts to a Gemini model and appends the model's short plain-English summary to the explanation
text. Nothing else reads its output.

**Rule 9 (CLAUDE.md), and how each part is enforced**

| requirement | enforcement | test |
|---|---|---|
| never decides the schedule | runs after `build_schedule` and `write_workflow`; the plan and the YAML are identical with / without it | `test_summary_never_changes_the_plan_or_the_workflow_and_is_appended_when_valid` |
| works fully without the API | off by default; unset key / model, HTTP error, bad body, exception in a custom client => a one-line note, analysis unchanged | `test_gemini_failures_become_llm_errors_without_leaking_the_key`, `test_client_from_env_requires_both_settings`, the pipeline test (exploding client) |
| validate output application-side | `validate_summary`: every number must appear in the facts, no code / workflow text, bounded length, plan figures must be called estimates, "measured" only when replay figures are in the facts; a failing answer is discarded whole | `test_a_grounded_answer_passes`, `test_ungrounded_or_unsafe_answers_are_rejected`, `test_measured_is_only_allowed_...` |
| no accidental data egress | facts hold job ids, runner labels and computed numbers only - no file paths, commit ids, repository names or workflow text; the API key is sent in a header, never in the URL | pipeline test (forbidden strings absent from the prompt), URL / header test |

**Configuration.** `GEMINI_API_KEY` and `GHA_LLM_MODEL` (the model id is deliberately not defaulted). Requests use
`POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent` with the `x-goog-api-key` header,
`systemInstruction`, `contents[].parts[].text` and `generationConfig`; the answer is read from
`candidates[].content.parts[].text`. This follows Google's published reference; it was **not run against the live
service** (no API key was available in development), so the first real call may still reveal a mismatch.

**Limits.** A number check cannot catch a wrong sentence made of correct numbers (for example swapping which job a figure
belongs to); the text is wording, and the numbers and tables above it remain the record. Summaries are not
deterministic. The facts are sent to a third party when the flag is used.
