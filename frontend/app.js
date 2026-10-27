const $ = (id) => document.getElementById(id);

const state = {
  mode: "movie", // "movie" | "series"
  results: [],
  item: null,
  episode: null,
  view: null, // "results" | "episodes" | "files"
};

// Build a DOM node; strings in `children` become text nodes (no HTML injection).
function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null || value === false) continue;
    if (key === "class") el.className = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(child));
  }
  return el;
}

async function api(path, options = {}) {
  const res = await fetch(path, options);
  if (!res.ok) {
    const text = await res.text();
    let detail = text;
    try {
      detail = JSON.parse(text).detail ?? text;
    } catch (_) {}
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail) || res.statusText);
  }
  return res.json();
}

const postJson = (path, body) =>
  api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

function formatBytes(n) {
  if (n == null) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i++;
  }
  return `${n.toFixed(1)} ${units[i]}`;
}

// --- Browser panel (results → episodes → files) ---------------------------

function show(title, body, { back = false } = {}) {
  $("browser-title").textContent = title;
  $("back-btn").classList.toggle("hidden", !back);
  $("browser-body").replaceChildren(body);
  $("browser").classList.remove("hidden");
}

const note = (text, cls = "") => h("p", { class: `meta ${cls}` }, text);

function resetBrowser() {
  Object.assign(state, { results: [], item: null, episode: null, view: null });
  $("browser").classList.add("hidden");
  $("browser-body").replaceChildren();
}

function setMode(mode) {
  state.mode = mode;
  document.querySelectorAll(".mode-btn").forEach((btn) => {
    const active = btn.dataset.mode === mode;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-selected", String(active));
  });
  $("query").placeholder = mode === "series" ? "Show name…" : "Movie name…";
  resetBrowser();
}

function posterCard(item) {
  const year = item.year ? ` (${item.year})` : "";
  return h(
    "button",
    { type: "button", class: "poster-card", onclick: () => selectItem(item) },
    item.poster
      ? h("img", { src: item.poster, alt: "", referrerpolicy: "no-referrer", loading: "lazy" })
      : h("div", { class: "poster-placeholder" }, "No image"),
    h("div", { class: "poster-info" }, h("strong", {}, item.name), year)
  );
}

function showResults(items) {
  Object.assign(state, { results: items, item: null, episode: null, view: "results" });
  const body = items.length
    ? h("div", { class: "poster-grid" }, items.map(posterCard))
    : note("No results.");
  show("Results", body);
}

function selectItem(item) {
  state.item = item;
  return state.mode === "series" ? showEpisodes(item) : showFiles(item, null);
}

async function showEpisodes(item) {
  state.view = "episodes";
  state.episode = null;
  show(`Episodes — ${item.name}`, note("Loading episodes…"), { back: true });
  try {
    const episodes = await api(`/api/show/${encodeURIComponent(item.id)}/episodes`);
    show(
      `Episodes — ${item.name}`,
      h(
        "ul",
        {},
        episodes.map((ep) =>
          h(
            "li",
            { class: "card clickable", onclick: () => selectEpisode(ep) },
            h("strong", {}, `S${ep.season}E${ep.episode}`),
            ` — ${ep.title}`
          )
        )
      ),
      { back: true }
    );
  } catch (e) {
    show(`Episodes — ${item.name}`, note(e.message, "err"), { back: true });
  }
}

function selectEpisode(ep) {
  state.episode = ep;
  return showFiles(state.item, ep, `${state.item.name} — S${ep.season}E${ep.episode}`);
}

function linkRow(label, url) {
  if (!url) return null;
  return h(
    "div",
    { class: "link-row" },
    h("span", { class: "link-label" }, label),
    h("a", { class: "link-url", href: url, target: "_blank", rel: "noopener noreferrer" }, url)
  );
}

function streamCard(stream) {
  const downloadable = !!(stream.ddl || stream.rd_link || stream.magnet);
  return h(
    "li",
    { class: "card" },
    h("strong", {}, stream.name || `Stream ${stream.index + 1}`),
    h("div", { class: "meta stream-title" }, stream.title || "No description"),
    linkRow("Magnet", stream.magnet),
    linkRow("Real Debrid", stream.rd_link),
    linkRow("Direct download", stream.ddl),
    h(
      "button",
      { type: "button", disabled: !downloadable, onclick: () => startDownload(stream.index) },
      "Download"
    )
  );
}

async function showFiles(item, episode, label = item.name) {
  state.view = "files";
  const title = `Files — ${label}`;
  show(title, note("Loading streams…"), { back: true });
  try {
    const base = episode
      ? `/api/show/${encodeURIComponent(item.id)}/streams?season=${episode.season}&episode=${episode.episode}`
      : `/api/movie/${encodeURIComponent(item.id)}/streams`;
    const streams = await api(base);
    show(title, h("ul", {}, streams.map(streamCard)), { back: true });
  } catch (e) {
    show(title, note(e.message, "err"), { back: true });
  }
}

