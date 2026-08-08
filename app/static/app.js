/* dl4tv UI — vanilla JS, no build step. */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = {
  status: null,
  settings: null,
  mappings: [],
  openPlaylists: new Set(),
  logSeq: 0,
  downloadRoot: "",
};

// --------------------------------------------------------------------------
// helpers
// --------------------------------------------------------------------------

async function api(path, options = {}) {
  const opts = { headers: {}, ...options };
  if (opts.body !== undefined && typeof opts.body !== "string") {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.body);
  }
  const response = await fetch(path, opts);
  const text = await response.text();
  let payload = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    payload = text;
  }
  if (!response.ok) {
    const detail = (payload && payload.detail) || response.statusText;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return payload;
}

function toast(message, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  $("#toasts").append(el);
  setTimeout(() => el.remove(), kind === "err" ? 9000 : 4500);
}

const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );

function relTime(iso) {
  if (!iso) return "never";
  const delta = (new Date(iso).getTime() - Date.now()) / 1000;
  const abs = Math.abs(delta);
  const units = [
    ["week", 604800],
    ["day", 86400],
    ["hour", 3600],
    ["minute", 60],
    ["second", 1],
  ];
  for (const [name, size] of units) {
    if (abs >= size || name === "second") {
      const value = Math.max(1, Math.round(abs / size));
      const label = `${value} ${name}${value === 1 ? "" : "s"}`;
      return delta > 0 ? `in ${label}` : `${label} ago`;
    }
  }
  return "just now";
}

function bytes(size) {
  if (!size) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = size;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i += 1;
  }
  return `${value.toFixed(value < 10 && i > 0 ? 1 : 0)} ${units[i]}`;
}

