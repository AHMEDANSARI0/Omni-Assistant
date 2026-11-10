/**
 * Removes only the generated bundle from the Python package before a build.
 * Deliberately narrow: `webui/__init__.py` is hand-written package code and must
 * survive, so this never empties the directory wholesale.
 */
import { rm } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const target = resolve(here, "../../src/pc_agent/webui");

for (const entry of ["assets", "index.html"]) {
  await rm(resolve(target, entry), { recursive: true, force: true });
}
console.log(`cleaned generated bundle in ${target}`);
