import type { Box, Citation } from "./api";
import { element, still } from "./dom";
import { open, render } from "./pdf";

export class Viewer {
  private sheet: HTMLElement;
  private caption: HTMLElement;
  private quote: HTMLElement;
  private closer: HTMLButtonElement;
  private opener: HTMLElement | null = null;
  private shown = 0;

  constructor(
    private root: HTMLElement,
    private onClose: () => void,
  ) {
    const part = <T extends HTMLElement>(selector: string) => {
      const found = root.querySelector<T>(selector);
      if (!found) throw new Error(`The viewer has no ${selector}`);
      return found;
    };
    this.sheet = part(".viewer-sheet");
    this.caption = part(".viewer-caption");
    this.quote = part(".viewer-quote");
    this.closer = part<HTMLButtonElement>(".viewer-close");
    this.closer.addEventListener("click", () => this.close());
    addEventListener("keydown", (event) => event.key === "Escape" && !root.hidden && this.close());
  }

  show(citation: Citation, number: number, name: string) {
    const [path = "", page = ""] = (citation.url ?? "").split("#page=");
    return this.display(path, Number(page), `[${number}] ${name}, page ${page}`, citation.claim.quote, citation.boxes);
  }

  page(path: string, page: number, name: string) {
    return this.display(path, page, `${name}, page ${page}`, "", []);
  }

  close() {
    if (this.root.hidden) return;
    this.shown++;
    this.root.hidden = true;
    this.onClose();
    this.opener?.focus();
  }

  private async display(path: string, page: number, caption: string, quote: string, boxes: Box[]) {
    const shown = ++this.shown;
    this.opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    this.caption.textContent = caption;
    this.quote.textContent = quote;
    this.quote.hidden = !quote;
    const loading = element("p", "viewer-loading");
    const icon = element("span", "spinner");
    icon.setAttribute("aria-hidden", "true");
    loading.append(icon, `Loading page ${page}`);
    this.sheet.replaceChildren(loading);
    this.root.hidden = false;
    const width = Math.min(this.root.clientWidth - 48, 760) * Math.min(devicePixelRatio, 2);
    const canvas = await open(path)
      .then((pdf) => render(pdf, page, width))
      .catch(() => null);
    if (shown !== this.shown) return;
    if (!canvas) {
      this.sheet.textContent = "This page could not be loaded. Close it and try again.";
      return;
    }
    const marks = boxes.map(([left, top, right, bottom]) => {
      const mark = element("span", "mark");
      Object.assign(mark.style, {
        left: `${left * 100}%`,
        top: `${top * 100}%`,
        width: `${(right - left) * 100}%`,
        height: `${(bottom - top) * 100}%`,
      });
      return mark;
    });
    this.sheet.replaceChildren(canvas, ...marks);
    marks[0]?.scrollIntoView({ block: "center", behavior: still.matches ? "auto" : "smooth" });
    this.closer.focus();
  }
}
