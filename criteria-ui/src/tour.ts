import { button, element, spinning } from "./dom";
import { TOUR } from "./labels";

type Questions = Record<string, string>;

function fill(cards: HTMLElement, questions: Questions, ask: (question: string) => boolean, waiting = false) {
  const shown = TOUR.cards.filter((card) => waiting || questions[card.kind]);
  cards.replaceChildren(
    ...shown.map(({ kind, title, shows, badge }) => {
      const question = questions[kind] ?? "";
      const card = element("article", `tour-card tour-${kind}`);
      const go = button(waiting ? TOUR.upload : TOUR.try, "tour-try", () => {
        if (!ask(question)) return;
        card.dataset.tried = "";
        go.textContent = TOUR.tried;
      });
      go.disabled = waiting;
      if (badge) card.append(element("span", "chip tour-badge", badge));
      card.append(element("p", "tour-title", title), element("p", "tour-shows", shows), element("p", "tour-question", question ? `“${question}”` : TOUR.later), go);
      return card;
    }),
  );
}

export function tour(document: string | null, name: string, ask: (question: string) => boolean, overview: () => void) {
  const root = element("li", "tour");
  root.dataset.document = document ?? "";
  const cards = element("div", "tour-cards");
  root.append(element("p", "tour-lead", TOUR.lead), element("p", "tour-note", document ? TOUR.note(name) : TOUR.waiting), cards, button(TOUR.flow, "journey-open", overview));
  if (!document) {
    fill(cards, TOUR.fixed, ask, true);
    return root;
  }
  cards.append(spinning(TOUR.loading));
  fetch(`/journey/tour/${document}`)
    .then((response) => (response.ok ? (response.json() as Promise<Questions>) : Promise.reject(new Error(String(response.status)))))
    .then((questions) => fill(cards, { ...TOUR.fixed, ...questions }, ask))
    .catch(() => cards.replaceChildren(element("p", "tour-note", TOUR.failed)));
  return root;
}
