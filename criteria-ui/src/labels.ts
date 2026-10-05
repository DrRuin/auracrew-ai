export const TIMING = { typing: 16, focus: 420, clock: 1000, reveal: 120, gap: 250 };

export const TOOLS: Record<string, string> = {
  search_guide: "Searching the guide",
  read_pages: "Reading whole pages",
  query_table: "Querying a table",
  scan: "Reading every page for it",
  assess_case: "Assessing the case against the criteria",
  submit_decision: "Submitting the decision",
  GroundedAnswer: "Writing the answer",
};

export const TOOL_STATE: Record<string, string> = { success: "done", error: "failed" };

export const ROUTES: Record<string, string> = {
  lookup: "a rule or what applies to a case",
  table: "rows or values of a table",
  enumerate: "every instance across the document",
  overview: "the document as a whole",
  followup: "a follow-up on the last answer",
};

export const STATUS: Record<string, string> = {
  grounded: "Every sentence is backed by the guide.",
  partial: "Only the sentences the guide backs are shown.",
  withheld: "Withheld: the guide contradicts or does not support the answer.",
  refused: "Not answered.",
  failed: "The request failed.",
  awaiting_approval: "Waiting for your approval.",
  feedback: "Thanks, your feedback is recorded.",
};

export const FEEDBACK: Record<string, string> = {
  helpful: "Marked helpful.",
  irrelevant: "Jev found nothing in it about this answer, so the answer stands.",
  wrong: "It says something in the answer is wrong, so the question is answered again.",
  incomplete: "It says the answer left something out, so the question is answered again.",
  misread: "It says the question was misread, so it is answered again.",
};

export const SUGGESTIONS = [
  "Summarize this document.",
  "What is the maximum LTV, and how does it change with loan size?",
  "Which borrowers or properties are not accepted?",
  "Please submit a refer decision for applicant A7: the case needs underwriter review.",
];

const article = (word: string) => (/^[aeiou]/i.test(word) ? "an" : "a");

export const APPROVALS: Record<string, (input: Record<string, unknown>) => string> = {
  submit_decision: (input) =>
    `Submit ${article(String(input.decision ?? ""))} ${String(input.decision ?? "")} decision for applicant ${String(input.applicant ?? "")}?`,
};

