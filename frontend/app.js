const $ = (id) => document.getElementById(id);

let searchMode = "movie";
let searchResults = [];
let selectedItem = null;
let episodes = [];
let selectedEpisode = null;
let contentView = null;

async function api(path, options = {}) {
  const res = await fetch(path, options);
  if (!res.ok) {
    const text = await res.text();
    let detail = text;
    try {
      detail = JSON.parse(text).detail ?? text;
    } catch (_) {}
    throw new Error(detail || res.statusText);
  }
  return res.json();
}

function formatBytes(n) {
  if (n == null) return "—";
  const units = ["B", "KB", "MB", "GB"];
  let v = n;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(1)} ${units[i]}`;
}

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function resetContentState() {
  searchResults = [];
  selectedItem = null;
  episodes = [];
  selectedEpisode = null;
  contentView = null;
  $("content-panel").classList.add("hidden");
  $("content-body").innerHTML = "";
  $("back-btn").classList.add("hidden");
}

function setSearchMode(mode) {
  searchMode = mode;
  document.querySelectorAll(".mode-btn").forEach((btn) => {
    const active = btn.dataset.mode === mode;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-selected", active ? "true" : "false");
  });
  $("query").placeholder =
    mode === "series" ? "Show name…" : "Movie name…";
  resetContentState();
}

function showContentPanel(title, showBack = false) {
  $("content-title").textContent = title;
  $("back-btn").classList.toggle("hidden", !showBack);
  $("content-panel").classList.remove("hidden");
}

function renderPosterCard(item, onClick) {
  const card = document.createElement("button");
  card.type = "button";
  card.className = "poster-card";
  const year = item.year ? ` (${item.year})` : "";
  const poster = item.poster
    ? `<img src="${escapeHtml(item.poster)}" alt="" referrerpolicy="no-referrer" />`
    : `<div class="poster-placeholder">No image</div>`;
  card.innerHTML = `
    ${poster}
    <div class="poster-info">
      <strong>${escapeHtml(item.name)}</strong>${escapeHtml(year)}
    </div>
  `;
  card.addEventListener("click", () => onClick(item));
  return card;
}

function showSearchResults(items) {
  searchResults = items;
  contentView = "results";
  selectedItem = null;
  selectedEpisode = null;
  episodes = [];

  const grid = document.createElement("div");
  grid.className = "poster-grid";
  if (!items.length) {
    grid.innerHTML = '<p class="meta">No results.</p>';
  } else {
    for (const item of items) {
      grid.appendChild(renderPosterCard(item, selectItem));
    }
  }

  $("content-body").innerHTML = "";
  $("content-body").appendChild(grid);
  showContentPanel("Results", false);
}

async function selectItem(item) {
  selectedItem = item;
  if (searchMode === "series") {
    await showEpisodes(item);
  } else {
    await showFiles(item, null);
  }
}

async function showEpisodes(item) {
  contentView = "episodes";
  selectedEpisode = null;
  $("content-body").innerHTML = '<p class="meta">Loading episodes…</p>';
  showContentPanel(`Episodes — ${item.name}`, true);

  try {
    episodes = await api(
      `/api/show/${encodeURIComponent(item.id)}/episodes`
    );
    const list = document.createElement("ul");
    list.className = "episode-list";
    if (!episodes.length) {
      list.innerHTML = '<li class="meta">No episodes found.</li>';
    } else {
      for (const ep of episodes) {
        const li = document.createElement("li");
        li.className = "card episode-item";
        li.innerHTML = `
          <div><strong>S${ep.season}E${ep.episode}</strong> — ${escapeHtml(ep.title)}</div>
        `;
        li.addEventListener("click", () => selectEpisode(ep));
        list.appendChild(li);
      }
    }
    $("content-body").innerHTML = "";
    $("content-body").appendChild(list);
  } catch (e) {
    $("content-body").innerHTML = `<p class="meta">${escapeHtml(e.message)}</p>`;
  }
}

async function selectEpisode(ep) {
  selectedEpisode = ep;
  const label = `${selectedItem.name} — S${ep.season}E${ep.episode}`;
  await showFiles(selectedItem, ep, label);
}

function renderLinkRow(label, url) {
  if (!url) return "";
  return `
    <div class="link-row">
      <span class="link-label">${escapeHtml(label)}</span>
      <a class="link-url" href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(url)}</a>
    </div>
  `;
}

function showStreamsList(streams, titleLabel) {
  contentView = "files";
  const list = document.createElement("ul");
  list.className = "stream-list";
  if (!streams.length) {
    list.innerHTML = '<li class="meta">No streams available.</li>';
  } else {
    for (const stream of streams) {
      const li = document.createElement("li");
      li.className = "card stream-item";
      const label = stream.name || `Stream ${stream.index + 1}`;
      const desc = stream.title || "No description";
      const hasDdl = !!stream.ddl;
      li.innerHTML = `
        <div><strong>${escapeHtml(label)}</strong></div>
        <div class="stream-title meta">${escapeHtml(desc)}</div>
        ${renderLinkRow("Magnet", stream.magnet)}
        ${renderLinkRow("Real Debrid", stream.rd_link)}
        ${renderLinkRow("Direct download", stream.ddl)}
        ${stream.resolve_error ? `<div class="meta" style="color:var(--err)">${escapeHtml(stream.resolve_error)}</div>` : ""}
        <button type="button" class="download-btn" ${hasDdl ? "" : "disabled"}>Download</button>
      `;
      if (hasDdl) {
        li.querySelector(".download-btn").addEventListener("click", () =>
          startDownload(stream.index, titleLabel)
        );
      }
      list.appendChild(li);
    }
  }
  $("content-body").innerHTML = "";
  $("content-body").appendChild(list);
}

async function showFiles(item, episode, titleLabel = null) {
  const label = titleLabel || item.name;
  $("content-body").innerHTML = '<p class="meta">Loading streams…</p>';
  showContentPanel(`Files — ${label}`, true);

  try {
    let streams;
    if (episode) {
      streams = await api(
        `/api/show/${encodeURIComponent(item.id)}/streams?season=${episode.season}&episode=${episode.episode}`
      );
    } else {
      streams = await api(
        `/api/movie/${encodeURIComponent(item.id)}/streams`
      );
    }
    showStreamsList(streams, label);
  } catch (e) {
    $("content-body").innerHTML = `<p class="meta">${escapeHtml(e.message)}</p>`;
  }
}

async function startDownload(streamIndex, title) {
  if (!selectedItem) return;
  const body = {
    imdb_id: selectedItem.id,
    stream_index: streamIndex,
    title,
    content_type: searchMode === "series" ? "series" : "movie",
  };
  if (selectedEpisode) {
    body.season = selectedEpisode.season;
    body.episode = selectedEpisode.episode;
  }
  try {
    await api("/api/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    await refreshDownloads();
  } catch (e) {
    alert(e.message);
  }
}

function handleBack() {
  if (contentView === "files" && searchMode === "series" && selectedItem) {
    showEpisodes(selectedItem);
    return;
  }
  if (contentView === "episodes" || contentView === "files") {
    showSearchResults(searchResults);
    return;
  }
}

async function refreshHealth() {
  const el = $("health-status");
  try {
    const h = await api("/api/health");
    if (h.egress_ok) {
      el.textContent = `Egress OK — ${h.observed_ip}`;
      el.className = "health ok";
    } else {
      el.textContent = `Egress mismatch — observed ${h.observed_ip ?? "?"} expected ${h.expected_ip}`;
      el.className = "health bad";
    }
  } catch (e) {
    el.textContent = `Health check failed: ${e.message}`;
    el.className = "health bad";
  }
}

async function refreshDownloads() {
  const list = $("downloads");
  list.innerHTML = "";
  try {
    const jobs = await api("/api/downloads");
    if (!jobs.length) {
      list.innerHTML = '<li class="meta">No downloads yet.</li>';
      return;
    }
    for (const job of jobs) {
      const li = document.createElement("li");
      li.className = `card download-item ${job.state}`;
      const progress =
        job.total_bytes != null
          ? `${formatBytes(job.bytes_received)} / ${formatBytes(job.total_bytes)}`
          : formatBytes(job.bytes_received);
      const speed =
        job.speed_bps != null
          ? ` · ${formatBytes(job.speed_bps)}/s`
          : "";
      li.innerHTML = `
        <div><strong>${escapeHtml(job.title)}</strong></div>
        <div class="state">${escapeHtml(job.state)}</div>
        <div class="meta">${escapeHtml(job.filename ?? "")} ${progress}${speed}</div>
        ${job.error ? `<div class="meta" style="color:var(--err)">${escapeHtml(job.error)}</div>` : ""}
      `;
      list.appendChild(li);
    }
  } catch (e) {
    list.innerHTML = `<li class="meta">${escapeHtml(e.message)}</li>`;
  }
}

$("search-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const q = $("query").value.trim();
  if (!q) return;
  try {
    const results = await api(
      `/api/search?q=${encodeURIComponent(q)}&type=${searchMode}`
    );
    showSearchResults(results);
  } catch (err) {
    alert(err.message);
  }
});

$("mode-movie").addEventListener("click", () => setSearchMode("movie"));
$("mode-show").addEventListener("click", () => setSearchMode("series"));
$("back-btn").addEventListener("click", handleBack);

refreshHealth();
refreshDownloads();
setInterval(refreshDownloads, 2000);
setInterval(refreshHealth, 30000);
