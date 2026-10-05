import { button, element, plural } from "./dom";
import { INSIDE, JOURNEY } from "./labels";
import type { Run } from "./turn";

type Item = { name: string; kind: string; doc: string; line: number };
type Module = { name: string; folder: string; doc: string; lines: number; uses: string[]; items: Item[]; source?: string };
type Access = Record<keyof typeof JOURNEY.fields, string | null>;
type Data = { modules: Module[]; project: string; langfuse: Access | null };

const seconds = (ms: number) => `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)} s`;
const traced = (run: Run | null, data: Data) => (run?.trace ? `/langfuse/project/${data.project}/traces/${run.trace}` : null);

function inside(kind: string) {
  const [code, why] = INSIDE[kind] ?? [];
  const node = element("p", "step-inside");
  if (code && why) node.append(element("code", "step-code", code), element("span", "step-why", why));
  return node;
}

function ran(run: Run | null) {
  const names = (run?.steps ?? []).flatMap((step) => (INSIDE[step.kind]?.[0] ?? "").split(" → ").filter(Boolean));
  return { names: new Set(names), modules: [...new Set(names.map((name) => name.split(".")[0] ?? ""))] };
}

function link(text: string, href: string) {
  const node = element("a", "journey-action", text);
  node.href = href;
  node.target = "_blank";
  node.rel = "noopener";
  return node;
}

function flow() {
  const page = document.createDocumentFragment();
  page.append(element("p", "journey-note", JOURNEY.start));
  for (const { title, steps } of JOURNEY.flows) {
    const list = element("ol", "journey-steps");
    for (const [kind, label] of steps) {
      const row = element("li", "journey-step journey-flow");
      const what = element("div", "journey-what");
      what.append(element("p", "journey-label", label ?? ""), inside(kind ?? ""));
      row.append(what);
      list.append(row);
    }
    page.append(element("h3", "journey-flow-title", title), list);
  }
  return page;
}

function timeline(run: Run, data: Data) {
  const total = Math.max(run.ended - run.started, 1);
  const page = document.createDocumentFragment();
  const facts = element("p", "journey-facts");
  const claims = run.verdict ? [element("span", `chip chip-${run.status}`, run.verdict)] : [];
  facts.append(...claims, element("span", "chip", seconds(total)), element("span", "chip", plural(run.steps.length, "step")));
  const steps = element("ol", "journey-steps");
  for (const step of run.steps) {
    const row = element("li", "journey-step");
    row.dataset.state = step.state;
    const what = element("div", "journey-what");
    const title = element("p", "journey-label", step.label);
    if (step.detail) title.append(element("span", "journey-detail", step.detail));
    what.append(title, inside(step.kind));
    const track = element("div", "journey-track");
    const bar = element("span", "journey-bar");
    bar.style.left = `${((step.start - run.started) / total) * 100}%`;
    bar.style.width = `${((step.end - step.start) / total) * 100}%`;
    track.append(bar);
    row.append(what, track, element("span", "journey-time", seconds(step.end - step.start)));
    steps.append(row);
  }
  const trace = traced(run, data);
  page.append(element("p", "journey-question", `“${run.question}”`), facts, steps, element("p", "journey-note", JOURNEY.bars));
  if (trace) page.append(link(JOURNEY.trace, trace));
  return page;
}

function code(run: Run | null, data: Data) {
  const used = ran(run);
  const order = (m: Module) => (used.modules.includes(m.name) ? used.modules.indexOf(m.name) : used.modules.length);
  const modules = [...data.modules].sort((a, b) => order(a) - order(b));
  const names = data.modules.reduce((sum, m) => sum + m.items.length, 0);
  const lines = data.modules.reduce((sum, m) => sum + m.lines, 0);
  const page = document.createDocumentFragment();
  page.append(element("p", "journey-note", JOURNEY.code(data.modules.length, names, lines, used.names.size)));
  const list = element("div", "code-modules");
  for (const m of modules) {
    const box = element("details", "code-module");
    box.open = used.modules.includes(m.name);
    const summary = element("summary", "code-summary");
    summary.append(
      element("span", "code-name", `${m.folder === "." ? "" : `${m.folder}/`}${m.name}.${m.source ? "cedar" : "py"}`),
      element("span", "code-doc", m.doc),
      element("span", "code-lines", `${m.lines} lines`),
    );
    if (box.open) summary.append(element("span", "chip chip-ran", "ran"));
    const body = element("div", "code-body");
    if (m.uses.length) body.append(element("p", "code-uses", `uses ${m.uses.join(", ")}`));
    const items = element("ul", "code-items");
    for (const item of m.items) {
      const row = element("li", used.names.has(`${m.name}.${item.name}`) ? "ran" : "");
      row.append(element("code", "code-fn", item.name), element("span", "code-what", item.doc));
      items.append(row);
    }
    body.append(m.source ? element("pre", "detail-pre", m.source) : items);
    box.append(summary, body);
    list.append(box);
  }
  page.append(list);
  return page;
}