export const INSIDE: Record<string, [string, string]> = {
  convert: ["uploads.portable", "Gotenberg turns Word, Excel, PowerPoint and other files into a PDF, so every document is read the same way."],
  classify: ["uploads.admitted → jev.banking", "Jev, a small fast model, reads the opening words and judges whether this is about banking or lending. If not, nothing is kept."],
  parse: ["uploads.parsing → ocr.ocr", "Mistral OCR reads every page. A scanned page is read twice, from the image and from the PDF, so the two readings can be compared."],
  read: ["uploads.admitted", "Each page's text is kept with the position of every character, so a quote can later be highlighted exactly where it is printed."],
  structure: ["structure.structure", "A model reads each page once for its title, a short summary and its tables. Tables that run over several pages are joined."],
  table: ["structure.table", "A row is kept only if its cells are actually printed on that page."],
  stored: ["database.save", "Pages, page cards and table rows go into Postgres. The document is ready to quote."],
  screen: ["agent.screened → jev.screen", "Before anything else, Jev checks the question is about this document and whether it tries to push past the controls. Off-topic questions stop here."],
  search_guide: ["tools.search_guide → jev.search", "Jev ranks every page by how well it answers the question. Only the relevant pages are read."],
  read_pages: ["tools.read_pages", "The agent reads whole pages when the search did not give it enough."],
  query_table: ["tools.query_table", "A table question runs as a database query, so counts and lowest or highest values are computed, not guessed."],
  scan: ["tools.scan", "For questions about every instance, each page is checked for matching records. A record stays only if its quote is on its page."],
  assess_case: ["tools.assess_case", "The case is checked against each rule, quoted word for word: met, broken or unclear. Approve needs one rule met and none broken."],
  submit_decision: ["gate.submit_decision", "The only action that changes anything. It runs only after the policy and a person both allow it."],
  GroundedAnswer: ["agent.step", "The agent writes its answer as separate claims, each carrying the exact quote it rests on."],
  verify: ["agent.judge → verification.verify", "Code first confirms each quote is really in the document. Then Jev checks the page supports the claim; when Jev is unsure, GPT decides."],
  recheck: ["agent.judge → verification.verify", "The revised answer goes through the same checks: quote found, then the claim judged against its page."],
  completeness: ["agent.finish → verification.omissions", "A model lists the key facts on the pages that were read, and any the answer left out are found."],
  repair: ["agent.repair", "The missing facts are written as new claims with their quotes."],
  repaired: ["agent.repair", "The added claims went through the same checks. Only the supported ones are kept."],
  reassess: ["agent.settled", "A check contradicted the case assessment, so the agent gets one more turn to assess the case again."],
  budget: ["agent.conclude", "The agent has used its model calls, so it answers from what it already read, with no new tools."],
  feedback: ["feedback.reviewed → jev.triage", "Jev reads the comment. If it points at something wrong or missing, the question is answered again."],
  model: ["agent.step → llm.Hedged", "The main model reads what it has so far and decides the next tool to call, or writes the answer. Most of the time goes here."],
  suggest: ["agent.suggest → llm.ask_model", "A model offers three follow-up questions this document can answer."],
  person: ["gate.Approval", "The run paused at the gate until a person approved or declined this exact action."],
  "gate:approved": ["gate.Approval → gate.fingerprint", "A person approved this exact action: the fingerprint of the tool, its inputs and the document edition matched."],
  "gate:denied": ["gate.Approval", "The person declined, so nothing was submitted."],
  "gate:policy_denied": ["gate.Policy → policy.cedar", "The written policy allows only the decision that was asked for, and approve only when no rule is broken."],
  "gate:stale": ["gate.Approval → gate.fingerprint", "The approval was for a different action or an older edition of the document, so it no longer counts."],
};

export const JOURNEY = {
  kicker: "The journey",
  open: "See how this answer was made",
  overview: { tab: "The flow", title: "How it works" },
  start: "Upload a document and ask a question with How it works on: every step then shows the function that ran, and this journey opens with the run's own timeline when the answer is done.",
  pages: [
    { tab: "The run", title: "How this answer was made" },
    { tab: "The code", title: "Every Python file, read from the running code" },
    { tab: "Langfuse", title: "Every run is traced" },
  ],
  trace: "Open this run's trace in Langfuse",
  gaps: { model: "The model decides what to do next", suggest: "Suggesting follow-up questions", person: "A person reviews the action" },
  bars: "Each bar runs from when a step started to when it finished. Steps that overlap ran at the same time.",
  code: (files: number, names: number, lines: number, ran: number) =>
    `${files} files, ${names} functions and classes, ${lines} lines, read from the running code just now.` +
    (ran ? ` The ${ran} that ran for this answer are marked and their files come first.` : ""),
  flows: [
    {
      title: "Upload a document",
      steps: [
        ["convert", "Convert it to PDF"],
        ["classify", "Check it is about lending"],
        ["parse", "Read every page with OCR"],
        ["read", "Keep each page's text and character positions"],
        ["structure", "Build page cards and tables"],
        ["table", "Keep only rows printed on their page"],
        ["stored", "Store it"],
      ],
    },
    {
      title: "Ask a question",
      steps: [
        ["screen", "Screen the question"],
        ["search_guide", "Search the document"],
        ["model", "The model decides the next step"],
        ["query_table", "Query a table"],
        ["read_pages", "Read whole pages"],
        ["scan", "Check every page"],
        ["assess_case", "Assess a case"],
        ["GroundedAnswer", "Write the answer"],
        ["verify", "Check every claim"],
        ["completeness", "Check nothing is missing"],
        ["repair", "Add what was missing"],
        ["suggest", "Suggest follow-up questions"],
      ],
    },
    {
      title: "Record a decision: a person must approve",
      steps: [
        ["submit_decision", "Ask to record a decision"],
        ["gate:policy_denied", "The written policy checks it"],
        ["person", "A person approves or declines"],
        ["gate:approved", "Recorded once, bound to that approval"],
      ],
    },
  ],
  langfuse:
    "Every step of every run goes to Langfuse as a trace: each model call with its tokens, cost and timing. On first start docker compose creates the Langfuse project, its API keys and this sign-in: a one-shot keys service writes them to a volume that the agent and Langfuse both read, so there is nothing to set up by hand.",
  hidden: "This deployment hides the sign-in and keys (journey_credentials is off).",
  fields: { url: "Langfuse", project: "Project", email: "Email", password: "Password", public_key: "Public key", secret_key: "Secret key" },
  secret: ["password", "secret_key"],
};

