/* dl4tv UI — vanilla JS, no build step. */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = {
  status: null,
  settings: null,
  mappings: [],
  openPlaylists: new Set(),
  formatPresets: [],
  // Last-rendered signature per playlist, so an unchanged poll touches no DOM.
  playlistSignatures: new Map(),
  runsSignature: null,
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

/** Write only when the value actually differs — an identical assignment still
 * replaces the text node, which shows up as DOM churn. */
function setText(el, value) {
  if (el && el.textContent !== value) el.textContent = value;
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
// folder path autocomplete
// --------------------------------------------------------------------------

let autocompleteSeq = 0;

/**
 * Suggest existing sub-folders as the user types a path, and say plainly
 * whether the folder is there yet — it gets created on the next sync.
 */
function attachFolderAutocomplete(input) {
  if (!input || input.dataset.autocomplete) return;
  input.dataset.autocomplete = "1";
  input.setAttribute("autocomplete", "off");

  const listId = `folder-options-${++autocompleteSeq}`;
  const datalist = document.createElement("datalist");
  datalist.id = listId;
  input.setAttribute("list", listId);
  input.after(datalist);

  const hint = () => {
    const holder = input.closest("div");
    return holder ? holder.querySelector("[data-folder-hint]") : null;
  };

  const refresh = async () => {
    const value = input.value;
    // Absolute paths live outside the download root; nothing to suggest.
    if (value.startsWith("/")) {
      datalist.innerHTML = "";
      const el = hint();
      if (el) el.textContent = "Absolute path — used as-is.";
      return;
    }
    const cut = value.lastIndexOf("/");
    const parent = cut >= 0 ? value.slice(0, cut) : "";
    const leaf = (cut >= 0 ? value.slice(cut + 1) : value).toLowerCase();

    let data;
    try {
      data = await api(`/api/folders?path=${encodeURIComponent(parent)}`);
    } catch {
      datalist.innerHTML = "";
      return;
    }
    datalist.innerHTML = data.folders
      .filter((f) => f.name.toLowerCase().startsWith(leaf))
      .slice(0, 25)
      .map((f) => `<option value="${esc(f.path)}"></option>`)
      .join("");

    const el = hint();
    if (!el) return;
    if (!value.trim()) {
      el.textContent = "";
    } else if (!leaf) {
      // Trailing slash: the path itself is the folder we just listed.
      el.textContent = data.exists
        ? "Folder exists."
        : "Does not exist yet — created on the next sync.";
    } else if (data.folders.some((f) => f.name.toLowerCase() === leaf)) {
      el.textContent = "Folder exists.";
    } else {
      el.textContent = "Does not exist yet — created on the next sync.";
    }
  };

  let timer;
  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(refresh, 200);
  });
  input.addEventListener("focus", refresh);
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
    if (button.dataset.view === "settings") {
      loadSettings();
      refreshAccess();
    }
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
    if (/locked/i.test(err.message)) {
      window.location.href = "/login";
      return;
    }
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
  $("#status-line").innerHTML = status.cancelling
    ? "Stopping the sync…"
    : status.running
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
  $("#cancel-sync").disabled = status.cancelling;
  $("#cancel-sync").textContent = status.cancelling ? "Stopping…" : "Stop sync";
  $("#stop-sync").disabled = status.cancelling;
  $("#stop-sync").textContent = status.cancelling ? "Stopping…" : "Stop sync";

  const progress = status.progress || {};
  const wrap = $("#progress-wrap");
  if (status.running) {
    wrap.style.display = "";
    $("#progress-text").textContent = progress.video_title
      ? `[${progress.index}/${progress.total}] ${progress.mapping_title} — ${progress.video_title}`
      : "Looking for new videos…";
    $("#progress-pct").textContent = progress.video_title
      ? `${progress.percent ?? 0}%`
      : "";
    $("#progress-bar").value = progress.percent ?? 0;
  } else {
    wrap.style.display = "none";
  }

  renderPlaylistCards(status.playlists);
  renderRuns(status.runs);
}

const STATUS_PILL = {
  ok: ["ok", "ok"],
  partial: ["warn", "partial"],
  error: ["err", "error"],
  never: ["", "never run"],
};

/**
 * Everything the summary row displays. The card is only touched when this
 * changes, so a poll that brings no news leaves the DOM — and the page scroll —
 * completely alone.
 */
function playlistSignature(p) {
  return JSON.stringify([
    p.title,
    p.enabled,
    p.folder,
    p.last_status,
    p.last_error,
    p.last_sync_at,
    p.counts,
  ]);
}

