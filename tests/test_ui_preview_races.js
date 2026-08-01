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

  function makeElement(initial = {}) {
    return {
      ...initial,
      hidden: true,
      value: "",
      textContent: "",
      dataset: {},
      disabled: false,
      open: false,
      style: {},
      tabIndex: -1,
      focusCount: 0,
      attributes: {},
      addEventListener() {},
      setAttribute(name, value) { this.attributes[name] = String(value); },
      append() {},
      appendChild() {},
      replaceChildren() {},
      focus() { this.focusCount += 1; },
      querySelector() { return makeElement(); },
      closest() { return null; },
      matches() { return false; },
      classList: { add() {}, remove() {} },
    };
  }

  const document = {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, makeElement(id === "drawer" ? { inert: true } : {}));
      return elements.get(id);
    },
    createElement() { return makeElement(); },
    querySelectorAll() { return []; },
    addEventListener() {},
  };

  async function fetch(url, options = {}) {
    if (url === "/api/apps") return response([]);
    if (url === "/api/extensions") return response({ enabled: false, widgets: [], error: "" });
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
    open() {},
    location: { hostname: "127.0.0.1" },
    setTimeout() { return 1; },
    setInterval() { return 1; },
  };
  const context = vm.createContext({ console, document, fetch, URL, window, setTimeout, clearTimeout });
  const source = fs.readFileSync(path.join(__dirname, "..", "static", "app.js"), "utf8");
  vm.runInContext(source, context, { filename: "static/app.js" });
  return { context, elements, cleanups, previewRequests };
}

test("drawer starts inert and toggles inert with aria-hidden while returning focus", () => {
  const source = fs.readFileSync(path.join(__dirname, "..", "appdock.py"), "utf8");
  assert.match(source, /<aside id="drawer"[^>]*aria-hidden="true"[^>]*inert/);
  const harness = makeHarness();
  const drawer = harness.elements.get("drawer") || harness.context.document?.getElementById("drawer");
  vm.runInContext("openDrawer()", harness.context);
  assert.equal(drawer.inert, false);
  assert.equal(drawer.attributes["aria-hidden"], "false");
  vm.runInContext("closeDrawer()", harness.context);
  assert.equal(drawer.inert, true);
  assert.equal(drawer.attributes["aria-hidden"], "true");
  assert.equal(harness.elements.get("menuButton").focusCount, 1);
});

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

test("extension widgets use DOM text and context-aware app links", () => {
  const source = fs.readFileSync(path.join(__dirname, "..", "static", "app.js"), "utf8");
  assert.equal(source.includes("innerHTML"), false);
  assert.match(source, /textContent/);
  const harness = makeHarness();
  assert.equal(vm.runInContext("isLoopbackContext()", harness.context), true);
  vm.runInContext("window.location.hostname = 'private.example.invalid'", harness.context);
  assert.equal(vm.runInContext("isLoopbackContext()", harness.context), false);
  assert.equal(vm.runInContext("safeUrl('https://user:pass@example.invalid')", harness.context), "");
});
