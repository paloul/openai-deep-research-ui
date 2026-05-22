# Open Deep Research UI

A lightweight Web UI for OpenAI Deep Research models:

- `o3-deep-research`
- `o4-mini-deep-research`

The app calls OpenAI through the Responses API with `background=true` and `web_search_preview`. It does not run a local agent framework, local browser, or third-party search pipeline.

The UI exposes per-model reasoning effort options from its model metadata. Current Deep Research API support is `medium` for the supported models.

## Run

```bash
python web_app.py
```

The UI starts at `http://127.0.0.1:5080`.

## Authentication

The app is protected by a simple username/password login by default. Configure it
with environment variables:

```bash
export ODR_AUTH_USERNAME="admin"
export ODR_AUTH_PASSWORD="replace-with-a-strong-password"
export ODR_SESSION_SECRET="replace-with-a-long-random-secret"
```

Set `ODR_AUTH_ENABLED=false` to disable the login gate for local-only development.

## Configure

Set your OpenAI API key in either place:

```bash
export OPENAI_API_KEY="sk-..."
```

or in `odr-config.json`:

```json
{
  "model": {
    "providers": [
      {"provider": "openai", "api_key": "sk-...", "base_url": ""}
    ],
    "default_model_id": "o3-deep-research",
    "max_completion_tokens": 32768,
    "reasoning_effort": "medium"
  }
}
```

`odr-config.example.json` contains the complete sample configuration.

## Sessions

Research jobs are persisted in SQLite so the sidebar can replay prior runs. Background mode keeps polling the OpenAI response server-side and lets the browser reconnect later. Stop cancels the OpenAI response through the Responses API.