function createPlaylistCard(id) {
  const card = document.createElement("div");
  card.className = "playlist";
  card.dataset.id = id;
  card.innerHTML = `
    <div class="playlist-head">
      <span class="title"></span>
      <span class="pill" data-status><span class="dot"></span><span data-status-label></span></span>
      <span class="pill" data-disabled style="display:none">disabled</span>
      <span class="muted mono" data-folder></span>
      <div style="flex:1"></div>
      <span class="muted" data-summary></span>
      <button class="btn small secondary" data-act="toggle">Details</button>
      <button class="btn small" data-act="sync">Sync</button>
    </div>
    <div class="playlist-body">
      <div class="pill err" data-error style="display:none;margin-bottom:.5rem"></div>
      <div data-videos><div class="muted">Loading…</div></div>
    </div>`;

  // Wired once, when the card is created — not on every refresh.
  $('[data-act="toggle"]', card).addEventListener("click", () => {
    if (state.openPlaylists.has(id)) {
      state.openPlaylists.delete(id);
      card.classList.remove("open");
      $('[data-act="toggle"]', card).textContent = "Details";
    } else {
      state.openPlaylists.add(id);
      card.classList.add("open");
      $('[data-act="toggle"]', card).textContent = "Hide";
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
  return card;
}

function updatePlaylistCard(card, p) {
  setText($(".title", card), p.title);

  const [cls, label] = STATUS_PILL[p.last_status] || ["", p.last_status];
  const pill = $("[data-status]", card);
  const pillClass = `pill ${cls}`;
  if (pill.className !== pillClass) pill.className = pillClass;
  setText($("[data-status-label]", card), label);

  $("[data-disabled]", card).style.display = p.enabled ? "none" : "";
  setText($("[data-folder]", card), p.folder);

  const error = $("[data-error]", card);
  setText(error, p.last_error || "");
  error.style.display = p.last_error ? "" : "none";

  setText(
    $('[data-act="toggle"]', card),
    state.openPlaylists.has(p.id) ? "Hide" : "Details"
  );
  updatePlaylistSummary(card, p);
}

function updatePlaylistSummary(card, p) {
  setText(
    $("[data-summary]", card),
    `${p.counts.downloaded} downloaded` +
      (p.counts.permanent ? ` · ${p.counts.permanent} blocked` : "") +
      ` · last sync ${relTime(p.last_sync_at)}`
  );
}

function renderPlaylistCards(playlists) {
  const container = $("#playlist-list");

  if (!playlists.length) {
    if (!container.querySelector(".empty")) {
      container.innerHTML =
        '<div class="empty">No playlists mapped yet — add one on the Playlists tab.</div>';
    }
    state.playlistSignatures.clear();
    return;
  }
  const placeholder = container.querySelector(".empty");
  if (placeholder) placeholder.remove();

  for (const p of playlists) {
    let card = container.querySelector(`.playlist[data-id="${p.id}"]`);
    if (!card) {
      card = createPlaylistCard(p.id);
      container.append(card);
    }
    const signature = playlistSignature(p);
    if (state.playlistSignatures.get(p.id) === signature) {
      // Nothing new; only the "N minutes ago" text drifts.
      updatePlaylistSummary(card, p);
      continue;
    }
    state.playlistSignatures.set(p.id, signature);
    updatePlaylistCard(card, p);
    // The video list is only worth re-fetching when this playlist actually
    // changed, and only while someone is looking at it.
    if (state.openPlaylists.has(p.id)) loadVideos(p.id);
  }

  const wanted = playlists.map((p) => p.id);
  for (const card of [...container.querySelectorAll(".playlist")]) {
    if (!wanted.includes(card.dataset.id)) {
      card.remove();
      state.playlistSignatures.delete(card.dataset.id);
    }
  }
  // Reorder only if it genuinely differs — re-appending moves nodes, which
  // would undo a selection or blur a focused control inside a card.
  const current = [...container.querySelectorAll(".playlist")].map((c) => c.dataset.id);
  if (current.join() !== wanted.join()) {
    for (const id of wanted) {
      const card = container.querySelector(`.playlist[data-id="${id}"]`);
      if (card) container.append(card);
    }
  }
}

async function loadVideos(mappingId) {
  const body = $(`.playlist[data-id="${mappingId}"] [data-videos]`);
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
  // Same idea as the playlist cards: redraw only when something changed.
  const signature = JSON.stringify(runs);
  if (state.runsSignature === signature) return;
  state.runsSignature = signature;

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

async function requestStop() {
  try {
    await api("/api/sync/cancel", { method: "POST" });
    toast("Stopping — the download in progress is abandoned", "ok");
  } catch (err) {
    toast(err.message, "err");
  }
  refreshStatus();
}

$("#cancel-sync").addEventListener("click", requestStop);
$("#stop-sync").addEventListener("click", requestStop);

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
          <div><label>Folder</label><input type="text" data-folder value="${esc(
            m.folder
          )}"><div class="hint" data-folder-hint></div></div>
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
    attachFolderAutocomplete($("[data-folder]", card));
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

attachFolderAutocomplete($("#add-folder"));

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

  $$("[data-folder]", container).forEach(attachFolderAutocomplete);
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

  state.formatPresets = settings.format_presets || [];
  renderPresetOptions(state.formatPresets);
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

  syncPresetToFields();
  $("#public-url").value = settings.public_url || "";
  $("#public-url").disabled = settings.public_url_managed_by_env;
  $("#yt-source").value = youtube.source || "auto";
  $("#client-id").value = youtube.client_id || "";
  $("#client-secret").value = youtube.client_secret || "";
  $("#api-key").value = youtube.api_key || "";

  const auth = await api("/api/auth/status");
  $("#redirect-uri").textContent = auth.redirect_uri;
  $("#public-url-hint").textContent = auth.public_url_managed_by_env
    ? "Set by the DL4TV_PUBLIC_URL environment variable — change it there, not here."
    : auth.effective_public_url
    ? `Currently using ${auth.effective_public_url}. Leave blank to use whatever address each request arrives on.`
    : "Needed behind a reverse proxy or ingress, where the address dl4tv sees is not the one you use.";
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
    public_url: $("#public-url").value.trim(),
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
// access lock
// --------------------------------------------------------------------------

async function refreshAccess() {
  let access;
  try {
    access = await api("/api/access");
  } catch {
    return;
  }
  state.access = access;
  $("#lock-now").style.display = access.locked ? "" : "none";

  const stateLine = $("#access-state");
  if (!stateLine) return;
  if (access.managed_by_env) {
    stateLine.textContent =
      "Locked by the DL4TV_PASSPHRASE environment variable — change it there, not here.";
  } else if (access.locked) {
    stateLine.textContent = "Locked. Enter a new passphrase below to change it.";
  } else {
    stateLine.textContent = "Open — anyone who can reach this page can use it.";
  }
  $("#save-passphrase").disabled = access.managed_by_env;
  $("#remove-passphrase").disabled = access.managed_by_env || !access.locked;
  $("#passphrase").disabled = access.managed_by_env;
  $("#passphrase-confirm").disabled = access.managed_by_env;
}

async function submitPassphrase(passphrase) {
  const note = $("#access-saved");
  try {
    const result = await api("/api/access/passphrase", {
      method: "PUT",
      body: { passphrase },
    });
    $("#passphrase").value = "";
    $("#passphrase-confirm").value = "";
    note.textContent = result.locked ? "Passphrase set." : "Passphrase removed.";
    setTimeout(() => (note.textContent = ""), 4000);
    toast(result.locked ? "dl4tv is now locked" : "dl4tv is now open", "ok");
    refreshAccess();
  } catch (err) {
    toast(err.message, "err");
  }
}

$("#save-passphrase").addEventListener("click", () => {
  const passphrase = $("#passphrase").value;
  if (passphrase !== $("#passphrase-confirm").value) {
    toast("The two passphrases do not match", "err");
    return;
  }
  if (passphrase.length < 8) {
    toast("Use at least 8 characters", "err");
    return;
  }
  submitPassphrase(passphrase);
});

$("#remove-passphrase").addEventListener("click", () => {
  if (!confirm("Remove the passphrase? Anyone who can reach dl4tv will be able to use it."))
    return;
  submitPassphrase("");
});

$("#lock-now").addEventListener("click", async () => {
  await api("/api/access/lock", { method: "POST" });
  window.location.href = "/login";
});

// --------------------------------------------------------------------------
// download format presets
// --------------------------------------------------------------------------

const CUSTOM_PRESET = "custom";

function renderPresetOptions(presetList) {
  const select = $("#dl-preset");
  select.innerHTML =
    presetList.map((p) => `<option value="${esc(p.id)}">${esc(p.label)}</option>`).join("") +
    `<option value="${CUSTOM_PRESET}">Custom — set the fields below yourself</option>`;
}

/** Keep the dropdown honest about what the raw fields currently say. */
function syncPresetToFields() {
  const format = $("#dl-format").value.trim();
  const container = $("#dl-container").value.trim();
  const match = (state.formatPresets || []).find(
    (p) => p.format === format && p.merge_output_format === container
  );
  const id = match ? match.id : CUSTOM_PRESET;
  $("#dl-preset").value = id;
  $("#dl-preset-hint").textContent = match
    ? match.detail
    : "These fields do not match a preset. Pick one to overwrite them.";
}

$("#dl-preset").addEventListener("change", () => {
  const chosen = (state.formatPresets || []).find((p) => p.id === $("#dl-preset").value);
  if (!chosen) {
    // "Custom" selected: leave whatever is in the fields alone.
    $("#dl-preset-hint").textContent = "Edit the fields below as you like.";
    return;
  }
  $("#dl-format").value = chosen.format;
  $("#dl-container").value = chosen.merge_output_format;
  $("#dl-preset-hint").textContent = `${chosen.detail} Save settings to apply.`;
});

$("#dl-format").addEventListener("input", syncPresetToFields);
$("#dl-container").addEventListener("input", syncPresetToFields);

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
refreshAccess();
tick();