function duration(seconds) {
  if (!seconds) return "";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  return h
    ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`
    : `${m}:${String(s).padStart(2, "0")}`;
}

// --------------------------------------------------------------------------
// navigation
// --------------------------------------------------------------------------

$$("nav button").forEach((button) => {
  button.addEventListener("click", () => {
    $$("nav button").forEach((b) => b.classList.toggle("active", b === button));
    $$(".view").forEach((v) =>
      v.classList.toggle("active", v.id === `view-${button.dataset.view}`)
    );
    if (button.dataset.view === "playlists") loadMappings();
    if (button.dataset.view === "settings") loadSettings();
  });
});

// --------------------------------------------------------------------------
// status / dashboard
// --------------------------------------------------------------------------

async function refreshStatus() {
  let status;
  try {
    status = await api("/api/status");
  } catch (err) {
    $("#status-line").textContent = `Cannot reach dl4tv: ${err.message}`;
    return;
  }
  state.status = status;
  state.downloadRoot = status.download_dir;
  $("#download-root").textContent = status.download_dir;

  const auth = status.auth;
  const pill = $("#auth-pill");
  const usable = auth.connected || auth.has_api_key || auth.effective_source === "yt-dlp";
  pill.className = `pill ${auth.connected ? "ok" : usable ? "warn" : "err"}`;
  pill.lastElementChild.textContent = auth.connected
    ? `Connected${auth.channel ? `: ${auth.channel}` : ""}`
    : auth.has_api_key
    ? "API key"
    : auth.effective_source === "yt-dlp"
    ? "yt-dlp (public playlists)"
    : "No credentials";

  const counts = status.playlists.reduce(
    (acc, p) => {
      acc.downloaded += p.counts.downloaded;
      acc.failed += p.counts.permanent;
      return acc;
    },
    { downloaded: 0, failed: 0 }
  );
  $("#status-line").innerHTML = status.running
    ? "Sync running…"
    : `${status.playlists.length} playlist(s) · ${counts.downloaded} downloaded · ` +
      `${counts.failed} needing attention`;

  $("#next-run").textContent = status.schedule.enabled
    ? status.next_run_at
      ? `${new Date(status.next_run_at).toLocaleString()} (${relTime(status.next_run_at)})`
      : "pending"
    : "scheduling disabled";

  $("#sync-all").disabled = status.running;
  $("#cancel-sync").style.display = status.running ? "" : "none";

  const progress = status.progress || {};
  const wrap = $("#progress-wrap");
  if (status.running && progress.video_title) {
    wrap.style.display = "";
    $("#progress-text").textContent =
      `[${progress.index}/${progress.total}] ${progress.mapping_title} — ${progress.video_title}`;
    $("#progress-pct").textContent = `${progress.percent ?? 0}%`;
    $("#progress-bar").value = progress.percent ?? 0;
  } else {
    wrap.style.display = "none";
  }

  renderPlaylistCards(status.playlists);
  renderRuns(status.runs);
}

function statusPill(playlist) {
  const map = {
    ok: ["ok", "ok"],
    partial: ["warn", "partial"],
    error: ["err", "error"],
    never: ["", "never run"],
  };
  const [cls, label] = map[playlist.last_status] || ["", playlist.last_status];
  return `<span class="pill ${cls}"><span class="dot"></span>${esc(label)}</span>`;
}

function renderPlaylistCards(playlists) {
  const container = $("#playlist-list");
  if (!playlists.length) {
    container.innerHTML =
      '<div class="empty">No playlists mapped yet — add one on the Playlists tab.</div>';
    return;
  }
  container.innerHTML = playlists
    .map(
      (p) => `
    <div class="playlist ${state.openPlaylists.has(p.id) ? "open" : ""}" data-id="${p.id}">
      <div class="playlist-head">
        <span class="title">${esc(p.title)}</span>
        ${statusPill(p)}
        ${p.enabled ? "" : '<span class="pill">disabled</span>'}
        <span class="muted mono">${esc(p.folder)}</span>
        <div class="spacer" style="flex:1"></div>
        <span class="muted">${p.counts.downloaded} downloaded${
        p.counts.permanent ? ` · ${p.counts.permanent} blocked` : ""
      } · last sync ${relTime(p.last_sync_at)}</span>
        <button class="btn small secondary" data-act="toggle">${
          state.openPlaylists.has(p.id) ? "Hide" : "Details"
        }</button>
        <button class="btn small" data-act="sync">Sync</button>
      </div>
      <div class="playlist-body" data-body="${p.id}">
        ${p.last_error ? `<div class="pill err" style="margin-bottom:.5rem">${esc(p.last_error)}</div>` : ""}
        <div class="muted">Loading…</div>
      </div>
    </div>`
    )
    .join("");

  $$("#playlist-list .playlist").forEach((card) => {
    const id = card.dataset.id;
    $('[data-act="toggle"]', card).addEventListener("click", () => {
      if (state.openPlaylists.has(id)) {
        state.openPlaylists.delete(id);
        card.classList.remove("open");
      } else {
        state.openPlaylists.add(id);
        card.classList.add("open");
        loadVideos(id);
      }
    });
    $('[data-act="sync"]', card).addEventListener("click", async () => {
      try {
        await api(`/api/mappings/${id}/sync`, { method: "POST" });
        toast("Sync started", "ok");
        refreshStatus();
      } catch (err) {
        toast(err.message, "err");
      }
    });
    if (state.openPlaylists.has(id)) loadVideos(id);
  });
}

async function loadVideos(mappingId) {
  const body = $(`[data-body="${mappingId}"]`);
  if (!body) return;
  let data;
  try {
    data = await api(`/api/mappings/${mappingId}/videos`);
  } catch (err) {
    body.innerHTML = `<div class="pill err">${esc(err.message)}</div>`;
    return;
  }
  if (!data.videos.length) {
    body.innerHTML = '<div class="empty">Nothing downloaded yet.</div>';
    return;
  }
  const rows = data.videos
    .map((v) => {
      const kind =
        v.status === "downloaded" ? "ok" : v.status === "skipped" ? "" : "err";
      const detail =
        v.status === "downloaded"
          ? `<span class="mono muted">${esc(v.path || "")}</span> ${bytes(v.filesize)}`
          : v.status === "skipped"
          ? esc(v.reason || "skipped")
          : `${esc(v.error || "failed")}${
              v.error_kind ? ` <span class="pill err">${esc(v.error_kind)}</span>` : ""
            }${v.permanent ? ' <span class="pill">will not retry</span>' : ""}`;
      return `<tr>
        <td><a href="https://www.youtube.com/watch?v=${esc(v.video_id)}" target="_blank"
          rel="noreferrer">${esc(v.title || v.video_id)}</a>
          <div class="muted">${duration(v.duration)}</div></td>
        <td><span class="pill ${kind}">${esc(v.status)}</span></td>
        <td>${detail}</td>
        <td class="actions">
          ${
            v.status !== "downloaded"
              ? `<button class="btn small secondary" data-retry="${esc(v.video_id)}">Retry</button>`
              : ""
          }
          <button class="btn small danger" data-forget="${esc(v.video_id)}">Forget</button>
        </td>
      </tr>`;
    })
    .join("");
  body.innerHTML = `<table><thead><tr><th>Video</th><th>Status</th><th>Detail</th><th></th></tr></thead>
    <tbody>${rows}</tbody></table>`;

  $$("[data-retry]", body).forEach((button) =>
    button.addEventListener("click", async () => {
      await api(
        `/api/mappings/${mappingId}/videos/${button.dataset.retry}/retry`,
        { method: "POST" }
      );
      toast("Queued for the next sync", "ok");
      loadVideos(mappingId);
    })
  );
  $$("[data-forget]", body).forEach((button) =>
    button.addEventListener("click", async () => {
      await api(`/api/mappings/${mappingId}/videos/${button.dataset.forget}`, {
        method: "DELETE",
      });
      loadVideos(mappingId);
    })
  );
}

function renderRuns(runs) {
  const container = $("#run-list");
  if (!runs || !runs.length) {
    container.innerHTML = '<div class="empty">Nothing has run yet.</div>';
    return;
  }
  container.innerHTML = `<table><thead><tr><th>Started</th><th>Trigger</th><th>Result</th>
    <th>Downloaded</th><th>Failed</th><th>Skipped</th></tr></thead><tbody>
    ${runs
      .map(
        (r) => `<tr>
      <td>${new Date(r.started_at).toLocaleString()}<div class="muted">${relTime(
          r.started_at
        )}</div></td>
      <td>${esc(r.trigger)}</td>
      <td><span class="pill ${
        r.status === "ok" ? "ok" : r.status === "running" ? "" : r.status === "error" ? "err" : "warn"
      }">${esc(r.status)}</span>${r.error ? `<div class="muted">${esc(r.error)}</div>` : ""}</td>
      <td>${r.downloaded}</td><td>${r.failed}</td><td>${r.skipped}</td>
    </tr>`
      )
      .join("")}</tbody></table>`;
}

$("#sync-all").addEventListener("click", async () => {
  try {
    await api("/api/sync", { method: "POST", body: {} });
    toast("Sync started", "ok");
    refreshStatus();
  } catch (err) {
    toast(err.message, "err");
  }
});

$("#cancel-sync").addEventListener("click", async () => {
  await api("/api/sync/cancel", { method: "POST" });
  toast("Cancelling after the current video…");
});

// --------------------------------------------------------------------------
// playlists tab
// --------------------------------------------------------------------------

async function loadMappings() {
  const data = await api("/api/mappings");
  state.mappings = data.mappings;
  $("#download-root").textContent = data.download_dir;
  const container = $("#mapping-list");
  if (!data.mappings.length) {
    container.innerHTML = '<div class="empty">Nothing mapped yet.</div>';
    return;
  }
  container.innerHTML = data.mappings
    .map(
      (m) => `<div class="playlist" data-id="${m.id}">
      <div class="playlist-head">
        <span class="title">${esc(m.title)}</span>
        <span class="muted mono">${esc(m.playlist_id)}</span>
        <div style="flex:1"></div>
        <span class="check"><input type="checkbox" data-enabled ${
          m.enabled ? "checked" : ""
        }><label>Enabled</label></span>
        <button class="btn small secondary" data-edit>Edit</button>
        <button class="btn small danger" data-delete>Remove</button>
      </div>
      <div class="playlist-body">
        <div class="grid">
          <div><label>Folder</label><input type="text" data-folder value="${esc(m.folder)}"></div>
          <div><label>Format override</label><input type="text" data-format value="${esc(
            m.format || ""
          )}" placeholder="inherit from settings"></div>
          <div><label>Output template override</label><input type="text" data-template value="${esc(
            m.output_template || ""
          )}" placeholder="inherit from settings"></div>
          <div><label>Max new per run</label><input type="number" data-maxnew value="${
            m.max_new_per_run ?? ""
          }"></div>
          <div><label>Minimum duration (seconds)</label><input type="number" data-mindur value="${
            m.min_duration_seconds ?? ""
          }" placeholder="e.g. 61 to skip Shorts"></div>
          <div><label>Maximum duration (seconds)</label><input type="number" data-maxdur value="${
            m.max_duration_seconds ?? ""
          }"></div>
        </div>
        <div class="row" style="margin-top:.75rem">
          <span class="check"><input type="checkbox" data-nfo ${
            m.write_nfo ? "checked" : ""
          }><label>Write .nfo sidecar</label></span>
          <select data-nfokind style="width:auto">
            ${["movie", "musicvideo", "episodedetails"]
              .map(
                (k) =>
                  `<option value="${k}" ${m.nfo_kind === k ? "selected" : ""}>${k}</option>`
              )
              .join("")}
          </select>
          <div style="flex:1"></div>
          <button class="btn small" data-save>Save changes</button>
        </div>
      </div>
    </div>`
    )
    .join("");

  $$("#mapping-list .playlist").forEach((card) => {
    const id = card.dataset.id;
    $("[data-edit]", card).addEventListener("click", () => card.classList.toggle("open"));
    $("[data-enabled]", card).addEventListener("change", async (event) => {
      await patchMapping(id, { enabled: event.target.checked });
      refreshStatus();
    });
    $("[data-delete]", card).addEventListener("click", async () => {
      if (!confirm("Remove this mapping? Downloaded files are left on disk.")) return;
      await api(`/api/mappings/${id}`, { method: "DELETE" });
      toast("Mapping removed", "ok");
      loadMappings();
      refreshStatus();
    });
    $("[data-save]", card).addEventListener("click", async () => {
      const num = (sel) => {
        const value = $(sel, card).value.trim();
        return value === "" ? null : Number(value);
      };
      try {
        await patchMapping(id, {
          folder: $("[data-folder]", card).value.trim(),
          format: $("[data-format]", card).value.trim() || null,
          output_template: $("[data-template]", card).value.trim() || null,
          max_new_per_run: num("[data-maxnew]"),
          min_duration_seconds: num("[data-mindur]"),
          max_duration_seconds: num("[data-maxdur]"),
          write_nfo: $("[data-nfo]", card).checked,
          nfo_kind: $("[data-nfokind]", card).value,
        });
        toast("Saved", "ok");
        loadMappings();
        refreshStatus();
      } catch (err) {
        toast(err.message, "err");
      }
    });
  });
}

function patchMapping(id, body) {
  return api(`/api/mappings/${id}`, { method: "PATCH", body });
}

$("#add-by-url").addEventListener("click", async () => {
  const query = $("#add-query").value.trim();
  const folder = $("#add-folder").value.trim();
  if (!query || !folder) {
    toast("A playlist link and a folder are both required", "err");
    return;
  }
  try {
    const mapping = await api("/api/mappings", {
      method: "POST",
      body: { query, folder },
    });
    toast(`Added ${mapping.title}`, "ok");
    $("#add-query").value = "";
    $("#add-folder").value = "";
    loadMappings();
    refreshStatus();
  } catch (err) {
    toast(err.message, "err");
  }
});

$("#load-playlists").addEventListener("click", async () => {
  const container = $("#yt-playlists");
  container.innerHTML = '<div class="muted">Loading…</div>';
  let data;
  try {
    data = await api("/api/youtube/playlists");
  } catch (err) {
    container.innerHTML = `<div class="pill err">${esc(err.message)}</div>`;
    return;
  }
  if (!data.playlists.length) {
    container.innerHTML = '<div class="empty">No playlists found on this account.</div>';
    return;
  }
  container.innerHTML = `<table><thead><tr><th>Playlist</th><th>Items</th><th>Visibility</th>
    <th>Folder</th><th></th></tr></thead><tbody>
    ${data.playlists
      .map(
        (p) => `<tr data-playlist="${esc(p.id)}">
      <td>${esc(p.title)}<div class="muted mono">${esc(p.id)}</div></td>
      <td>${p.item_count ?? "—"}</td>
      <td>${esc(p.privacy || p.kind)}</td>
      <td><input type="text" data-folder placeholder="folder name"></td>
      <td class="actions"><button class="btn small" data-add>Map</button></td>
    </tr>`
      )
      .join("")}</tbody></table>`;

  $$("[data-add]", container).forEach((button) =>
    button.addEventListener("click", async () => {
      const row = button.closest("tr");
      const folder = $("[data-folder]", row).value.trim();
      if (!folder) {
        toast("Enter a folder for this playlist", "err");
        return;
      }
      try {
        await api("/api/mappings", {
          method: "POST",
          body: {
            playlist_id: row.dataset.playlist,
            title: row.cells[0].childNodes[0].textContent.trim(),
            folder,
          },
        });
        toast("Playlist mapped", "ok");
        loadMappings();
        refreshStatus();
      } catch (err) {
        toast(err.message, "err");
      }
    })
  );
});

// --------------------------------------------------------------------------
// settings tab
// --------------------------------------------------------------------------

async function loadSettings() {
  const settings = await api("/api/settings");
  state.settings = settings;
  const { schedule, downloads, youtube } = settings;

  $("#sched-enabled").checked = schedule.enabled;
  $("#sched-mode").value = schedule.mode;
  $("#sched-time").value = schedule.daily_at;
  $("#sched-tz").value = schedule.timezone;
  $("#sched-interval").value = schedule.interval_minutes;
  $("#sched-onstart").checked = schedule.run_on_start;

  $("#dl-format").value = downloads.format;
  $("#dl-container").value = downloads.merge_output_format;
  $("#dl-template").value = downloads.output_template;
  $("#dl-cookies").value = downloads.cookies_file || "";
  $("#dl-rate").value = downloads.rate_limit || "";
  $("#dl-subs").value = downloads.subtitle_languages;
  $("#dl-attempts").value = downloads.max_attempts;
  $("#dl-max-new").value = downloads.max_new_per_run ?? "";
  $("#dl-sponsorblock").value = (downloads.sponsorblock_remove || []).join(",");
  $("#dl-metadata").checked = downloads.embed_metadata;
  $("#dl-thumb").checked = downloads.embed_thumbnail;
  $("#dl-writethumb").checked = downloads.write_thumbnail;
  $("#dl-writesubs").checked = downloads.write_subtitles;
  $("#dl-embedsubs").checked = downloads.embed_subtitles;

  $("#yt-source").value = youtube.source || "auto";
  $("#client-id").value = youtube.client_id || "";
  $("#client-secret").value = youtube.client_secret || "";
  $("#api-key").value = youtube.api_key || "";

  const auth = await api("/api/auth/status");
  $("#redirect-uri").textContent = auth.redirect_uri;
  $("#source-hint").textContent =
    auth.effective_source === "yt-dlp"
      ? "Currently reading playlists with yt-dlp — no Google credentials in use."
      : "Currently reading playlists with the YouTube Data API.";
  $("#auth-details").textContent = auth.connected
    ? `Connected${auth.channel ? ` as ${auth.channel}` : ""}.`
    : auth.has_client
    ? "OAuth client saved — click Connect to authorize."
    : "Add an OAuth client id and secret, save, then connect.";
  $("#connect").disabled = !auth.has_client;
  $("#disconnect").disabled = !auth.token_present;
}

function collectSettings() {
  const number = (sel) => {
    const value = $(sel).value.trim();
    return value === "" ? null : Number(value);
  };
  return {
    schedule: {
      enabled: $("#sched-enabled").checked,
      mode: $("#sched-mode").value,
      daily_at: $("#sched-time").value.trim() || "03:00",
      timezone: $("#sched-tz").value.trim() || "UTC",
      interval_minutes: Number($("#sched-interval").value || 360),
      run_on_start: $("#sched-onstart").checked,
    },
    downloads: {
      ...state.settings.downloads,
      format: $("#dl-format").value.trim(),
      merge_output_format: $("#dl-container").value.trim(),
      output_template: $("#dl-template").value.trim(),
      cookies_file: $("#dl-cookies").value.trim() || null,
      rate_limit: $("#dl-rate").value.trim() || null,
      subtitle_languages: $("#dl-subs").value.trim() || "en",
      max_attempts: Number($("#dl-attempts").value || 3),
      max_new_per_run: number("#dl-max-new"),
      sponsorblock_remove: $("#dl-sponsorblock")
        .value.split(",")
        .map((s) => s.trim())
        .filter(Boolean),
      embed_metadata: $("#dl-metadata").checked,
      embed_thumbnail: $("#dl-thumb").checked,
      write_thumbnail: $("#dl-writethumb").checked,
      write_subtitles: $("#dl-writesubs").checked,
      embed_subtitles: $("#dl-embedsubs").checked,
    },
    youtube: {
      source: $("#yt-source").value,
      client_id: $("#client-id").value.trim() || null,
      client_secret: $("#client-secret").value || null,
      api_key: $("#api-key").value || null,
    },
  };
}

async function saveSettings(message = "Settings saved") {
  try {
    await api("/api/settings", { method: "PUT", body: collectSettings() });
    $("#settings-saved").textContent = message;
    setTimeout(() => ($("#settings-saved").textContent = ""), 3000);
    await loadSettings();
    refreshStatus();
    return true;
  } catch (err) {
    toast(err.message, "err");
    return false;
  }
}

$("#save-settings").addEventListener("click", () => saveSettings());
$("#save-credentials").addEventListener("click", () => saveSettings("Credentials saved"));

$("#connect").addEventListener("click", async () => {
  if (await saveSettings("Credentials saved")) {
    window.location.href = "/auth/start";
  }
});

$("#disconnect").addEventListener("click", async () => {
  await api("/api/auth/disconnect", { method: "POST" });
  toast("Disconnected", "ok");
  loadSettings();
  refreshStatus();
});

// --------------------------------------------------------------------------
// logs
// --------------------------------------------------------------------------

async function pollLogs() {
  try {
    const data = await api(`/api/logs?since=${state.logSeq}`);
    if (data.logs.length) {
      const box = $("#logs");
      const follow = $("#log-follow").checked;
      for (const entry of data.logs) {
        state.logSeq = Math.max(state.logSeq, entry.seq);
        const line = document.createElement("div");
        line.className = entry.level;
        line.innerHTML = `<span class="ts">${new Date(entry.ts).toLocaleTimeString()}</span> ` +
          `${esc(entry.level.padEnd(7))} ${esc(entry.message)}`;
        box.append(line);
      }
      while (box.childElementCount > 800) box.firstElementChild.remove();
      if (follow) box.scrollTop = box.scrollHeight;
    }
  } catch {
    /* transient; the next poll will catch up */
  }
}

// --------------------------------------------------------------------------
// boot
// --------------------------------------------------------------------------

async function tick() {
  await refreshStatus();
  await pollLogs();
  setTimeout(tick, state.status && state.status.running ? 1500 : 5000);
}

loadMappings().catch(() => {});
tick();
