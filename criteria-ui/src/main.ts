import "./style.css";
import { type AgentEvent, encode, invoke, newSession } from "./api";
import { $, block, element, listing, pause, plural, pretty } from "./dom";
import { RESET, SUGGESTIONS, TIMING, TOOL_STATE, TOOLS } from "./labels";
import { Journey } from "./journey";
import { forget, open, openBytes } from "./pdf";
import { Reset } from "./reset";
import { tour } from "./tour";
import { Stage } from "./stage";
import { PROGRESS, STAGES, stageBody, toolBody } from "./stages";
import { type Done, type Host, Turn, suggestions } from "./turn";
import { Viewer } from "./viewer";

type Upload = { id: string; name: string };
type Handler = (event: AgentEvent, turn: Turn) => void | Promise<void>;
type On<K extends AgentEvent["type"]> = (event: Extract<AgentEvent, { type: K }>, turn: Turn) => void | Promise<void>;
type Ingest = Extract<AgentEvent, { type: "ingest" }>;

const stage = new Stage($("#scene"));
const status = $("#status");
const announcer = $("#announce");
const thread = $<HTMLOListElement>("#thread");
const form = $<HTMLFormElement>("#ask");
const prompt = $<HTMLTextAreaElement>("#prompt");
const picker = $<HTMLSelectElement>("#doc-name");
const files = $<HTMLInputElement>("#file");
const reset = $<HTMLButtonElement>("#reset");
const inside = $<HTMLButtonElement>("#inside");
const loading = $("#loading");
const loadingLabel = $(".loading-label");
const uploader = $(".upload");
const uploadLabel = $(".upload-label");
const viewer = new Viewer($("#viewer"), () => stage.focus(null));
const journeyDialog = $<HTMLDialogElement>("#journey");
const journey = new Journey(journeyDialog);

let session = newSession();
let documentId: string | null = null;
let documents: Upload[] = [];
let running = false;
let choosing = 0;

const documentName = () => documents.find((d) => d.id === documentId)?.name ?? "guide";
const documentUrl = () => `/documents/${documentId}`;

const say = (text: string) => {
  status.textContent = text;
  document.querySelectorAll(".pending-label").forEach((label) => (label.textContent = text));
};
const announce = (text: string) => (announcer.textContent = text);
const load = (text: string | null) => {
  loading.hidden = text === null;
  loadingLabel.textContent = text ?? "";
};
const paint = (count: number, total: number) => load(count < total ? `Rendering page ${count + 1} of ${total}` : null);

function busy(on: boolean) {
  running = on;
  form.toggleAttribute("data-busy", on);
  uploader.toggleAttribute("data-locked", on);
  uploader.title = on ? "Wait for the current request to finish" : "";
  files.disabled = on;
  reset.disabled = on;
  picker.disabled = on || documents.length < 2;
}

const host: Host = {
  thread,
  busy: () => running,
  ask,
  run,
  rate: async (trace, helpful) => {
    busy(true);
    try {
      for await (const _ of invoke({ feedback: { trace_id: trace, helpful } }, session));
      return true;
    } catch {
      return false;
    } finally {
      busy(false);
    }
  },
  cite: (page) => stage.cite(page),
  open: (citation, number) => {
    stage.focus(citation.page);
    pause(TIMING.focus).then(() => viewer.show(citation, number, documentName()));
  },
  resolve: (pages) => stage.resolve(pages),
  say,
  announce,
  journey: (run, finished) => (!finished || document.documentElement.hasAttribute("data-inside")) && journey.show(run),
};

function opening(locked: boolean) {
  const empty = element("li", "empty");
  empty.append(element("p", "empty-lead", "Every sentence comes back with the page it rests on."));
  if (!locked) empty.append(...suggestions(SUGGESTIONS, host));
  return empty;
}

function showInside(on: boolean) {
  document.documentElement.toggleAttribute("data-inside", on);
  inside.setAttribute("aria-pressed", String(on));
  if (on) thread.querySelectorAll<HTMLDetailsElement>(".thinking").forEach((details) => (details.open = true));
  try {
    localStorage.setItem("inside", on ? "on" : "");
  } catch {}
}

function showTour() {
  const shown = thread.querySelector<HTMLElement>(".tour");
  if (!document.documentElement.hasAttribute("data-inside") || shown?.dataset.document === (documentId ?? "")) return;
  shown?.remove();
  const asked = (question: string) => !running && (ask(question), true);
  thread.append(tour(documentId, documentName(), asked, () => journey.show(null)));
  thread.lastElementChild?.scrollIntoView({ block: "start" });
}

function ready(on: boolean) {
  form.toggleAttribute("data-locked", !on);
  prompt.disabled = !on;
  prompt.placeholder = on ? "LTV, terms, borrowers, property types" : "Waiting for a document";
  thread.querySelector(".empty")?.remove();
  if (!thread.querySelector(".turn")) thread.append(opening(!on));
}

