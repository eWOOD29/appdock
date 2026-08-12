"use strict";

const byId = (id) => document.getElementById(id);
let previewState = null;
let previewKind = "";
let verifiedRelease = null;
let previewRequestId = 0;
let addDialogOpen = false;

function element(tag, text = "", className = "") {
  const node = document.createElement(tag);
  node.textContent = String(text ?? "");
  if (className) node.className = className;
  return node;
}

function safeUrl(value) {
  try {
    const url = new URL(String(value));
    return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password ? url.href : "";
  } catch (_error) {
    return "";
  }
}

function isLoopbackContext() {
  return ["127.0.0.1", "localhost", "::1", "[::1]"].includes(window.location.hostname.toLowerCase());
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Request failed");
  return data;
}

function addLink(parent, label, value) {
  const href = safeUrl(value);
  if (!href) return;
  const link = element("a", label);
  link.href = href;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  parent.append(link);
}

function addPreferredAppLinks(parent, app) {
  const localFirst = isLoopbackContext();
  const primary = localFirst ? app.local_url : app.private_url;
  const secondary = localFirst ? app.private_url : app.local_url;
  addLink(parent, "Open ↗", primary || secondary);
  if (primary && secondary && safeUrl(primary) !== safeUrl(secondary)) {
    addLink(parent, localFirst ? "Private ↗" : "Local ↗", secondary);
  }
}

function actionButton(action, appId, label = "") {
  const button = element("button", label || `${action[0].toUpperCase()}${action.slice(1)}`);
  button.type = "button";
  button.dataset.action = action;
  button.dataset.id = appId;
  return button;
}

function renderApps(apps) {
  const root = byId("apps");
  root.replaceChildren();
  if (!apps.length) {
    root.append(element("div", "No apps registered yet. Add a local folder or import a public GitHub repository.", "empty"));
    return;
  }

  apps.forEach((app, index) => {
    const row = element("article", "", "app-row");
    const info = element("div");
    const title = element("div", "", "app-title");
    title.append(element("strong", app.name), element("span", app.state, `badge ${app.state}`));
    info.append(title, element("p", app.description || "No description.", "app-description"));

    const meta = element("div", "", "meta");
    meta.append(
      element("span", app.pid ? `PID ${app.pid}` : "not running"),
      element("span", app.port ? `port ${app.port}` : "no port"),
      element("span", app.health_detail || "health not configured"),
    );
    info.append(meta);

    const links = element("div", "", "links");
    addPreferredAppLinks(links, app);
    info.append(links);

    const logs = element("details", "", "log-details");
    logs.dataset.logs = app.id;
    logs.append(element("summary", "Recent log"), element("pre", "Open to load logs.", "log-output"));
    info.append(logs);

    const controls = element("div", "", "actions lifecycle");
    const up = actionButton("move-up", app.id, "↑");
    const down = actionButton("move-down", app.id, "↓");
    up.className = "order";
    down.className = "order";
    up.title = `Move ${app.name} up`;
    down.title = `Move ${app.name} down`;
    up.disabled = index === 0;
    down.disabled = index === apps.length - 1;
    controls.append(up, down, actionButton("start", app.id), actionButton("stop", app.id), actionButton("restart", app.id));

    row.append(info, controls);
    root.append(row);
  });
}

async function loadApps() {
  try {
    renderApps(await api("/api/apps"));
  } catch (error) {
    byId("apps").replaceChildren(element("div", error.message, "empty"));
  }
}

function renderMetricWidget(widget) {
  const card = element("article", "", `widget widget-${widget.status}`);
  const heading = element("div", "", "widget-heading");
  heading.append(element("h3", widget.title), element("span", widget.status, `badge ${widget.status}`));
  card.append(heading);
  const rows = element("dl", "", "metric-rows");
  const metrics = Array.isArray(widget.metrics) ? widget.metrics : [];
  if (!metrics.length) {
    card.append(element("p", "Data unavailable.", "muted widget-unavailable"));
  } else {
    metrics.forEach((metric) => {
      const row = element("div", "", "metric-row");
      row.append(element("dt", metric.label), element("dd", metric.value));
      rows.append(row);
    });
    card.append(rows);
  }
  return card;
}

