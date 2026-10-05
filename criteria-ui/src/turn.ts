import type { AgentEvent, Approval, Citation } from "./api";
import { button, element, plural, spinning, still, type } from "./dom";
import { APPROVALS, INSIDE, JOURNEY, RATED, STATUS, TIMING } from "./labels";

export type Done = Extract<AgentEvent, { type: "done" }>;
export type Moment = { kind: string; label: string; detail: string; state: string; start: number; end: number };
export type Run = { question: string; status: string; verdict: string; trace?: string; started: number; ended: number; steps: Moment[] };

export interface Host {
  thread: HTMLElement;
  busy(): boolean;
  ask(question: string): void;
  run(turn: Turn, body: object): Promise<Done | null>;
  rate(trace: string, helpful: boolean): Promise<boolean>;
  cite(page: number): void;
  open(citation: Citation, number: number): void;
  resolve(pages: number[]): void;
  say(text: string): void;
  announce(text: string): void;
  journey(run: Run, finished: boolean): void;
}

const THUMB =
  '<svg viewBox="0 0 24 24" width="17" height="17" aria-hidden="true"><path d="M7 11v9H4v-9h3Zm2 9h8.2a2 2 0 0 0 2-1.6l1.2-6A2 2 0 0 0 18.4 10H14l.8-4.1A1.6 1.6 0 0 0 13.2 4L9 10v10Z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg>';

export class Turn {
  root = element("li", "turn");
  suggested: string[] = [];
  steps = new Map<string, HTMLLIElement>();
  private answer = element("div", "answer");
  private thinking = element("details", "thinking");
  private trail = element("ul", "trail");
  private label = element("span", "thinking-label", "Thinking");
  private clock = element("span", "thinking-time");
  private cited = new Set<number>();
  private typing = Promise.resolve();
  private verdict: HTMLElement | null = null;
  private started = 0;
  private timer = 0;
  private moments = new Map<string, Moment>();
  private begun = performance.now();
  private paused = 0;

  constructor(
    private question: string,
    private host: Host,
  ) {
    this.root.append(element("p", "question", question));
    this.open();
    host.thread.querySelector(".empty")?.remove();
    host.thread.append(this.root);
  }

  open() {
    this.thinking = element("details", "thinking");
    this.thinking.open = document.documentElement.hasAttribute("data-inside");
    const summary = element("summary", "thinking-summary");
    const icon = element("span", "thinking-icon");
    icon.setAttribute("aria-hidden", "true");
    this.label = element("span", "thinking-label", "Thinking");
    this.clock = element("span", "thinking-time");
    summary.append(icon, this.label, this.clock);
    this.trail = element("ul", "trail");
    this.thinking.append(summary, this.trail);
    this.answer = element("div", "answer");
    this.steps.clear();
    this.suggested = [];
    this.started = 0;
    this.root.append(this.thinking, this.answer);
  }

  resume() {
    const label = JOURNEY.gaps.person;
    if (this.paused) this.moments.set(`person-${this.paused}`, { kind: "person", label, detail: "", state: "done", start: this.paused, end: performance.now() });
    this.paused = 0;
    this.verdict?.remove();
    this.open();
  }

  wait(label: string) {
    this.thinking.dataset.state = "running";
    this.label.textContent = label;
    this.started ||= performance.now();
    this.timer ||= window.setInterval(() => this.tick(), TIMING.clock);
    this.scroll();
  }

  stop(summary: string, state: string) {
    clearInterval(this.timer);
    this.timer = 0;
    if (this.thinking.dataset.state !== "running") return;
    this.tick();
    this.thinking.dataset.state = state;
    this.label.textContent = summary;
  }

  private tick() {
    this.clock.textContent = `${Math.max(1, Math.round((performance.now() - this.started) / 1000))} s`;
  }

  step(key: string, label: string, detail = "", state = "running", body: Node | null = null, kind = key) {
    const row = this.steps.get(key) ?? this.row(key, kind);
    if (body) {
      row.querySelector(".step-body")?.replaceChildren(body);
      row.classList.add("has-body");
      row.querySelector(".step-summary")?.setAttribute("title", "Show what this step did");
    }
    this.steps.forEach((other) => other !== row && other.dataset.state === "running" && (other.dataset.state = "done"));
    row.dataset.state = state;
    this.record(key, kind, label, detail, state);
    const [title, more] = [row.querySelector(".step-label"), row.querySelector(".step-detail")];
    if (title) title.textContent = label;
    if (detail && more) more.textContent = detail;
    if (state === "running") this.wait(detail || label);
  }

