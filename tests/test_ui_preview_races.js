"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function response(data, ok = true) {
  return { ok, async json() { return data; } };
}

function makeHarness() {
  const elements = new Map();
  const cleanups = [];
  const previewRequests = [];

  function makeElement() {
    return {
      hidden: true,
      value: "",
      textContent: "",
      dataset: {},
      disabled: false,
      open: false,
      addEventListener() {},
      append() {},
      appendChild() {},
      replaceChildren() {},
      focus() {},
      querySelector() { return makeElement(); },
      closest() { return null; },
      matches() { return false; },
      classList: { add() {}, remove() {} },
    };
  }

  const document = {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, makeElement());
      return elements.get(id);
    },
    createElement() { return makeElement(); },
    addEventListener() {},
  };

  async function fetch(url, options = {}) {
    if (url === "/api/apps") return response([]);
    if (url === "/api/onboarding/github/cleanup") {
      cleanups.push(JSON.parse(options.body).staging_id);
      return response({ cleaned: true });
    }
    if (url === "/api/onboarding/github/preview") {
      return new Promise((resolve) => previewRequests.push(resolve));
    }
    throw new Error(`unexpected request: ${url}`);
  }

  const window = {
    confirm: () => true,
    alert() {},
    setInterval() { return 1; },
  };
  const context = vm.createContext({ console, document, fetch, URL, window, setTimeout, clearTimeout });
  const source = fs.readFileSync(path.join(__dirname, "..", "static", "app.js"), "utf8");
  vm.runInContext(source, context, { filename: "static/app.js" });
  return { context, elements, cleanups, previewRequests };
}

async function tick() {
  await new Promise((resolve) => setImmediate(resolve));
}

test("closing during GitHub preview cleans the late staging result", async () => {
  const harness = makeHarness();
  vm.runInContext("byId('githubUrl').value = 'https://github.com/owner/repo'", harness.context);
  vm.runInContext("showAddDialog()", harness.context);
  const pending = vm.runInContext("previewApp('github')", harness.context);
  await tick();
  assert.equal(harness.previewRequests.length, 1);

  vm.runInContext("closeAddDialog()", harness.context);
  harness.previewRequests[0](response({ staging_id: "repo-late", digest: "late", app: { id: "late" } }));
  await pending;
  assert.deepEqual(harness.cleanups, ["repo-late"]);
  assert.equal(vm.runInContext("previewState", harness.context), null);
});

test("overlapping GitHub previews keep only the newest and clean the stale result", async () => {
  const harness = makeHarness();
  vm.runInContext("byId('githubUrl').value = 'https://github.com/owner/repo'", harness.context);
  vm.runInContext("showAddDialog()", harness.context);

  const older = vm.runInContext("previewApp('github')", harness.context);
  await tick();
  const newer = vm.runInContext("previewApp('github')", harness.context);
  await tick();
  assert.equal(harness.previewRequests.length, 2);

  harness.previewRequests[1](response({ staging_id: "repo-new", digest: "new", app: { id: "new" } }));
  await newer;
  harness.previewRequests[0](response({ staging_id: "repo-old", digest: "old", app: { id: "old" } }));
  await older;

  assert.equal(vm.runInContext("previewState.staging_id", harness.context), "repo-new");
  assert.deepEqual(harness.cleanups, ["repo-old"]);
  vm.runInContext("closeAddDialog()", harness.context);
  await tick();
  assert.deepEqual(harness.cleanups, ["repo-old", "repo-new"]);
});
