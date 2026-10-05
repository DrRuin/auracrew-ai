# Criteria agent

The agent reads lending documents and answers questions about them. Each sentence in an answer quotes a page. The agent checks each quote before it sends the answer.

## Before you start

You need:

- Python 3.13 and uv.
- Docker, for Postgres and Gotenberg.
- An OpenAI key and a TypeSafe key. TypeSafe runs Jev, the model that screens, searches and checks.
- A Mistral key, if you want the agent to read scanned pages. Without it, the agent reads only pages with a text layer.

## Set the keys

1. Copy `.env.example` to `.env` in this folder.
2. Put your keys in `.env`.

## Run the agent

1. From the repository root, start Postgres and Gotenberg:

   ```sh
   docker compose --env-file criteria-agent/.env up -d postgres gotenberg
   ```

2. Go to this folder and install the dependencies:

   ```sh
   cd criteria-agent
   uv sync
   ```

3. Start the agent:

   ```sh
   uv run uvicorn app.main:app --port 8080
   ```

The agent listens on http://127.0.0.1:8080. To use it in a browser, start the web app. See `../criteria-ui/README.md`.

## Run the tests

The tests use Postgres. They do not call OpenAI, Jev or Mistral.

1. Start Postgres and Gotenberg (step 1 above).
2. Run the tests:

   ```sh
   uv run pytest
   ```

The tests write only to the schemas `test` and `test_run`. They do not change your documents.

## Change a setting

`app/core/config.py` holds every setting and its default value. To change a setting, put it in `.env` in upper case:

```sh
MAX_TURNS=12
```

| Setting | Default | What it does |
|---|---|---|
| `MODEL_ID` | `gpt-6-luna` | The OpenAI model that answers. |
| `TYPESAFE_MODEL` | `jev-1.13.0` | The Jev version. A new version starts with no trust. |
| `MAX_TURNS` | `10` | The maximum number of model calls for one question. |
| `PARALLEL_CALLS` | `16` | The maximum number of model calls at the same time. |
| `DATABASE_URL` | local Postgres | The Postgres database. |
| `CONVERTER_URL` | `http://127.0.0.1:3100` | Gotenberg, which converts Word and Excel files to PDF. |
| `RESET_ENABLED` | `true` | Lets the web app delete all data. |
| `JOURNEY_CREDENTIALS` | `true` | Shows the Langfuse sign-in in the web app. |

CAUTION: When `RESET_ENABLED` is `true`, any user of the web app can delete all documents, answers and traces. On a public server, set it to `false`.

## What happens to a question

1. Jev checks that the question is about the document (`answer/agent.py`, `ai/jev.py`).
2. The model calls tools to search, read pages, query tables or assess a case (`answer/tools.py`).
3. Code finds each quote in the document. Then Jev or GPT checks that the quote supports the claim (`answer/verification.py`).
4. A decision goes to the gate. The Cedar policy must allow it, then a person must approve it (`gate/`).

## Folders

```
app/
  main.py      HTTP entrypoint (FastAPI)
  journey.py   the run timeline and code map for the web app
  core/        settings, database, documents, prompts, schemas, errors, tracing
  ai/          OpenAI and Jev clients
  ingest/      upload, OCR, page cards and tables
  answer/      agent, tools, claim checks, feedback
  gate/        decision policy (policy.cedar) and human approval
tests/         tests that guard behaviour; sample.pdf is a fictional guide
```