function renderProgressWidget(widget) {
  const card = element("article", "", `widget widget-${widget.status}`);
  const heading = element("div", "", "widget-heading");
  heading.append(element("h3", widget.title), element("span", widget.status, `badge ${widget.status}`));
  card.append(heading);
  const items = Array.isArray(widget.progress) ? widget.progress : [];
  if (!items.length) {
    card.append(element("p", "Data unavailable.", "muted widget-unavailable"));
  } else {
    const root = element("div", "", "progress-rows");
    items.forEach((item) => {
      const row = element("div", "", "progress-row");
      const label = element("div", "", "progress-label");
      label.append(element("span", item.label), element("strong", `${Math.round(Number(item.value) * 100)}%`));
      const track = element("div", "", "progress-track");
      const fill = element("div", "", "progress-fill");
      fill.style.width = `${Math.max(0, Math.min(100, Number(item.value) * 100))}%`;
      track.append(fill);
      row.append(label, track);
      if (item.reset_at) row.append(element("span", `Resets ${item.reset_at}`, "progress-reset"));
      root.append(row);
    });
    card.append(root);
  }
  return card;
}

function renderExtensions(payload) {
  const panel = byId("extensionsPanel");
  const root = byId("widgets");
  const error = byId("extensionsError");
  root.replaceChildren();
  error.hidden = !payload.error;
  error.textContent = payload.error || "";
  const widgets = Array.isArray(payload.widgets) ? payload.widgets : [];
  panel.hidden = !payload.enabled && !payload.error;
  widgets.forEach((widget) => {
    const card = widget.type === "progress" ? renderProgressWidget(widget) : renderMetricWidget(widget);
    const href = safeUrl(widget.drill_down_url);
    if (href) {
      card.tabIndex = 0;
      card.setAttribute("role", "link");
      card.setAttribute("aria-label", `Open ${widget.title}`);
      const open = () => window.open(href, "_blank", "noopener,noreferrer");
      card.addEventListener("click", open);
      card.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          open();
        }
      });
    }
    if (widget.timestamp) card.append(element("p", widget.timestamp, "widget-timestamp"));
    root.append(card);
  });
}

async function loadExtensions() {
  try {
    renderExtensions(await api("/api/extensions"));
  } catch (_error) {
    renderExtensions({ enabled: false, widgets: [], error: "Extensions are unavailable." });
  }
}

async function runAction(button) {
  const { action, id } = button.dataset;
  button.disabled = true;
  try {
    if (action === "move-up" || action === "move-down") {
      const direction = action.endsWith("up") ? "up" : "down";
      await api(`/api/apps/${encodeURIComponent(id)}/move/${direction}`, { method: "POST", body: "{}" });
    } else if (["start", "stop", "restart"].includes(action)) {
      if ((action === "start" || action === "restart") && !window.confirm(`${action === "start" ? "Start" : "Restart"} this app? Its manifest command will run with your user permissions.`)) return;
      await api(`/api/apps/${encodeURIComponent(id)}/${action}`, { method: "POST", body: "{}" });
    }
    await loadApps();
  } catch (error) {
    window.alert(error.message);
  } finally {
    button.disabled = false;
  }
}

function showAddDialog() {
  addDialogOpen = true;
  previewRequestId += 1;
  const abandonedStage = previewKind === "github" ? previewState?.staging_id : "";
  previewState = null;
  previewKind = "";
  void cleanupGitHubStage(abandonedStage);
  byId("previewPanel").hidden = true;
  byId("registerButton").hidden = true;
  byId("previewOutput").textContent = "";
  byId("addModal").hidden = false;
  byId("localFolder").focus();
}

async function cleanupGitHubStage(stagingId) {
  if (!stagingId) return;
  try {
    await api("/api/onboarding/github/cleanup", {
      method: "POST",
      body: JSON.stringify({ staging_id: stagingId }),
    });
  } catch (_error) {
    // Staging may already have been moved by a successful registration.
  }
}