const ON: { [K in AgentEvent["type"]]?: On<K> } = {
  stage: (event, turn) => {
    const label =
      event.status === "failed" ? `${event.stage} failed (${event.reason})` : (STAGES[event.stage]?.label(event.count ?? 0) ?? event.stage);
    turn.step(event.stage, label, "", event.status ?? "running", stageBody(event.stage, event.details));
    if (event.status !== "done") say(label);
  },
  tool: (event, turn) => {
    const label = TOOLS[event.name] ?? event.name;
    const kept = event.status === "success" && event.name === "search_guide" && Array.isArray(event.pages);
    const detail = event.progress ? (PROGRESS[event.progress]?.(event) ?? "") : kept ? (PROGRESS.kept?.(event) ?? "") : "";
    const state = TOOL_STATE[event.status] ?? "running";
    turn.step(event.id, label, detail, state, toolBody(event), event.name);
    if (event.status !== "success") say(detail || label);
    if (Array.isArray(event.pages)) stage.lift(event.pages);
    else if (event.page) stage.lift([event.page]);
  },
  gate: (event, turn) => {
    const reason = element("div", "");
    if (event.reason) reason.append(block("Reason", event.reason));
    if (event.input !== undefined) reason.append(block("Action", pretty(event.input)));
    const outcome = `Decision ${event.outcome.replace("_", " ")}`;
    turn.step(`gate-${turn.steps.size}`, outcome, "", event.outcome === "approved" ? "done" : "failed", reason, `gate:${event.outcome}`);
  },
  claim: (event, turn) => turn.claim(event.number, event.text, event.citation),
  note: (event, turn) => turn.note(event.text),
  suggestions: (event, turn) => void (turn.suggested = event.questions),
  done: (event, turn) => turn.finish(event),
};

async function run(turn: Turn, body: object): Promise<Done | null> {
  busy(true);
  stage.working(true);
  turn.wait("Sending to the agent");
  let done: Done | null = null;
  try {
    for await (const event of invoke(body, session)) {
      if (event.type === "done") done = event;
      await (ON[event.type] as Handler | undefined)?.(event, turn);
    }
    if (!done) turn.note("The answer stopped before it finished. Ask again.");
  } catch (error) {
    turn.note(`The request stopped: ${(error as Error).message}`);
    say("The request stopped");
  } finally {
    turn.stop("Stopped", "failed");
    busy(false);
    stage.working(false);
  }
  return done;
}

function ask(question: string) {
  if (running || !documentId || !question.trim()) return;
  stage.settle();
  const turn = new Turn(question.trim(), host);
  prompt.value = "";
  run(turn, { prompt: question.trim(), document_id: documentId });
}

const INGEST: Record<Ingest["stage"], (event: Ingest, turn: Turn) => void> = {
  convert: (_, turn) => turn.step("convert", "Converting it to PDF"),
  classify: (event, turn) => {
    const label = "Checking it is about banking or lending";
    if (event.status !== "done") return turn.step("classify", label);
    const share = Math.round((event.confidence ?? 0) * 100);
    const body = document.createDocumentFragment();
    body.append(
      listing([[event.banking ? "banking" : "not banking", `Jev puts the chance it is about banking or lending at ${share}%.`, ""]]),
      block("Opening words it read", event.excerpt ?? ""),
    );
    turn.step("classify", label, event.banking ? "yes" : "no", event.banking ? "done" : "failed", body);
  },
  parse: (event, turn) => {
    const label = "Reading with Mistral OCR";
    const pages = plural(event.pages ?? 0, "page");
    if (event.status === "failed") return turn.step("parse", label, "unavailable", "failed", block("Why", event.reason ?? ""));
    if (event.status === "done") return turn.step("parse", label, `${pages} read`, "done");
    turn.step("parse", label, pages);
    say(`Mistral OCR is reading ${pages}`);
  },
  read: (event, turn) => {
    if (event.page) stage.reveal(event.page);
    const label = `Reading page ${event.page} of ${event.pages}`;
    turn.step("read", "Reading the text layer", label);
    say(label);
  },
  structure: (event, turn) => {
    const label = "Reading page titles and tables";
    if (event.status !== "done") {
      turn.step("structure", label, plural(event.pages ?? 0, "page"));
      return say("Reading page titles and stitching tables across pages");
    }
    const found = `${plural(event.pages ?? 0, "page card")}, ${plural(event.tables ?? 0, "table")}, ${plural(event.rows ?? 0, "row")}`;
    turn.step("structure", label, found, "done");
  },
  table: (event, turn) => {
    const dropped = event.dropped ? `, ${event.dropped} not found printed and dropped` : "";
    turn.step(`table-${event.number}`, `Table ${event.number}: ${event.title}`, `${plural(event.rows ?? 0, "row")}${dropped}`, "done", null, "table");
  },
  stored: (event, turn) => {
    const parse = event.parsed ? ", read by Mistral OCR" : "";
    turn.step("stored", "Stored", `${event.pages} pages${parse}, ready to quote`, "done");
  },
};

