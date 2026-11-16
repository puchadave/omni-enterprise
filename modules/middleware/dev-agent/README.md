# Autonomous Dev Agent (Omni-Core Middleware)

Scoped coding agent to **maintain and further develop this middleware only**. It runs against an OpenAI-compatible API (e.g. `OMNIROUTE_URL` + `OMNIROUTE_API_KEY`) and operates on the mounted workspace.

## Usage (via start.sh / compose profile)

```bash
# set OMNIROUTE_URL/OMNIROUTE_API_KEY (or AGENT_BASE_URL/AGENT_API_KEY) in .env
./start.sh agent "Describe and implement <development task>"
```

Direct:
```bash
docker compose --profile dev run --rm coding-agent "<task>"
```

## Environment
- `AGENT_BASE_URL` / `OMNIROUTE_URL` – OpenAI-compatible endpoint
- `AGENT_API_KEY` / `OMNIROUTE_API_KEY` – API key
- `AGENT_MODEL` – model id (default: `OMNI_MODEL_ADMIN` or `gpt-4o-mini`)
- `AGENT_MAX_STEPS` – max tool/think steps (default 30)

## Safety / Scope
Restricted to `/workspace` (this project). Not started by default (compose profile `dev`). Do not commit secrets; review changes before deploy.