async function cleanupGitHubPreview() {
  const stagingId = previewKind === "github" ? previewState?.staging_id : "";
  previewState = null;
  previewKind = "";
  await cleanupGitHubStage(stagingId);
}

function closeAddDialog() {
  addDialogOpen = false;
  previewRequestId += 1;
  byId("addModal").hidden = true;
  void cleanupGitHubPreview();
}

async function previewApp(kind) {
  const isLocal = kind === "local";
  const value = (isLocal ? byId("localFolder") : byId("githubUrl")).value.trim();
  if (!value) {
    window.alert(isLocal ? "Choose a local folder." : "Enter a public GitHub repository URL.");
    return;
  }
  const path = isLocal ? "/api/onboarding/local/preview" : "/api/onboarding/github/preview";
  const payload = isLocal ? { folder: value } : { url: value };
  const requestId = ++previewRequestId;
  try {
    await cleanupGitHubPreview();
    if (!addDialogOpen || requestId !== previewRequestId) return;
    const result = await api(path, { method: "POST", body: JSON.stringify(payload) });
    if (!addDialogOpen || requestId !== previewRequestId) {
      if (kind === "github") await cleanupGitHubStage(result.staging_id);
      return;
    }
    previewState = result;
    previewKind = kind;
    byId("previewOutput").textContent = JSON.stringify(previewState, null, 2);
    byId("previewPanel").hidden = false;
    byId("registerButton").hidden = false;
  } catch (error) {
    if (!addDialogOpen || requestId !== previewRequestId) return;
    byId("previewPanel").hidden = false;
    byId("registerButton").hidden = true;
    byId("previewOutput").textContent = error.message;
  }
}

async function registerPreview() {
  if (!previewState) return;
  const isLocal = previewKind === "local";
  const path = isLocal ? "/api/onboarding/local/register" : "/api/onboarding/github/register";
  const payload = isLocal
    ? { folder: byId("localFolder").value.trim(), confirmation: previewState.digest, preview: previewState }
    : { confirmation: previewState.digest, preview: previewState };
  try {
    const result = await api(path, { method: "POST", body: JSON.stringify(payload) });
    byId("previewOutput").textContent = `${result.id} registered. It remains stopped until you press Start.`;
    byId("registerButton").hidden = true;
    previewState = null;
    previewKind = "";
    await loadApps();
  } catch (error) {
    byId("previewOutput").textContent = error.message;
  }
}

async function loadLogs(details) {
  if (!details.open || details.dataset.loaded === "true") return;
  const output = details.querySelector(".log-output");
  try {
    const result = await api(`/api/apps/${encodeURIComponent(details.dataset.logs)}/logs`);
    output.textContent = result.lines.length ? result.lines.join("\n") : "No log output yet.";
    details.dataset.loaded = "true";
  } catch (error) {
    output.textContent = error.message;
  }
}

const AUTO_UPDATE_CHECK_INTERVAL_MS = 6 * 60 * 60 * 1000;
let lmPending = false;
let drawerWasOpen = false;

function addOption(select, value, label) {
  const option = element("option", label);
  option.value = value;
  select.append(option);
}

function numericInput(name, label, min, max, step = "1") {
  const wrapper = element("label", "", "lm-field");
  wrapper.append(element("span", label));
  const input = document.createElement("input");
  input.name = name; input.type = "number"; input.min = String(min); input.max = String(max); input.step = step;
  wrapper.append(input);
  return wrapper;
}

function selectInput(name, label, options) {
  const wrapper = element("label", "", "lm-field");
  wrapper.append(element("span", label));
  const select = document.createElement("select");
  select.name = name;
  options.forEach(([value, optionLabel]) => addOption(select, value, optionLabel));
  wrapper.append(select);
  return wrapper;
}

function lmFormRequest(form, modelKey) {
  const request = { model: modelKey };
  for (const field of form.elements) {
    if (!field.name || field.value === "" || field.name === "gpu_choice" || field.name === "gpu_ratio") continue;
    request[field.name] = field.type === "number" ? Number(field.value) : field.value;
  }
  const gpuChoice = form.elements.gpu_choice.value;
  const gpuRatio = form.elements.gpu_ratio.value;
  if (gpuChoice) request.gpu = gpuChoice;
  else if (gpuRatio !== "") request.gpu = Number(gpuRatio);
  return request;
}

