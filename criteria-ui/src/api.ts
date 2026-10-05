export type Box = [number, number, number, number];

export type Citation = {
  claim: { text: string; quote: string };
  page: number | null;
  url: string | null;
  boxes: Box[];
};

export type Approval = {
  id: string;
  tool: string;
  input: Record<string, unknown>;
  hash: string;
  flags: string[];
};

export type AgentEvent =
  | { type: "stage"; stage: string; count?: number; status?: "done" | "failed"; reason?: string; details?: unknown }
  | {
      type: "tool";
      id: string;
      name: string;
      status: string;
      progress?: "ranking" | "kept" | "designed" | "read";
      count?: number;
      pages?: number[] | number;
      page?: number;
      done?: number;
      records?: number;
      fields?: string[];
      input?: unknown;
      output?: string;
    }
  | { type: "gate"; outcome: string; reason?: string; input?: unknown }
  | { type: "claim"; number: number; text: string; citation: Citation }
  | { type: "note"; text: string }
  | { type: "suggestions"; questions: string[] }
  | {
      type: "ingest";
      stage: "convert" | "classify" | "parse" | "read" | "structure" | "table" | "stored";
      status?: "done" | "failed";
      reason?: string;
      parsed?: boolean;
      banking?: boolean;
      confidence?: number;
      excerpt?: string;
      page?: number;
      pages?: number;
      number?: number;
      title?: string;
      rows?: number;
      tables?: number;
      dropped?: number;
    }
  | {
      type: "done";
      status: string;
      error?: string;
      reason?: string;
      note?: string;
      trace_id?: string;
      document_id?: string;
      name?: string;
      pages?: number;
      approvals?: Approval[];
      checks?: unknown[];
      released?: unknown[];
    };

export async function* invoke(body: object, session: string): AsyncGenerator<AgentEvent> {
  const response = await fetch("/invocations", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "X-Session-Id": session,
    },
    body: JSON.stringify(body),
  });
  if (!response.ok || !response.body) throw new Error(`The agent answered ${response.status}.`);
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) return;
      buffer += value.replaceAll("\r\n", "\n");
      const frames = buffer.split("\n\n");
      buffer = frames.pop() ?? "";
      for (const data of frames.flatMap(payloads)) {
        const event = JSON.parse(data);
        if (!event.type) throw new Error(event.message ?? event.error ?? "the stream failed");
        yield event as AgentEvent;
      }
    }
  } finally {
    reader.cancel().catch(() => {});
  }
}

const payloads = (frame: string) =>
  frame
    .split("\n")
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice("data:".length).trim())
    .filter(Boolean);

export const newSession = () => `ui-${crypto.randomUUID()}`;

export const encode = async (file: File) =>
  new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",")[1] ?? "");
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
