import { GlobalWorkerOptions, getDocument, type PDFDocumentProxy } from "pdfjs-dist";
import worker from "pdfjs-dist/build/pdf.worker.min.mjs?url";

GlobalWorkerOptions.workerSrc = worker;

const ASSETS = { wasmUrl: "/pdfjs/wasm/", standardFontDataUrl: "/pdfjs/standard_fonts/" };

const opened = new Map<string, Promise<PDFDocumentProxy>>();

export function open(url: string) {
  const known = opened.get(url);
  if (known) return known;
  const loading = getDocument({ url, ...ASSETS }).promise;
  loading.catch(() => opened.delete(url));
  opened.set(url, loading);
  return loading;
}

export const openBytes = (bytes: ArrayBuffer) => getDocument({ data: new Uint8Array(bytes), ...ASSETS }).promise;

export function forget() {
  opened.forEach((loading) => loading.then((pdf) => pdf.loadingTask.destroy()).catch(() => {}));
  opened.clear();
}

export async function render(pdf: PDFDocumentProxy, number: number, width: number) {
  const page = await pdf.getPage(number);
  const viewport = page.getViewport({ scale: width / page.getViewport({ scale: 1 }).width });
  const canvas = document.createElement("canvas");
  canvas.width = Math.floor(viewport.width);
  canvas.height = Math.floor(viewport.height);
  await page.render({ canvas, viewport }).promise;
  page.cleanup();
  return canvas;
}
