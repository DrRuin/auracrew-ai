import type { AgentEvent } from "./api";
import { block, element, listing, parsed, plural, pretty, type Row } from "./dom";
import { FEEDBACK, ROUTES } from "./labels";

type Check = { claim: string; verdict: string; judge: string; page: number | null };
type Stage = { label: (count: number) => string; body?: (details: never) => Node };

const where = (page: number | null) => (page ? `page ${page}, ` : "");
const verdict = (check: { verdict: string }) => check.verdict.replace("_", " ");
const checked = (checks: Check[]) => listing(checks.map((c): Row => [verdict(c), c.claim, `${where(c.page)}checked by ${c.judge}`]));

export const STAGES: Record<string, Stage> = {
  screen: {
    label: () => "Checking the request is about the guide",
    body: (view: { off_topic: boolean; pressure: boolean; route: string }) =>
      listing([
        [view.off_topic ? "off topic" : "on topic", view.off_topic ? "The request is not about the guide." : "The request is about the guide.", ""],
        [
          view.pressure ? "pressure" : "no pressure",
          view.pressure ? "It pushes against the controls, so any approval card is flagged." : "It does not push against the controls.",
          "",
        ],
        ["route", `It asks about ${ROUTES[view.route] ?? view.route}.`, ""],
      ]),
  },
  budget: { label: () => "Step budget spent: answering from what was gathered" },
  feedback: {
    label: () => "Reading your feedback",
    body: ({ verdict }: { verdict: string }) => listing([[verdict, FEEDBACK[verdict] ?? verdict, ""]]),
  },
  verify: { label: (count) => `Checking ${plural(count, "claim")} against the guide`, body: checked },
  recheck: { label: (count) => `Checking the revised answer, ${plural(count, "claim")}`, body: checked },
  completeness: {
    label: (count) => `Checking the answer covers ${plural(count, "key fact")}`,
    body: ({ facts, missing }: { facts: string[]; missing: string[] }) =>
      listing(facts.map((fact): Row => [missing.includes(fact) ? "missing" : "covered", fact, ""])),
  },
  repair: {
    label: (count) => `Adding ${plural(count, "missing fact")}`,
    body: ({ missing }: { missing: string[] }) => listing(missing.map((fact): Row => ["adding", fact, ""])),
  },
  repaired: {
    label: (count) => `Kept ${plural(count, "added fact")}`,
    body: ({ added, dropped }: { added: string[]; dropped: Check[] }) =>
      listing([
        ...added.map((fact): Row => ["added", fact, ""]),
        ...dropped.map((c): Row => [verdict(c), c.claim, `${where(c.page)}left out of the answer`]),
      ]),
  },
  reassess: {
    label: () => "Assessing the case again: a check contradicted it",
    body: ({ contradicted }: { contradicted: string[] }) => listing(contradicted.map((text): Row => ["contradicts", text, ""])),
  },
};

type Progress = Extract<AgentEvent, { type: "tool" }>;

export const PROGRESS: Record<string, (event: Progress) => string> = {
  ranking: (event) => `Ranking ${plural(event.count ?? 0, "page")}`,
  kept: (event) => `Kept ${plural(Array.isArray(event.pages) ? event.pages.length : 0, "relevant page")}`,
  designed: (event) => `Looking for ${event.fields?.join(", ") ?? "the records"}`,
  read: (event) => `Read ${event.done ?? 0} of ${plural(Number(event.pages), "page")}, ${plural(event.records ?? 0, "record")} kept`,
};

export function stageBody(stage: string, details: unknown): Node | null {
  const body = STAGES[stage]?.body as ((details: unknown) => Node) | undefined;
  return details === undefined || !body ? null : body(details);
}

function output(text: string): Node | string {
  const rows = parsed(text);
  if (!Array.isArray(rows) || !rows.every((row) => typeof row?.page === "number")) return pretty(rows);
  const list = element("ol", "detail-pages");
  for (const row of rows) {
    const page = element("details", "detail-page");
    const relevance = typeof row.relevance === "number" ? `, relevance ${Math.round(row.relevance * 100)}%` : "";
    page.append(element("summary", "detail-page-summary", `Page ${row.page}${relevance}`), element("pre", "detail-pre", row.text ?? ""));
    const item = element("li", "");
    item.append(page);
    list.append(item);
  }
  return list;
}

export function toolBody(event: Progress) {
  if (event.input === undefined && !event.output) return null;
  const body = document.createDocumentFragment();
  if (event.input !== undefined) body.append(block("Input", pretty(event.input)));
  if (event.output) body.append(block("Output", output(event.output)));
  return body;
}