async function uploaded(event: Done, turn: Turn, previewed: boolean) {
  if (event.status !== "uploaded" || !event.document_id) {
    turn.stop(event.status === "rejected" ? "Not a banking document" : "Could not use this file", "failed");
    turn.note(event.reason ?? `The upload failed (${event.error}). Try again.`);
    say("That document was not used");
    if (previewed && documentId) stage.show(await open(documentUrl()), false, paint);
    else if (previewed) stage.clear();
    return;
  }
  listed([{ id: event.document_id, name: event.name ?? "" }, ...documents.filter((d) => d.id !== event.document_id)]);
  documentId = event.document_id;
  picker.value = documentId;
  session = newSession();
  ready(true);
  turn.step("read", "Reading the text layer", `${event.pages} pages read`, "done");
  turn.stop(`Read ${plural(event.pages ?? 0, "page")}`, "done");
  showTour();
  say(`Ask anything about ${event.name}`);
  if (!previewed) stage.show(await open(documentUrl()), false, paint);
}

async function upload(file: File, place = "") {
  if (running) return;
  busy(true);
  const turn = new Turn(`Upload ${file.name}`, host);
  uploader.toggleAttribute("data-busy", true);
  uploadLabel.textContent = "Uploading";
  say(`Opening ${file.name}${place}`);
  turn.wait(`Sending ${file.name} to the agent`);
  try {
    const bytes = await file.arrayBuffer();
    const preview = openBytes(bytes)
      .then((pdf) => (stage.show(pdf, true, paint, true), true))
      .catch(() => (load(null), false));
    stage.working(true);
    let done: Done | null = null;
    for await (const event of invoke({ upload: { name: file.name, content: await encode(file) } }, newSession())) {
      if (event.type === "ingest") INGEST[event.stage]?.(event, turn);
      if (event.type === "done") done = event;
    }
    if (done) await uploaded(done, turn, await preview);
    else turn.note("The upload stopped before it finished. Try again.");
  } catch (error) {
    turn.note(`The upload could not be sent: ${(error as Error).message}`);
    say("The agent could not be reached");
  } finally {
    turn.stop("Stopped", "failed");
    busy(false);
    stage.working(false);
    uploader.toggleAttribute("data-busy", false);
    uploadLabel.textContent = "Upload a document";
  }
}

function listed(uploads: Upload[]) {
  documents = uploads;
  picker.replaceChildren(...uploads.map((d) => new Option(d.name, d.id)));
  picker.hidden = !uploads.length;
  picker.disabled = running || uploads.length < 2;
}

function switchTo(id: string | null) {
  viewer.close();
  documentId = id;
  session = newSession();
  thread.replaceChildren();
  ready(id !== null);
  showTour();
}

async function choose(id: string) {
  if (running || id === documentId) return;
  switchTo(id);
  say(`Ask anything about ${documentName()}`);
  load("Loading the guide");
  const token = ++choosing;
  const pdf = await open(documentUrl()).catch(() => null);
  if (token !== choosing) return;
  if (!pdf) {
    load(null);
    return say("This document could not be loaded");
  }
  stage.show(pdf, false, paint);
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  ask(prompt.value);
});
prompt.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;
  event.preventDefault();
  ask(prompt.value);
});
files.addEventListener("change", async () => {
  const chosen = [...(files.files ?? [])];
  files.value = "";
  for (const [index, file] of chosen.entries()) await upload(file, chosen.length > 1 ? ` (${index + 1} of ${chosen.length})` : "");
});
picker.addEventListener("change", () => choose(picker.value));
inside.addEventListener("click", () => {
  const on = inside.getAttribute("aria-pressed") !== "true";
  showInside(on);
  if (!on) thread.querySelector(".tour")?.remove();
  else showTour();
});
const resetting = new Reset(
  $<HTMLDialogElement>("#resetting"),
  () => {
    if (journeyDialog.open) journeyDialog.close();
    switchTo(null);
    listed([]);
    stage.clear();
    forget();
    load(null);
    say(RESET.after);
  },
  busy,
);
reset.addEventListener("click", () => running || resetting.open());

stage.pick = (page) => {
  if (running || !documentId) return;
  stage.focus(page);
  pause(TIMING.focus).then(() => viewer.page(documentUrl(), page, documentName()));
};

ready(false);
try {
  showInside(localStorage.getItem("inside") === "on");
} catch {
  showInside(false);
}
fetch("/documents")
  .then((response) => (response.ok ? response.json() : []))
  .then(async (uploads: Upload[]) => {
    listed(uploads);
    if (uploads[0]) await choose(uploads[0].id);
  })
  .catch(() => {
    load(null);
    say("The agent could not be reached");
  });