function lmModelRow(model) {
  const row = element("article", "", "lm-model-row");
  const heading = element("div", "", "lm-model-heading");
  heading.append(element("strong", model.display_name || model.key || "Model"));
  if (model.loaded) heading.append(element("span", "loaded", "badge running"));
  row.append(heading, element("p", model.key || "Model key unavailable", "lm-model-key"));
  const meta = [model.publisher, model.params, model.quantization, model.max_context ? `max context ${model.max_context}` : ""].filter(Boolean);
  if (meta.length) row.append(element("p", meta.join(" · "), "meta"));
  const details = document.createElement("details");
  details.append(element("summary", "Load settings"));
  const form = document.createElement("form");
  form.dataset.lmForm = "true"; form.dataset.model = model.key || ""; form.className = "lm-form";
  form.append(selectInput("gpu_choice", "GPU", [["", "Automatic"], ["off", "Off"], ["max", "Maximum"]]));
  form.append(numericInput("gpu_ratio", "GPU ratio (0–1)", 0, 1, "0.01"));
  form.append(numericInput("context_length", "Context length", 1, 1048576));
  form.append(numericInput("parallel", "Parallel count", 1, 128));
  form.append(numericInput("ttl", "TTL seconds", 1, 604800));
  const identifier = element("label", "", "lm-field"); identifier.append(element("span", "Custom identifier"));
  const identifierInput = document.createElement("input"); identifierInput.name = "identifier"; identifierInput.maxLength = 128; identifier.append(identifierInput); form.append(identifier);
  form.append(selectInput("speculative_draft_mtp", "MTP", [["default", "Default"], ["enable", "Enable"], ["disable", "Disable"]]));
  form.append(selectInput("speculative_draft_simple", "Simple speculative", [["default", "Default"], ["enable", "Enable"], ["disable", "Disable"]]));
  form.append(numericInput("speculative_draft_max_tokens", "Draft max tokens", 1, 512));
  form.append(numericInput("speculative_draft_min_tokens", "Draft min tokens", 0, 512));
  form.append(numericInput("speculative_draft_min_continue_probability", "Draft minimum continue probability", 0, 1, "0.01"));
  const draft = element("label", "", "lm-field"); draft.append(element("span", "Draft model"));
  const draftInput = document.createElement("input"); draftInput.name = "speculative_draft_model"; draftInput.maxLength = 256; draft.append(draftInput); form.append(draft);
  const submit = element("button", "Load", "primary"); submit.type = "submit"; submit.classList.add("lm-action"); form.append(submit);
  details.append(form); row.append(details);
  return row;
}

function lmLoadedRow(instance) {
  const row = element("article", "", "lm-loaded-row");
  const details = element("div", "", "lm-loaded-details");
  details.append(element("strong", instance.identifier || "Unnamed instance"));
  details.append(element("span", instance.display_name || instance.key || "Model unavailable", "lm-model-key"));
  const meta = [instance.status, instance.context_length != null ? `context ${instance.context_length}` : "", instance.parallel != null ? `parallel ${instance.parallel}` : "", instance.ttl_seconds != null ? `TTL ${instance.ttl_seconds}s` : ""].filter(Boolean);
  details.append(element("span", meta.join(" · ") || "Loaded details unavailable", "meta"));
  const unload = element("button", "Unload"); unload.type = "button"; unload.classList.add("lm-action"); unload.dataset.lmUnload = instance.identifier || "";
  row.append(details, unload);
  return row;
}

function setLmPending(value) {
  lmPending = value;
  document.querySelectorAll(".lm-action").forEach((button) => { button.disabled = value; });
}

