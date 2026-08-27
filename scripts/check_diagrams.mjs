#!/usr/bin/env node
/**
 * Parse every ```mermaid block in the docs with mermaid's own parser.
 *
 * A malformed diagram renders as raw text on GitHub with no error anywhere —
 * it just silently looks broken to every reader (F-001). Eyeballing does not
 * catch it; only the real parser does.
 *
 *   npm install mermaid@11 jsdom
 *   node scripts/check_diagrams.mjs
 */

import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { JSDOM } from "jsdom";

// mermaid needs a DOM at import time, and DOMPurify needs a real window.
const dom = new JSDOM("<!doctype html><html><body></body></html>");
global.window = dom.window;
global.document = dom.window.document;
global.Element = dom.window.Element;
global.Node = dom.window.Node;
global.HTMLElement = dom.window.HTMLElement;
Object.defineProperty(global, "navigator", {
  value: dom.window.navigator,
  configurable: true,
});

const mermaid = (await import("mermaid")).default;

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

function markdownFiles(dir) {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      return entry.name === "node_modules" || entry.name.startsWith(".")
        ? []
        : markdownFiles(full);
    }
    return entry.name.endsWith(".md") ? [full] : [];
  });
}

const files = markdownFiles(root);
let total = 0;
let failed = 0;

for (const file of files) {
  const text = fs.readFileSync(file, "utf8");
  const blocks = [...text.matchAll(/```mermaid\n([\s\S]*?)```/g)].map((m) => m[1]);
  for (const [i, block] of blocks.entries()) {
    total++;
    const label = `${path.relative(root, file)} block ${i + 1} (${block.split("\n")[0]})`;
    try {
      await mermaid.parse(block);
    } catch (error) {
      failed++;
      console.error(`FAIL ${label}\n     ${error.message}`);
    }
  }
}

console.log(`parsed ${total} mermaid block(s) across ${files.length} file(s), ${failed} failure(s)`);
process.exit(failed ? 1 : 0);
