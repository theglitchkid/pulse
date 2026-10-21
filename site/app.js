"use strict";
(() => {
  const $ = (s) => document.querySelector(s);
  const PAGE = 40;
  const LABELS = { all: "All" };
  const ET = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", hour: "numeric", minute: "2-digit" });
  const saved = JSON.parse(localStorage.getItem("pulse:state") || "{}");
  const params = new URLSearchParams(location.search);
  const S = {
    cat: params.get("cat") || saved.cat || "all",
    sort: params.get("sort") || saved.sort || "hot",
    range: +(params.get("range") || saved.range || 24),
    q: params.get("q") || "",
  };
  let offline = false, items = [], meta = null, list = [], shown = 0, lastFetch = 0, heroIds = new Set();

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const terms = () => S.q.toLowerCase().split(/\s+/).filter(Boolean);
  const hl = (s) => {
    let out = esc(s);
    for (const t of terms()) {
      if (t.length < 2) continue;
      out = out.replace(new RegExp("(" + esc(t).replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + ")(?![^<]*>)", "gi"), "<mark>$1</mark>");
    }
    return out;
  };
  const ago = (ts) => {
    const m = Math.max(0, Math.round((Date.now() - ts) / 60000));
    if (m < 1) return "now";
    if (m < 60) return m + "m";
    const h = Math.round(m / 60);
    return h < 48 ? h + "h" : Math.round(h / 24) + "d";
  };
  const save = () => localStorage.setItem("pulse:state", JSON.stringify({ cat: S.cat, sort: S.sort, range: S.range }));
  const inCat = (it, c) => c === "all" || it.category === c || (it.also && it.also.includes(c));
  const inRange = (it) => Date.now() - it.ts <= S.range * 3600e3;

  function matches(it, ts) {
    if (!ts.length) return true;
    const hay = it._hay || (it._hay = [it.title, it.source, it.summary, it.who, LABELS[it.category], (it.more || []).join(" ")].join(" ").toLowerCase());
    return ts.every((t) => hay.includes(t));
  }

  function compute() {
    const ts = terms();
    const pool = items.filter((it) => inRange(it) && matches(it, ts));
    list = pool.filter((it) => inCat(it, S.cat));
    const key = S.sort === "new" ? "ts" : S.sort === "top" ? "top" : "score";
    list.sort((a, b) => b[key] - a[key] || b.score - a.score);
    return pool;
  }

  function renderChips(pool) {
    const cats = [{ key: "all", label: "All" }, ...((meta && meta.categories) || [])];
    $("#chips").innerHTML = cats.map((c) => {
      const n = c.key === "all" ? pool.length : pool.filter((it) => inCat(it, c.key)).length;
      return `<button class="chip" data-v="${c.key}" data-c="${c.key}" aria-pressed="${S.cat === c.key}"${n || S.cat === c.key ? "" : " data-zero"}>${c.key === "all" ? "" : "<i></i>"}${esc(c.label)}<span class="n">${n}</span></button>`;
    }).join("");
    const on = $("#chips").querySelector('[aria-pressed="true"]');
    if (on && on.offsetLeft + on.offsetWidth > $("#chips").scrollLeft + $("#chips").clientWidth) $("#chips").scrollLeft = on.offsetLeft - 16;
  }

  const heatDots = (h) => `<span class="heat" data-h="${h}" title="Heat ${h}/3" aria-label="Heat ${h} of 3"><b></b><b></b><b></b></span>`;
  function metaLine(it, withHeat = true) {
    const same = S.cat === it.category;
    const label = same ? it.who || "" : LABELS[it.category] + (it.who ? " · " + it.who : "");
    const more = it.n > 1 ? `<span class="more">+${it.n - 1} source${it.n > 2 ? "s" : ""}</span>` : "";
    return `<div class="meta">${label ? `<span class="tag" data-c="${it.category}">${esc(label)}</span>` : ""}<span class="src">${esc(it.source)}</span><span>${ago(it.ts)}</span>${more}${withHeat ? heatDots(it.heat) : ""}</div>`;
  }

  function row(it, i, k) {
    const img = it.image ? `<img class="thumb" src="${esc(it.image)}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer" onerror="this.remove()">` : "";
    const sum = it.summary ? `<div class="sum">${hl(it.summary)}</div>` : "";
    const delay = k < 12 ? ` style="animation-delay:${k * 22}ms"` : "";
    return `<li class="row"${delay}><a href="${esc(it.url)}" target="_blank" rel="noopener noreferrer"><span class="rk">${i + 1}</span><span class="t">${hl(it.title)}</span>${img}${sum}${metaLine(it)}</a></li>`;
  }

  function renderHero() {
    const on = S.cat === "all" && S.sort === "hot" && !S.q && list.length > 8;
    $("#hero").hidden = !on;
    heroIds = new Set();
    if (!on) return;
    const top = list.slice(0, 5);
    top.forEach((it) => heroIds.add(it.id));
    $("#heroRow").innerHTML = top.map((it, i) =>
      `<a class="hcard" data-c="${it.category}" href="${esc(it.url)}" target="_blank" rel="noopener noreferrer"><span class="big">${i + 1}</span><span class="t">${esc(it.title)}</span>${metaLine(it, false)}</a>`).join("");
    $("#heroRow").scrollLeft = 0;
  }

  function more() {
    if (shown >= list.length) return;
    const next = list.slice(shown, shown + PAGE);
    const off = heroIds.size;
    $("#feed").insertAdjacentHTML("beforeend", next.map((it, k) => row(it, shown + k + off, k)).join(""));
    shown += next.length;
  }

  function stateMsg() {
    const st = $("#state");
    if (list.length) { st.hidden = true; return; }
    st.hidden = false;
    if (!items.length) return;
    const ts = terms();
    const any = (h) => items.some((it) => Date.now() - it.ts <= h * 3600e3 && inCat(it, S.cat) && matches(it, ts));
    const wider = [6, 24, 72].find((h) => h > S.range && any(h));
    st.innerHTML = S.q
      ? `<strong>No matches for “${esc(S.q)}”</strong>${wider ? "There are matches further back." : "Try different or fewer words."}` + (wider ? `<br><button data-range="${wider}">Search last ${wider === 72 ? "3d" : wider + "h"}</button>` : "")
      : `<strong>Quiet out here</strong>Nothing in this view for the last ${S.range === 72 ? "3 days" : S.range + "h"}.` + (wider ? `<br><button data-range="${wider}">Show last ${wider === 72 ? "3d" : wider + "h"}</button>` : "");
  }

  function render(scroll) {
    const pool = compute();
    renderChips(pool);
    for (const [id, v] of [["#sort", S.sort], ["#range", String(S.range)]])
      document.querySelectorAll(id + " button").forEach((b) => b.setAttribute("aria-selected", b.dataset.v === v));
    renderHero();
    list = list.filter((it) => !heroIds.has(it.id));
    $("#feed").innerHTML = "";
    shown = 0;
    more();
    stateMsg();
    if (scroll) window.scrollTo({ top: 0 });
    $("#clear").hidden = !S.q;
  }

  function stamp() {
    if (!meta) return;
    const d = new Date(meta.generated_utc);
    $("#updated").textContent = (offline ? "Offline · " : "Updated ") + ET.format(d) + " ET";
    $("#updated").title = "Updated " + ago(d.getTime()) + " ago";
  }

  async function load(manual) {
    const btn = $("#refresh");
    btn.classList.add("spin");
    try {
      const res = await fetch("data.json?t=" + Date.now(), { cache: "no-store" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const data = await res.json();
      meta = data.meta;
      for (const c of meta.categories) LABELS[c.key] = c.label;
      items = data.items.map((it) => ({ ...it, ts: Date.parse(it.published_utc) }));
      lastFetch = Date.now();
      offline = false;
      stamp();
      render(manual);
    } catch (e) {
      offline = true;
      if (items.length) stamp();
      else {
        const st = $("#state");
        st.hidden = false;
        st.innerHTML = `<strong>Couldn’t load the feed</strong>Check your connection and try again.<br><button data-retry>Retry</button>`;
      }
    } finally {
      setTimeout(() => btn.classList.remove("spin"), 350);
    }
  }

  // events
  $("#chips").addEventListener("click", (e) => {
    const b = e.target.closest(".chip");
    if (!b || b.dataset.v === S.cat) return;
    S.cat = b.dataset.v; save(); render(true);
    b.scrollIntoView({ inline: "nearest", block: "nearest", behavior: "smooth" });
  });
  for (const id of ["#sort", "#range"]) $(id).addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    if (id === "#sort") S.sort = b.dataset.v; else S.range = +b.dataset.v;
    save(); render(true);
  });
  let raf = 0;
  $("#q").addEventListener("input", (e) => {
    S.q = e.target.value.trim();
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(() => render(true));
  });
  $("#clear").addEventListener("click", () => { $("#q").value = ""; S.q = ""; render(true); $("#q").focus(); });
  $("#refresh").addEventListener("click", () => load(true));
  $("#state").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    if (b.dataset.retry !== undefined) return load(true);
    S.range = +b.dataset.range; save(); render(true);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "/" && document.activeElement !== $("#q")) { e.preventDefault(); $("#q").focus(); }
    if (e.key === "Escape" && document.activeElement === $("#q")) $("#q").blur();
  });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && Date.now() - lastFetch > 5 * 60e3) load(false);
  });
  setInterval(() => { if (document.visibilityState === "visible") load(false); }, 10 * 60e3);
  setInterval(stamp, 60e3);
  new IntersectionObserver((en) => { if (en[0].isIntersecting) more(); }, { rootMargin: "800px" }).observe($("#sentinel"));

  $("#q").value = S.q;
  load(false);
  if ("serviceWorker" in navigator && (location.protocol === "https:" || location.hostname === "localhost"))
    navigator.serviceWorker.register("sw.js").catch(() => {});
})();