function renderLM(state) {
  const status = byId("lmStatus"); status.className = "status";
  if (!state.available) { status.classList.add("warning"); status.textContent = state.error || "LM Studio is unavailable. This optional integration is not installed."; }
  else if (state.status === "timeout") { status.classList.add("warning"); status.textContent = "LM Studio status check timed out. Retry when the local application is responsive."; }
  else if (state.error) { status.classList.add("warning"); status.textContent = state.error; }
  else if (state.warning) { status.classList.add("warning"); status.textContent = state.warning; }
  else if (state.status === "empty") status.textContent = "LM Studio is reachable, but no installed models or loaded instances were reported.";
  else status.textContent = state.running ? "LM Studio is reachable. Loaded state is live." : "LM Studio CLI found; start the local application to inspect loaded instances.";
  const loaded = Array.isArray(state.loaded_instances) ? state.loaded_instances : [];
  const models = Array.isArray(state.installed_models) ? state.installed_models : [];
  const loadedRoot = byId("lmLoaded"); const modelsRoot = byId("lmModels");
  loadedRoot.replaceChildren(); modelsRoot.replaceChildren();
  if (loaded.length) loaded.forEach((item) => loadedRoot.append(lmLoadedRow(item))); else loadedRoot.append(element("p", "No loaded instances reported.", "empty"));
  if (models.length) models.forEach((item) => modelsRoot.append(lmModelRow(item))); else modelsRoot.append(element("p", state.available ? "No installed models were reported by lms ls." : "Installed models are unavailable until the optional CLI is installed.", "empty"));
  setLmPending(lmPending);
}

async function loadLM() {
  try { renderLM(await api("/api/lm-studio")); }
  catch (error) { renderLM({ available: false, error: error.message, installed_models: [], loaded_instances: [] }); }
}

async function lmMutation(path, body) {
  if (lmPending) return;
  setLmPending(true); byId("lmStatus").textContent = "Working…";
  try { await api(path, { method: "POST", body: JSON.stringify(body) }); await loadLM(); }
  catch (error) { await loadLM(); byId("lmStatus").className = "status warning"; byId("lmStatus").textContent = error.message; }
  finally { setLmPending(false); }
}

function setUpdateAvailability(release) {
  const available = Boolean(release && release.update_available);
  byId("updatesBadge").hidden = !available; byId("updateBanner").hidden = !available;
  byId("updateButton").hidden = !available;
  if (available) { byId("updateBannerText").textContent = ` AppDock ${release.version} is available.`; byId("updateButton").hidden = false; }
}

async function checkUpdates({ automatic = false } = {}) {
  const output = byId("updateResult");
  if (!automatic) {
    output.textContent = "Checking GitHub Releases…";
    byId("updateButton").hidden = true;
    byId("releaseNotes").textContent = "";
  }
  try {
    const release = await api("/api/updates/check");
    verifiedRelease = release;
    setUpdateAvailability(release);
    if (!automatic) {
      output.textContent = release.update_available
        ? `AppDock ${release.version} is available. You are running ${release.current}.`
        : `AppDock ${release.current} is current.`;
      byId("releaseNotes").textContent = release.notes || "No release notes provided.";
      byId("updateButton").hidden = !release.update_available;
    }
    return release;
  } catch (error) {
    if (!automatic) output.textContent = `Could not check GitHub Releases. ${error.message} Try again from this page.`;
    return null;
  }
}

async function waitForHealthyVersion(expectedVersion) {
  const deadline = Date.now() + 45_000;
  while (Date.now() < deadline) {
    try {
      const response = await fetch("/health", { cache: "no-store", headers: { Accept: "application/json" } });
      if (response.ok) {
        const health = await response.json();
        if (health && health.ok === true && health.version === expectedVersion) return true;
      }
    } catch (_error) { /* restart window */ }
    await new Promise((resolve) => window.setTimeout(resolve, 500));
  }
  return false;
}

