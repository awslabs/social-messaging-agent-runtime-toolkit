import assert from "node:assert/strict";
import test from "node:test";
import { BASE, REPO_URL, convertPage, extractTitle, rewriteTarget, slugFor } from "./prepare-content.mjs";

test("slugFor turns a directory README into the section index", () => {
  assert.equal(slugFor("adr/README.md"), "adr");
  assert.equal(slugFor("adr/0001-x.md"), "adr/0001-x");
  assert.equal(slugFor("state.md"), "state");
});

test("extractTitle takes the first H1 outside code fences", () => {
  const { title, body } = extractTitle("```\n# not a title\n```\n\n# Real title\n\ntext");
  assert.equal(title, "Real title");
  assert.ok(body.includes("# not a title"));
  assert.ok(!body.includes("# Real title"));
});

test("links to other docs pages become site paths and keep anchors", () => {
  assert.equal(rewriteTarget("configuration.md#model", "state.md"), `${BASE}/configuration/#model`);
  assert.equal(rewriteTarget("0001-x.md", "adr/0002-y.md"), `${BASE}/adr/0001-x/`);
  assert.equal(rewriteTarget("../architecture.md", "adr/0001-x.md"), `${BASE}/architecture/`);
  assert.equal(rewriteTarget("README.md", "adr/0001-x.md"), `${BASE}/adr/`);
});

test("links outside docs/ become GitHub URLs", () => {
  assert.equal(rewriteTarget("../AGENTS.md#known-issues", "state.md"), `${REPO_URL}/blob/main/AGENTS.md#known-issues`);
  assert.equal(rewriteTarget("../../CONTRIBUTING.md", "adr/0001-x.md"), `${REPO_URL}/blob/main/CONTRIBUTING.md`);
  assert.equal(rewriteTarget("../infra-terraform/", "state.md"), `${REPO_URL}/tree/main/infra-terraform`);
});

test("absolute, in-page and mailto links are untouched", () => {
  for (const t of ["https://example.com/x.md", "#section", "mailto:a@b.c"]) assert.equal(rewriteTarget(t, "state.md"), t);
});

test("convertPage adds frontmatter and leaves code fences and inline code alone", () => {
  const out = convertPage("# T: \"q\"\n\nSee [c](configuration.md) and `[x](y.md)`.\n\n```\n[a](b.md)\n```\n", "state.md");
  assert.ok(out.startsWith('---\ntitle: "T: \\"q\\""\n'));
  assert.ok(out.includes(`(${BASE}/configuration/)`));
  assert.ok(out.includes("`[x](y.md)`"));
  assert.ok(out.includes("[a](b.md)"));
});

test("a page without an H1 is an error", () => {
  assert.throws(() => convertPage("no heading", "x.md"), /no H1/);
});
