# Criteria web app

The web app is the page where you upload a document, ask questions and approve decisions. It uses TypeScript, Vite, three.js and pdf.js. It talks only to the agent.

## Before you start

You need:

- Node 24 and npm.
- The agent on http://127.0.0.1:8080. See `../criteria-agent/README.md`.

## Run the web app

1. Go to this folder and install the dependencies:

   ```sh
   cd criteria-ui
   npm ci
   ```

2. Start the development server:

   ```sh
   npm run dev
   ```

3. Open the address that Vite shows, usually http://localhost:5173.

The development server sends `/invocations`, `/documents`, `/journey` and `/api/reset` to the agent. If the agent uses a different address, set `AGENT_URL`:

```sh
AGENT_URL=http://127.0.0.1:9000 npm run dev
```

The **Langfuse traces** link works only in Docker, at http://localhost:8000.

## Build the web app

```sh
npm run build
```

The build checks the types, then writes the files to `dist/`. In Docker, nginx serves `dist/` (see `../ui.Dockerfile`).

## Files

```
src/
  main.ts      starts the page and connects the events to the views
  api.ts       sends requests to the agent and reads the event stream
  turn.ts      one question and its answer, approval card and rating
  stages.ts    the label and the detail of each step
  labels.ts    all the text that the page shows
  tour.ts      the six questions that show each safeguard
  journey.ts   the popup: run timeline, code map, Langfuse sign-in
  reset.ts     the reset dialog and its progress bar
  viewer.ts    opens the page of a citation and marks the quote
  pdf.ts       loads and draws pages with pdf.js
  stage.ts     the three.js background
  dom.ts       small helpers for HTML elements
  style.css    the styles
```
