// Generates the Starlight content collection from the repository's docs/ directory,
// so the Markdown stays the single source of truth and nothing under
// src/content/docs is edited by hand.
import { existsSync, mkdirSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync, cpSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const REPO_URL = "https://github.com/awslabs/social-messaging-agent-runtime-toolkit";
export const BASE = "/social-messaging-agent-runtime-toolkit";

const siteRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = path.resolve(siteRoot, "..");

// Page slug for a path relative to docs/: adr/README.md is the section index.
export function slugFor(relDocsPath) {
  const noExt = relDocsPath.replace(/\.md$/, "");
  return noExt.replace(/(^|\/)README$/, "$1").replace(/\/$/, "");
}

// Applies fn to every line outside a fenced code block.
function mapOutsideFences(text, fn) {
  let fence = null;
  return text
    .split("\n")
    .map((line) => {
      const m = line.match(/^\s*(`{3,}|~{3,})/);
      if (m) {
        if (fence === null) fence = m[1][0];
        else if (m[1][0] === fence) fence = null;
        return line;
      }
      return fence === null ? fn(line) : line;
    })
    .join("\n");
}

// Splits the first H1 (outside code fences) off the body; Starlight renders the title itself.
export function extractTitle(markdown) {
  let title = null;
  let fence = null;
  const kept = [];
  for (const line of markdown.split("\n")) {
    const m = line.match(/^\s*(`{3,}|~{3,})/);
    if (m) {
      if (fence === null) fence = m[1][0];
      else if (m[1][0] === fence) fence = null;
    }
    if (fence === null && title === null && /^# /.test(line)) {
      title = line.slice(2).trim();
      continue;
    }
    kept.push(line);
  }
  return { title, body: kept.join("\n").replace(/^\n+/, "") };
}

// Rewrites one link target found in docs/<fromRel>. Links to other docs pages become
// site paths; links to anything else in the repository become GitHub URLs.
export function rewriteTarget(target, fromRel, repo = repoRoot) {
  if (/^([a-z][a-z0-9+.-]*:|#|\/\/)/i.test(target)) return target;
  const hashAt = target.search(/[#?]/);
  const filePart = hashAt === -1 ? target : target.slice(0, hashAt);
  const suffix = hashAt === -1 ? "" : target.slice(hashAt);
  if (filePart === "") return target;
  const resolved = path.posix.normalize(path.posix.join("docs", path.posix.dirname(fromRel), filePart));
  if (resolved.startsWith("docs/") && resolved.endsWith(".md")) {
    const slug = slugFor(resolved.slice("docs/".length));
    return `${BASE}/${slug ? `${slug}/` : ""}${suffix}`;
  }
  const isDir = existsSync(path.join(repo, resolved)) && statSync(path.join(repo, resolved)).isDirectory();
  return `${REPO_URL}/${isDir ? "tree" : "blob"}/main/${resolved.replace(/\/$/, "")}${suffix}`;
}

// Inline links and images, [text](target) and ![alt](target), outside code fences.
// Inline code spans are skipped so examples in prose keep their literal text.
export function rewriteLinks(markdown, fromRel, repo = repoRoot) {
  return mapOutsideFences(markdown, (line) =>
    line
      .split(/(`[^`]*`)/)
      .map((part, i) =>
        i % 2 === 1
          ? part
          : part.replace(/(\]\()([^)\s]+)(\))/g, (_, open, target, close) => open + rewriteTarget(target, fromRel, repo) + close),
      )
      .join(""),
  );
}

export function convertPage(markdown, relDocsPath, repo = repoRoot) {
  const { title, body } = extractTitle(markdown);
  if (!title) throw new Error(`docs/${relDocsPath} has no H1 to use as the page title`);
  const front = [
    "---",
    `title: ${JSON.stringify(title)}`,
    `editUrl: ${JSON.stringify(`${REPO_URL}/edit/main/docs/${relDocsPath}`)}`,
    "---",
    "",
  ].join("\n");
  return front + rewriteLinks(body, relDocsPath, repo);
}

function listMarkdown(dir, rel = "") {
  return readdirSync(path.join(dir, rel), { withFileTypes: true }).flatMap((entry) => {
    const next = path.posix.join(rel, entry.name);
    if (entry.isDirectory()) return entry.name === "assets" ? [] : listMarkdown(dir, next);
    return entry.name.endsWith(".md") ? [next] : [];
  });
}

export function main() {
  const docsDir = path.join(repoRoot, "docs");
  const outDir = path.join(siteRoot, "src", "content", "docs");
  rmSync(outDir, { recursive: true, force: true });
  const pages = listMarkdown(docsDir);
  for (const rel of pages) {
    // A directory's README.md becomes its index page (adr/README.md -> adr/index.md).
    const dest = path.join(outDir, rel.endsWith("README.md") ? rel.replace(/README\.md$/, "index.md") : rel);
    mkdirSync(path.dirname(dest), { recursive: true });
    writeFileSync(dest, convertPage(readFileSync(path.join(docsDir, rel), "utf8"), rel));
  }
  cpSync(path.join(siteRoot, "home.mdx"), path.join(outDir, "index.mdx"));
  console.log(`prepare-content: wrote ${pages.length + 1} pages to ${path.relative(repoRoot, outDir)}`);
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) main();