async function startDownload(streamIndex) {
  if (!state.item) return;
  const body = {
    imdb_id: state.item.id,
    stream_index: streamIndex,
    content_type: state.mode,
  };
  if (state.episode) {
    body.season = state.episode.season;
    body.episode = state.episode.episode;
  }
  try {
    await postJson("/api/download", body);
    await refreshDownloads();
  } catch (e) {
    alert(e.message);
  }
}

function goBack() {
  if (state.view === "files" && state.mode === "series" && state.item) showEpisodes(state.item);
  else if (state.view === "files" || state.view === "episodes") showResults(state.results);
}

// --- CDN status -----------------------------------------------------------

let cdnPoll = null;

function cdnText(scan) {
  if (!scan) return null;
  const speed = scan.mbps ? ` (${Number(scan.mbps).toFixed(1)} Mbps)` : "";
  const host = scan.winner || "(unknown)";
  switch (scan.state) {
    case "pinned":
      return [`Real-Debrid CDN pinned: ${host}`, "ok"];
    case "cached":
      return [`Real-Debrid CDN cached: ${host}${speed}`, "ok"];
    case "done":
      return [`Real-Debrid CDN selected: ${host}${speed}`, "ok"];
    case "scanning": {
      const eta = typeof scan.eta_sec === "number" ? ` (ETA ~${Math.round(scan.eta_sec)}s)` : "";
      return [`Scanning Real-Debrid CDN${eta}…`, "scanning"];
    }
    case "error":
      return [`Real-Debrid CDN scan error${scan.error ? `: ${scan.error}` : ""}`, "bad"];
    default:
      return null;
  }
}

async function refreshCdnStatus() {
  const el = $("cdn-status");
  let scan = null;
  try {
    scan = (await api("/api/health")).rd_cdn_scan;
  } catch (_) {}
  const [text, cls] = cdnText(scan) ?? ["", ""];
  el.textContent = text;
  el.className = `status ${cls}`;
  clearTimeout(cdnPoll);
  if (scan?.state === "scanning") cdnPoll = setTimeout(refreshCdnStatus, 3500);
}

// --- Downloads ------------------------------------------------------------

function downloadCard(job) {
  const progress =
    job.total_bytes != null
      ? `${formatBytes(job.bytes_received)} / ${formatBytes(job.total_bytes)}`
      : formatBytes(job.bytes_received);
  const speed = job.speed_bps != null ? ` · ${formatBytes(job.speed_bps)}/s` : "";
  const pct = job.total_bytes ? Math.min(100, (job.bytes_received / job.total_bytes) * 100) : null;
  return h(
    "li",
    { class: `card download ${job.state}` },
    h("strong", {}, job.title),
    h("div", { class: "state" }, job.state),
    job.state === "downloading" && pct != null
      ? h("div", { class: "bar" }, h("div", { class: "bar-fill", style: `width:${pct.toFixed(1)}%` }))
      : null,
    h("div", { class: "meta" }, `${job.filename ?? ""} ${progress}${speed}`),
    job.error ? h("div", { class: "meta err" }, job.error) : null
  );
}

async function refreshDownloads() {
  const list = $("downloads");
  try {
    const jobs = await api("/api/downloads");
    list.replaceChildren(
      ...(jobs.length ? jobs.map(downloadCard) : [h("li", { class: "meta" }, "No downloads yet.")])
    );
  } catch (e) {
    list.replaceChildren(h("li", { class: "meta err" }, e.message));
  }
}

// --- Wiring ---------------------------------------------------------------

$("search-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const q = $("query").value.trim();
  if (!q) return;
  try {
    showResults(await api(`/api/search?q=${encodeURIComponent(q)}&type=${state.mode}`));
  } catch (err) {
    alert(err.message);
  }
});

$("paste-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const link = $("paste-link").value.trim();
  if (!link) return;
  try {
    await postJson("/api/download/link", {
      link,
      title: $("paste-title").value.trim() || null,
      content_type: state.mode,
    });
    $("paste-link").value = "";
    $("paste-title").value = "";
    await refreshDownloads();
  } catch (err) {
    alert(err.message);
  }
});

document.querySelectorAll(".mode-btn").forEach((btn) =>
  btn.addEventListener("click", () => setMode(btn.dataset.mode))
);
$("back-btn").addEventListener("click", goBack);

refreshCdnStatus();
refreshDownloads();
setInterval(refreshDownloads, 2000);
