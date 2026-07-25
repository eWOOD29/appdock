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

async function checkUpdates() {
  const output = byId("updateResult");
  output.textContent = "Checking GitHub releases…";
  byId("updateButton").hidden = true;
  byId("releaseNotes").textContent = "";
  try {
    verifiedRelease = await api("/api/updates/check");
    output.textContent = verifiedRelease.update_available
      ? `AppDock ${verifiedRelease.version} is available. You are running ${verifiedRelease.current}.`
      : `AppDock ${verifiedRelease.current} is current.`;
    byId("releaseNotes").textContent = verifiedRelease.notes || "";
    byId("updateButton").hidden = !verifiedRelease.update_available;
  } catch (error) {
    output.textContent = error.message;
  }
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
    output.textContent = "Verified. Restarting AppDock…";
    await api("/api/updates/apply", {
      method: "POST",
      body: JSON.stringify({ confirmation: staged.confirmation_digest }),
    });
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
byId("settingsButton").addEventListener("click", () => { byId("updatesPanel").hidden = !byId("updatesPanel").hidden; });
byId("previewLocalButton").addEventListener("click", () => previewApp("local"));
byId("previewGithubButton").addEventListener("click", () => previewApp("github"));
byId("registerButton").addEventListener("click", registerPreview);
byId("checkUpdateButton").addEventListener("click", checkUpdates);
byId("updateButton").addEventListener("click", applyVerifiedUpdate);
byId("addModal").addEventListener("click", (event) => { if (event.target === byId("addModal")) closeAddDialog(); });
document.addEventListener("keydown", (event) => { if (event.key === "Escape" && !byId("addModal").hidden) closeAddDialog(); });

loadApps();
loadExtensions();
window.setInterval(loadApps, 5000);
window.setInterval(loadExtensions, 5000);
