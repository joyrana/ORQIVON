# Connecting Models

The platform talks to models through the OpenAI Chat Completions API. Anything that serves that
API works: vLLM, TGI, llama.cpp server or Ollama for self-hosted models, and Azure OpenAI,
OpenRouter, Together or similar for hosted ones. No model is bundled and none is required: without
one, skills fall back to their rule-based strategy and every result says so.

## 1. Declare endpoints and models

Copy `services/api/src/agentic_sdlc/config/models.yaml`, edit it, and point `MODEL_REGISTRY_PATH`
at the copy.

```yaml
endpoints:
  - name: default                       # always exists; reads MODEL_ENDPOINT_URL / MODEL_ENDPOINT_API_KEY
    base_url_env: MODEL_ENDPOINT_URL
    api_key_env: MODEL_ENDPOINT_API_KEY
    privacy: self_hosted
  - name: azure-openai
    base_url_env: AZURE_OPENAI_BASE_URL # https://<resource>.openai.azure.com/openai/v1
    api_key_env: AZURE_OPENAI_API_KEY
    auth: api_key_header                # sends `api-key:` instead of `Authorization: Bearer`
    privacy: external

models:
  - model_id: qwen2.5-coder-32b
    provider: openai_compatible
    endpoint: default
    served_model_name: Qwen/Qwen2.5-Coder-32B-Instruct   # the id the server lists at /models
    revision: <commit hash you deployed>
    family: qwen2.5-coder
    license: Apache-2.0
    license_verified: true              # only after you have read the model card and licence
    tokenizer: Qwen/Qwen2.5-Coder-32B-Instruct
    context_window: 32768
    max_output_tokens: 8192
    tasks: [code_generation, test_generation, code_review, repair]
    capabilities: [code, structured_output, instruction_following]
    privacy: self_hosted
    priority: 60
```

Rules the registry enforces at start-up:

- Keys come only from environment variables (`api_key_env`). A credential in `headers` is rejected.
- A model on an endpoint declared `privacy: external` must itself declare `privacy: external`.
- A model that names an endpoint that does not exist is rejected.
- An endpoint whose URL is not set is skipped; its models show as "provider not configured".

## 2. Choose a privacy policy

`MODEL_PRIVACY_POLICY` decides whether project content (requirements, source code) may be sent to
hosted endpoints.

| Value | Effect |
|---|---|
| `self_hosted_only` (default) | Hosted models are never selected, whatever their score. |
| `any` | Hosted models may be selected. A project can still opt out with `limits.model_privacy: self_hosted_only`. |

A project can narrow the deployment policy but never widen it.

## 3. Which tasks need which capability

| Task (registry `tasks`) | Skills | What the model returns |
|---|---|---|
| `extraction` | brd-analysis, transcript-analysis | requirements quoting source passages (checked against the document) |
| `planning` | backlog-decomposition | backlog items (dependencies validated deterministically) |
| `architecture` | architecture-design | design options and a recommendation (a human approves) |
| `code_generation`, `repair` | code-generation | complete new contents of each changed file; the platform builds and reviews the diff |
| `test_generation` | acceptance-test-generation | QA acceptance tests written from the criteria before any code exists |

Code generation and test generation are what make the platform work on a customer repository.
Without a model for them, delivery stops at implementation with a clear reason, and criteria
without tests stay *unverifiable*, which blocks a release.

## 4. Verify a deployment

```bash
agentic-sdlc models list     # endpoints, availability, privacy
agentic-sdlc models check    # sends a synthetic prompt (no project data) to every non-mock model
```

`models check` reports, per model: whether the server lists it, whether its output matched the JSON
schema, which structured-output mode the server accepted (`json_schema`, or the `json_object`
fallback), whether token usage was reported, and latency. It exits non-zero if any model fails.
The **Capabilities → Models** page shows the same endpoints (never their keys).

## 5. Structured output across servers

The provider asks for `response_format: json_schema`. If a server rejects that with HTTP 400, it
retries once in `json_object` mode with the schema in the system prompt and keeps using that mode
for the endpoint. Output wrapped in a single Markdown code fence is accepted. Either way, every
result is validated against the skill's schema and postconditions, and invalid output is retried
within the skill's bounded retry budget.

## 6. Licensing

`license_verified` stays `false` until someone has read the model card and licence for the exact
revision you deploy. Skills can require a verified licence; the router then skips unverified models
and says why. Record the revision you deployed (`revision:`), not `main`.
