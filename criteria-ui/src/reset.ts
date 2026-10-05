import { button, element, plural } from "./dom";
import { RESET } from "./labels";

type Stage = { stage: "data" | "traces" | "done"; status?: "done" | "failed"; reason?: string; deleted?: number } & Partial<
  Record<"documents" | "conversations" | "decisions", number>
>;

export class Reset {
  private running = false;
  private bar = element("div", "reset-fill");
  private rows = new Map<string, HTMLLIElement>();

  constructor(
    private root: HTMLDialogElement,
    private clear: () => void,
    private busy: (on: boolean) => void,
  ) {
    root.addEventListener("cancel", (event) => this.running && event.preventDefault());
  }

  open() {
    const list = element("ul", "reset-list");
    list.append(...RESET.gone.map((line) => element("li", "", line)));
    const body = element("div", "journey-body");
    body.append(list, element("p", "journey-note", RESET.kept));
    const confirm = button(RESET.confirm, "journey-nav reset-confirm", () => this.run());
    this.show(RESET.ask, body, button("Cancel", "journey-nav", () => this.root.close()), confirm);
    this.root.showModal();
    confirm.focus();
  }

  private show(title: string, body: HTMLElement, ...actions: HTMLElement[]) {
    const head = element("header", "journey-head");
    head.append(element("p", "journey-kicker", RESET.kicker), element("h2", "journey-title", title));
    const foot = element("footer", "journey-foot");
    foot.append(...actions);
    this.root.replaceChildren(head, body, foot);
    this.root.setAttribute("aria-label", title);
  }

  private step(name: keyof typeof RESET.stages, state: string, detail = "") {
    const row = this.rows.get(name) ?? element("li", "reset-step");
    if (!this.rows.has(name)) {
      row.append(element("span", "reset-label", RESET.stages[name]), element("span", "reset-detail"));
      this.rows.set(name, row);
    }
    row.dataset.state = state;
    const shown = row.querySelector(".reset-detail");
    if (shown) shown.textContent = detail;
  }

  private progress(share: number) {
    this.bar.style.width = `${Math.round(share * 100)}%`;
    this.bar.parentElement?.setAttribute("aria-valuenow", String(Math.round(share * 100)));
  }

  private async run() {
    this.running = true;
    this.busy(true);
    this.rows.clear();
    const track = element("div", "reset-track");
    track.setAttribute("role", "progressbar");
    track.setAttribute("aria-label", RESET.working);
    track.setAttribute("aria-valuemin", "0");
    track.setAttribute("aria-valuemax", "100");
    track.append(this.bar);
    const steps = element("ul", "reset-steps");
    const body = element("div", "journey-body");
    body.append(track, steps);
    this.show(RESET.working, body);
    this.step("data", "running");
    steps.append(...this.rows.values());
    this.progress(0.08);
    let ended = false;
    try {
      ended = await this.stream(steps);
    } catch (error) {
      body.append(element("p", "journey-note", `${RESET.failed} ${(error as Error).message}`));
    } finally {
      this.running = false;
      this.busy(false);
    }
    const done = button("Done", "journey-nav journey-next", () => this.root.close());
    this.show(ended ? RESET.cleared : RESET.stopped, body, done);
    done.focus();
  }

  private async stream(steps: HTMLElement): Promise<boolean> {
    const init = { method: "POST", headers: { "content-type": "application/json" }, body: "{}" };
    const response = await fetch("/api/reset", init);
    if (!response.ok || !response.body) throw new Error(response.status === 403 ? RESET.off : `the agent answered ${response.status}.`);
    const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) return false;
      buffer += value;
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      for (const line of lines.filter(Boolean)) if (this.apply(JSON.parse(line) as Stage, steps)) return true;
    }
  }

  private apply(event: Stage, steps: HTMLElement): boolean {
    const deleted = event.deleted ?? 0;
    if (event.stage === "data" && event.status === "done") {
      const counts = [plural(event.documents ?? 0, "document"), plural(event.conversations ?? 0, "conversation"), plural(event.decisions ?? 0, "decision")];
      this.step("data", "done", counts.join(", "));
      this.clear();
      this.step("screen", "done", RESET.screen);
      this.step("traces", "running", RESET.traces(0));
      steps.append(...this.rows.values());
      this.progress(0.45);
    }
    if (event.stage === "traces" && !event.status) {
      this.step("traces", "running", RESET.traces(deleted));
      this.progress(0.45 + 0.5 * (deleted / (deleted + 40)));
    }
    if (event.stage === "traces" && event.status) this.step("traces", event.status, event.status === "failed" ? `${RESET.traces(deleted)}; stopped (${event.reason})` : RESET.traces(deleted));
    if (event.stage === "done") this.progress(1);
    return event.stage === "done";
  }
}