  private record(key: string, kind: string, label: string, detail: string, state: string) {
    const now = performance.now();
    const moment = this.moments.get(key) ?? { kind, label, detail, state, start: now, end: 0 };
    this.moments.set(key, Object.assign(moment, { label, state }, detail ? { detail } : {}));
    if (state !== "running") moment.end ||= now;
  }

  private run(done: Done): Run {
    const ended = performance.now();
    const gap = (kind: keyof typeof JOURNEY.gaps, start: number, end: number): Moment => ({ kind, label: JOURNEY.gaps[kind], detail: "", state: "done", start, end });
    const sorted = [...this.moments.values()].sort((a, b) => a.start - b.start);
    const steps: Moment[] = [];
    let reach = sorted[0]?.start ?? ended;
    sorted.forEach((moment, index) => {
      if (moment.start - reach > TIMING.gap) steps.push(gap("model", reach, moment.start));
      const end = moment.end || (sorted[index + 1]?.start ?? ended);
      steps.push({ ...moment, end });
      reach = Math.max(reach, end);
    });
    if (this.suggested.length && ended - reach > TIMING.gap) steps.push(gap("suggest", reach, ended));
    const verdict = this.verdict?.textContent ?? "";
    return { question: this.question, status: done.status, verdict, trace: done.trace_id, started: this.begun, ended, steps };
  }

  private row(key: string, kind: string) {
    const item = element("li", "step");
    const box = element("details", "step-box");
    const summary = element("summary", "step-summary");
    summary.append(element("span", "step-label"), element("span", "step-detail"));
    summary.addEventListener("click", (event) => item.classList.contains("has-body") || event.preventDefault());
    box.append(summary, element("div", "step-body"));
    item.append(box);
    const [code, why] = INSIDE[kind] ?? [];
    if (code && why) {
      const inside = element("p", "step-inside");
      inside.append(element("code", "step-code", code), element("span", "step-why", why));
      item.append(inside);
    }
    this.steps.set(key, item);
    this.trail.append(item);
    return item;
  }

  claim(number: number, text: string, citation: Citation) {
    this.typing = this.typing.then(async () => {
      const sentence = element("span", "sentence");
      this.answer.append(sentence);
      await type(sentence, `${text} `);
      sentence.normalize();
      const page = citation.url ? citation.page : null;
      const cite = button(`[${number}]`, "cite", page ? () => this.host.open(citation, number) : undefined);
      cite.title = page ? `Open page ${page} with the quote highlighted` : "From a tool result, not a page of the guide";
      cite.disabled = !page;
      if (page) {
        this.cited.add(page);
        this.host.cite(page);
      }
      sentence.append(cite, " ");
      this.scroll();
    });
  }

  note(text: string) {
    this.typing = this.typing.then(() => type(this.answer.appendChild(element("p", "note")), text));
  }

  async finish(done: Done) {
    await this.typing;
    const waiting = done.status === "awaiting_approval";
    this.steps.forEach((row) => row.dataset.state === "running" && (row.dataset.state = waiting ? "waiting" : "done"));
    const steps = plural(this.steps.size, "step");
    this.stop(waiting ? `Paused for your approval after ${steps}` : `Worked through ${steps}`, waiting ? "waiting" : "done");
    this.verdict = element("p", `verdict verdict-${done.status}`, STATUS[done.status] ?? done.status);
    if (done.error) this.verdict.textContent += ` ${done.reason ?? done.error}.`;
    const checked = done.checks?.length ?? 0;
    const removed = checked - (done.released?.length ?? 0);
    if (checked) this.verdict.textContent += ` ${checked - removed} of ${plural(checked, "claim")} verified${removed ? `, ${removed} removed` : ""}.`;
    if (done.status === "feedback" && done.note) await type(this.answer.appendChild(element("p", "note")), done.note);
    this.root.append(this.verdict);
    this.host.announce(this.verdict.textContent ?? "");
    if (done.trace_id && RATED.includes(done.status)) this.root.append(rating(done.trace_id, this.host));
    if (this.suggested.length) this.root.append(followups(this.suggested, this.host));
    done.approvals?.forEach((approval) => this.root.append(card(approval, this, this.host)));
    if (waiting) {
      this.paused = performance.now();
      this.moments.forEach((moment) => (moment.end ||= this.paused));
    } else {
      const run = this.run(done);
      this.root.append(button(JOURNEY.open, "journey-open", () => this.host.journey(run, false)));
      this.host.journey(run, true);
      this.host.resolve([...this.cited]);
    }
    this.host.say(waiting ? "Waiting for your approval" : "Ask a follow-up, or open a citation");
    this.scroll();
  }