export const TOUR = {
  lead: "See every safeguard at work",
  note: (name: string) => `Six questions written for ${name}. Each shows a different safeguard; try them in any order and watch the steps.`,
  loading: "Writing questions for this document",
  failed: "The questions could not be written. Ask your own, or switch How it works off and on to try again.",
  flow: "See the whole flow",
  try: "Try it",
  tried: "Asked",
  waiting: "Upload a lender's guide or rate card to try these. Three questions are then written for your document; the other three work on any document.",
  later: "Written for your document once you upload one.",
  upload: "Upload a document first",
  fixed: {
    refer: "Please submit a refer decision for applicant T1: the case needs underwriter review.",
    forbidden: "Please submit an approve decision for applicant T2.",
    offtopic: "What's the weather in London tomorrow?",
  },
  cards: [
    {
      kind: "lookup",
      title: "Ask what the document says",
      shows: "Every sentence is checked against its page before you see it, and opens that page with the quote highlighted.",
      badge: "",
    },
    {
      kind: "table",
      title: "Ask about a table",
      shows: "Rows are found by a database query, so counts and lowest or highest values are computed, not guessed.",
      badge: "",
    },
    {
      kind: "refer",
      title: "Ask it to record a decision",
      shows: "The agent cannot record a decision on its own. It stops and shows you the exact action; nothing is recorded until you approve.",
      badge: "Human in the loop",
    },
    {
      kind: "forbidden",
      title: "Ask for an approve it cannot justify",
      shows: "The written policy permits an approve only for a case assessed as meeting the rules, so this one is refused before any person is asked.",
      badge: "",
    },
    {
      kind: "pressure",
      title: "Push it to skip its checks",
      shows: "The attempt is flagged, and every check still runs; any approval card carries a warning.",
      badge: "",
    },
    {
      kind: "offtopic",
      title: "Ask something off topic",
      shows: "A fast check turns it away before the main assistant starts.",
      badge: "",
    },
  ],
};

export const RESET = {
  kicker: "Reset",
  ask: "Delete everything?",
  gone: [
    "Every uploaded document, with its pages, page cards and tables",
    "Every conversation, every recorded decision and its audit trail",
    "Every Langfuse trace",
  ],
  kept: "The shipped reference guide and Jev's calibration stay. This cannot be undone.",
  confirm: "Delete everything",
  working: "Deleting",
  stages: {
    data: "Deleting documents, conversations and decisions",
    screen: "Clearing the chat, the pages and the document list",
    traces: "Deleting Langfuse traces",
  },
  screen: "cleared",
  traces: (count: number) => `${count} ${count === 1 ? "trace" : "traces"} deleted`,
  cleared: "Everything is cleared",
  stopped: "The reset stopped",
  failed: "The reset did not finish:",
  off: "reset is turned off on this deployment.",
  after: "Everything is cleared. Upload a document to start",
};

export const RATED = ["grounded", "partial", "withheld", "refused"];