function secret(value: string) {
  const shown = element("span", "key-value", "••••••••••••");
  const toggle = button("Show", "journey-mini", () => {
    const hidden = toggle.textContent === "Show";
    shown.textContent = hidden ? value : "••••••••••••";
    toggle.textContent = hidden ? "Hide" : "Show";
  });
  return [shown, toggle];
}

function langfuse(run: Run | null, data: Data) {
  const page = document.createDocumentFragment();
  page.append(element("p", "journey-note", JOURNEY.langfuse));
  if (!data.langfuse) {
    page.append(element("p", "journey-note", JOURNEY.hidden));
    return page;
  }
  const keys = element("dl", "keys");
  for (const [field, label] of Object.entries(JOURNEY.fields) as [keyof Access, string][]) {
    const value = data.langfuse[field] ?? "not set";
    const row = element("div", "key-row");
    const shown = field === "url" ? [link(`${location.origin}${value}`, value)] : JOURNEY.secret.includes(field) ? secret(value) : [element("span", "key-value", value)];
    const copy = button("Copy", "journey-mini", () => navigator.clipboard?.writeText(field === "url" ? `${location.origin}${value}` : value).then(() => (copy.textContent = "Copied")));
    const dd = element("dd", "key-cell");
    dd.append(...shown);
    row.append(element("dt", "", label), dd, copy);
    keys.append(row);
  }
  page.append(keys);
  const trace = traced(run, data);
  if (trace) page.append(link(JOURNEY.trace, trace));
  return page;
}

const PAGES = [(run: Run | null, data: Data) => (run ? timeline(run, data) : flow()), code, langfuse];

export class Journey {
  private data: Promise<Data | null> | null = null;
  private run: Run | null = null;
  private page = 0;

  constructor(private root: HTMLDialogElement) {
    root.addEventListener("click", (event) => event.target === root && root.close());
  }

  async show(run: Run | null) {
    this.run = run;
    this.page = 0;
    this.data ??= fetch("/journey")
      .then((response) => (response.ok ? (response.json() as Promise<Data>) : null))
      .catch(() => null);
    const data = await this.data;
    if (!data) this.data = null;
    this.render(data);
    if (!this.root.open) this.root.showModal();
  }

  private go(page: number) {
    this.page = page;
    this.data?.then((data) => this.render(data));
  }

  private render(data: Data | null) {
    const run = this.run;
    const pages = JOURNEY.pages.map((page, index) => (index || run ? page : JOURNEY.overview));
    const head = element("header", "journey-head");
    const tabs = element("nav", "journey-tabs");
    pages.forEach(({ tab }, index) => {
      const node = button(`${index + 1}  ${tab}`, "pill", () => this.go(index));
      node.setAttribute("aria-current", String(index === this.page));
      tabs.append(node);
    });
    const title = pages[this.page]?.title ?? "";
    head.append(
      element("p", "journey-kicker", JOURNEY.kicker),
      element("h2", "journey-title", title),
      tabs,
      button("Close", "pill journey-close", () => this.root.close()),
    );
    const body = element("div", "journey-body");
    body.append(data ? (PAGES[this.page]?.(run, data) ?? "") : element("p", "journey-note", "The code map could not be loaded."));
    const foot = element("footer", "journey-foot");
    const back = button("Back", "journey-nav", () => this.go(this.page - 1));
    back.disabled = this.page === 0;
    const next = pages[this.page + 1];
    const forward = next ? button(`Next: ${next.tab}`, "journey-nav journey-next", () => this.go(this.page + 1)) : button("Done", "journey-nav journey-next", () => this.root.close());
    foot.append(back, forward);
    this.root.replaceChildren(head, body, foot);
    this.root.setAttribute("aria-label", title);
    forward.focus();
  }
}