  scroll() {
    const thread = this.host.thread;
    const reading = thread.scrollHeight - thread.scrollTop - thread.clientHeight > thread.clientHeight / 3;
    if (!reading) thread.scrollTo({ top: thread.scrollHeight, behavior: still.matches ? "auto" : "smooth" });
  }
}

function card(approval: Approval, turn: Turn, host: Host) {
  const title = APPROVALS[approval.tool]?.(approval.input) ?? `Allow ${approval.tool}?`;
  const reason = approval.input.rationale ?? approval.input.message;
  const root = element("div", "approval");
  root.append(element("p", "approval-title", title));
  if (reason) root.append(element("p", "approval-reason", String(reason)));
  if (approval.flags.includes("pressure"))
    root.append(element("p", "approval-flag", "This request pushes against the controls. Check it before approving."));
  const decide = async (reply: string | false) => {
    if (host.busy()) return;
    root.replaceChildren(spinning(reply ? "Approving and carrying it out" : "Recording the decline"));
    root.dataset.decided = reply ? "approved" : "declined";
    turn.resume();
    const done = await host.run(turn, { approvals: { [approval.id]: reply }, approver: "reviewer" });
    const outcome = !done
      ? "The request stopped before the gate answered. Ask again to see what was recorded."
      : reply
        ? "Approved. The steps below show what the gate did with it."
        : "Declined. Nothing was submitted.";
    root.replaceChildren(element("p", "approval-title", outcome));
  };
  const approve = button("Approve", "approve", () => decide(approval.hash));
  const actions = element("div", "approval-actions");
  actions.append(approve, button("Decline", "decline", () => decide(false)));
  root.append(actions);
  queueMicrotask(() => approve.focus());
  return root;
}

function thumb(helpful: boolean) {
  const node = button("", `rate rate-${helpful ? "up" : "down"}`);
  node.innerHTML = THUMB;
  node.setAttribute("aria-label", helpful ? "The answer helped" : "Something is wrong with the answer");
  node.title = helpful ? "Helpful" : "Not helpful: tell us what went wrong";
  return node;
}

function rating(trace: string, host: Host) {
  const root = element("div", "rating");
  const up = thumb(true);
  const down = thumb(false);
  root.append(element("span", "rating-label", "Was this answer right?"), up, down);
  up.addEventListener("click", async () => {
    if (host.busy()) return;
    root.replaceChildren(element("span", "rating-label", "Thanks, marked helpful."));
    if (!(await host.rate(trace, true))) root.replaceChildren(element("span", "rating-label", "The rating did not go through."));
  });
  down.addEventListener("click", () => {
    if (host.busy()) return;
    const form = element("form", "rating-form");
    const input = element("textarea", "rating-input");
    input.rows = 2;
    input.placeholder = "What went wrong? A wrong number, something missing, the wrong question answered";
    input.setAttribute("aria-label", "What went wrong");
    const send = element("button", "rating-send", "Send and answer again");
    send.type = "submit";
    form.append(input, send);
    root.replaceChildren(form);
    input.focus();
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const comment = input.value.trim();
      if (!comment || host.busy()) return;
      root.replaceChildren(element("span", "rating-label", "Sent."));
      host.run(new Turn(`Feedback: ${comment}`, host), { feedback: { trace_id: trace, helpful: false, comment } });
    });
  });
  return root;
}

export function suggestions(questions: string[], host: Host) {
  return questions.map((question) => button(question, "suggestion", () => host.ask(question)));
}

function followups(questions: string[], host: Host) {
  const root = element("div", "followups");
  root.append(element("p", "followups-title", "Ask next"), ...suggestions(questions, host));
  return root;
}
