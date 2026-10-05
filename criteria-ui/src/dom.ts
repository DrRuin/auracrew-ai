import { TIMING } from "./labels";

export const still = matchMedia("(prefers-reduced-motion: reduce)");

export function $<T extends HTMLElement>(selector: string): T {
  const found = document.querySelector<T>(selector);
  if (!found) throw new Error(`The page has no ${selector}`);
  return found;
}

export const plural = (count: number, word: string) => `${count} ${word}${count === 1 ? "" : "s"}`;

export const pause = (ms: number) => new Promise((resolve) => setTimeout(resolve, still.matches ? 0 : ms));

export function element<K extends keyof HTMLElementTagNameMap>(tag: K, className: string, text = "") {
  const node = document.createElement(tag);
  node.className = className;
  node.textContent = text;
  return node;
}

export function button(text: string, className: string, onClick?: () => void) {
  const node = element("button", className, text);
  node.type = "button";
  if (onClick) node.addEventListener("click", onClick);
  return node;
}

export async function type(target: HTMLElement, text: string) {
  if (still.matches) {
    target.append(text);
    return;
  }
  for (const [index, word] of text.split(" ").entries()) {
    target.append(index ? ` ${word}` : word);
    await pause(TIMING.typing);
  }
}

export function spinning(label: string) {
  const row = element("p", "pending");
  const icon = element("span", "spinner");
  icon.setAttribute("aria-hidden", "true");
  row.append(icon, element("span", "pending-label", label));
  return row;
}

export function parsed(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

export function pretty(value: unknown): string {
  const data = typeof value === "string" ? parsed(value) : value;
  return typeof data === "string" ? data : JSON.stringify(data, null, 2);
}

export function block(title: string, content: Node | string) {
  const section = element("div", "detail-block");
  section.append(element("p", "detail-title", title), typeof content === "string" ? element("pre", "detail-pre", content) : content);
  return section;
}

export type Row = [chip: string, text: string, meta: string];

export function listing(rows: Row[]) {
  const list = element("ul", "detail-list");
  for (const [chip, text, meta] of rows) {
    const item = element("li", "");
    item.append(element("span", `chip chip-${chip.replaceAll(" ", "_")}`, chip), element("span", "detail-text", text));
    if (meta) item.append(element("span", "detail-meta", meta));
    list.append(item);
  }
  return list;
}
