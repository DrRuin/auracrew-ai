import { cpSync } from "node:fs";
import { defineConfig } from "vite";

for (const folder of ["wasm", "standard_fonts"])
  cpSync(`node_modules/pdfjs-dist/${folder}`, `public/pdfjs/${folder}`, { recursive: true });

const agent = process.env.AGENT_URL ?? "http://127.0.0.1:8080";

export default defineConfig({
  build: {
    rolldownOptions: {
      output: {
        codeSplitting: {
          groups: [
            { name: "three", test: /node_modules[\/]three/ },
            { name: "pdfjs", test: /node_modules[\/]pdfjs-dist/ },
          ],
        },
      },
    },
  },
  server: {
    proxy: {
      "/invocations": agent,
      "/documents": agent,
      "/journey": agent,
      "/api/reset": { target: agent, rewrite: () => "/reset" },
    },
  },
});