async function applyVerifiedUpdate() {
  if (!verifiedRelease || !window.confirm(`Update AppDock to ${verifiedRelease.version}? AppDock will restart after verification.`)) return;
  const output = byId("updateResult");
  byId("updateButton").disabled = true;
  try {
    output.textContent = "Downloading and verifying the release…";
    const staged = await api("/api/updates/stage", {
      method: "POST",
      body: JSON.stringify({ confirmation: verifiedRelease.confirmation_digest }),
    });
    output.textContent = "Verified. Applying the update and waiting for the expected version…";
    const applied = await api("/api/updates/apply", {
      method: "POST",
      body: JSON.stringify({ confirmation: staged.confirmation_digest }),
    });
    const healthy = await waitForHealthyVersion(applied.version || verifiedRelease.version);
    if (healthy) {
      output.textContent = `AppDock ${applied.version || verifiedRelease.version} is healthy. Reloading…`;
      window.location.reload();
    } else {
      output.textContent = "AppDock did not report the expected version healthy before the timeout. Check the local update log and restart AppDock manually; success was not confirmed.";
      byId("updateButton").disabled = false;
    }
  } catch (error) {
    output.textContent = error.message;
    byId("updateButton").disabled = false;
  }
}

byId("apps").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-id]");
  if (button) runAction(button);
});
byId("apps").addEventListener("toggle", (event) => {
  if (event.target.matches("details[data-logs]")) loadLogs(event.target);
}, true);
byId("addButton").addEventListener("click", showAddDialog);
byId("closeAddButton").addEventListener("click", closeAddDialog);
byId("refreshButton").addEventListener("click", loadApps);
byId("previewLocalButton").addEventListener("click", () => previewApp("local"));
byId("previewGithubButton").addEventListener("click", () => previewApp("github"));
byId("registerButton").addEventListener("click", registerPreview);
byId("checkUpdateButton").addEventListener("click", checkUpdates);
byId("updateButton").addEventListener("click", applyVerifiedUpdate);
byId("lmRefreshButton").addEventListener("click", loadLM);
byId("lmModels").addEventListener("submit", (event) => {
  if (!event.target.matches("form[data-lm-form]")) return;
  event.preventDefault();
  lmMutation("/api/lm-studio/load", lmFormRequest(event.target, event.target.dataset.model));
});
byId("lmLoaded").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-lm-unload]");
  if (button) lmMutation("/api/lm-studio/unload", { identifier: button.dataset.lmUnload });
});
function closeDrawer() {
  const drawer = byId("drawer");
  drawer.classList.remove("open"); drawer.setAttribute("aria-hidden", "true"); drawer.inert = true;
  byId("drawerBackdrop").hidden = true; byId("menuButton").setAttribute("aria-expanded", "false");
  if (drawerWasOpen) byId("menuButton").focus();
  drawerWasOpen = false;
}
function openDrawer() {
  const drawer = byId("drawer");
  drawerWasOpen = true; drawer.inert = false; drawer.classList.add("open"); drawer.setAttribute("aria-hidden", "false");
  byId("drawerBackdrop").hidden = false; byId("menuButton").setAttribute("aria-expanded", "true");
  byId("dashboardLink").focus();
}
function showView(view) {
  document.querySelectorAll(".view").forEach((section) => { section.hidden = section.dataset.view !== view; });
  document.querySelectorAll(".drawer-nav button").forEach((button) => { button.setAttribute("aria-current", button.dataset.view === view ? "page" : "false"); });
  closeDrawer();
  if (view === "lm-studio") loadLM();
}
byId("menuButton").addEventListener("click", () => byId("drawer").classList.contains("open") ? closeDrawer() : openDrawer());
byId("closeDrawerButton").addEventListener("click", closeDrawer);
byId("drawerBackdrop").addEventListener("click", closeDrawer);
document.querySelectorAll(".drawer-nav button").forEach((button) => button.addEventListener("click", () => showView(button.dataset.view)));
byId("bannerUpdatesButton").addEventListener("click", () => showView("updates"));
byId("addModal").addEventListener("click", (event) => { if (event.target === byId("addModal")) closeAddDialog(); });
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  if (!byId("addModal").hidden) closeAddDialog();
  else if (byId("drawer").classList.contains("open")) closeDrawer();
});

loadApps();
loadExtensions();
loadLM();
window.setTimeout(() => { checkUpdates({ automatic: true }); }, 0);
window.setInterval(loadApps, 5000);
window.setInterval(loadExtensions, 5000);
window.setInterval(() => { checkUpdates({ automatic: true }); }, AUTO_UPDATE_CHECK_INTERVAL_MS);
