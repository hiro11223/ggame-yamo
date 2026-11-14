/* 実況ネタ探索: 一覧の描画・並べ替え・絞り込み（クライアントサイド）
 *
 * 作品名・説明文などは外部由来なので、必ず textContent / setAttribute で描画する（innerHTML は使わない）。
 * 前半の純粋関数は Node のテスト（tests/js/app.test.mjs）からも使う。
 */
"use strict";

const VERDICTS = {
  ok: { icon: "✅", label: "OK" },
  conditional: { icon: "⚠️", label: "条件付き" },
  ng: { icon: "❌", label: "NG" },
  unknown: { icon: "❓", label: "不明" },
};
const RECHECK_DAYS = 30;
const LANG_LABELS = { japanese: "日本語", english: "英語" };
const SOURCE_LABELS = { steam: "Steam", itch: "itch.io" };
const MAX_TAGS_SHOWN = 6;
const DEFAULT_STATE = {
  tab: null, q: "", verdict: "", tag: "", lang: "", sort: "score", dir: "desc", demo: false, new: false,
};

// ---- 純粋関数 -------------------------------------------------------------

/** タブに応じたスコア（カテゴリタブではそのカテゴリのスコア、全部では最大値）。 */
function scoreOf(item, tab) {
  if (tab && tab !== "all") return item.categories[tab] ?? -Infinity;
  return item.score;
}

function verdictOf(item) {
  return (item.streaming && item.streaming.verdict) || "unknown";
}

/** 判定から30日以上経っていれば「要再確認」。 */
function needsRecheck(item, now) {
  const s = item.streaming;
  if (!s || !s.checked_at) return false;
  const days = (now - Date.parse(s.checked_at)) / 86400000;
  return days >= RECHECK_DAYS;
}

function matches(item, state) {
  if (state.tab && state.tab !== "all" && !(state.tab in item.categories)) return false;
  if (state.verdict) {
    const v = verdictOf(item);
    if (state.verdict === "not_ng" ? v === "ng" : v !== state.verdict) return false;
  }
  if (state.tag && !item.tags.includes(state.tag)) return false;
  if (state.lang && !item.languages.includes(state.lang)) return false;
  if (state.demo && !item.has_demo) return false;
  if (state.new && !item.is_new) return false;
  if (state.q) {
    const hay = [item.title, (item.developers || []).join(" "), item.description_ja || "", item.description || ""]
      .join("\n").toLowerCase();
    for (const word of state.q.toLowerCase().split(/\s+/).filter(Boolean)) {
      if (!hay.includes(word)) return false;
    }
  }
  return true;
}

const SORT_KEYS = {
  score: (item, tab) => scoreOf(item, tab),
  reviews: (item) => item.reviews ?? -1,
  ratio: (item) => item.positive_ratio ?? -1,
  price: (item) => item.price_jpy ?? Infinity,
  release: (item) => item.release_date || "",
  title: (item) => (item.title || "").toLowerCase(),
};

function compare(a, b) {
  if (a < b) return -1;
  if (a > b) return 1;
  return 0;
}

/** 絞り込み＋並べ替え。同順位はスコア降順→ID で安定させる。 */
function selectItems(items, state) {
  const key = SORT_KEYS[state.sort] || SORT_KEYS.score;
  const sign = state.dir === "asc" ? 1 : -1;
  return items
    .filter((item) => matches(item, state))
    .sort((a, b) =>
      sign * compare(key(a, state.tab), key(b, state.tab)) ||
      compare(scoreOf(b, state.tab), scoreOf(a, state.tab)) ||
      compare(a.id, b.id));
}

/** 現在のタブに含まれる作品のタグを、多い順に返す。 */
function tagOptions(items, tab, limit = 80) {
  const counts = new Map();
  for (const item of items) {
    if (tab && tab !== "all" && !(tab in item.categories)) continue;
    for (const t of item.tags) counts.set(t, (counts.get(t) || 0) + 1);
  }
  return [...counts.entries()]
    .sort((a, b) => b[1] - a[1] || compare(a[0], b[0]))
    .slice(0, limit);
}

function formatPrice(yen) {
  if (yen === null || yen === undefined) return "—";
  if (yen === 0) return "無料";
  return "¥" + Math.round(yen).toLocaleString("ja-JP");
}

function formatRatio(r) {
  return r === null || r === undefined ? "—" : Math.round(r * 100) + "%";
}

/** URL の hash（#tab=horror&tag=Short ...）と状態の相互変換。 */
function stateFromHash(hash, defaults) {
  const state = { ...defaults };
  const params = new URLSearchParams(hash.replace(/^#/, ""));
  for (const [k, v] of params) {
    if (!(k in state)) continue;
    state[k] = typeof defaults[k] === "boolean" ? v === "1" : v;
  }
  return state;
}

function stateToHash(state, defaults) {
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(state)) {
    if (v === defaults[k]) continue;
    params.set(k, typeof v === "boolean" ? (v ? "1" : "0") : v);
  }
  const s = params.toString();
  return s ? "#" + s : "";
}

// ---- DOM ------------------------------------------------------------------

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else node.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

/** http(s) 以外の URL（javascript: 等）はリンクにしない。 */
function safeUrl(url) {
  try {
    const u = new URL(url);
    return u.protocol === "https:" || u.protocol === "http:" ? u.href : null;
  } catch {
    return null;
  }
}

function link(url, text) {
  const href = safeUrl(url);
  return href ? el("a", { href, target: "_blank", rel: "noopener noreferrer" }, text) : null;
}

function cell(label, cls, ...children) {
  return el("td", { class: cls, "data-label": label }, ...children);
}

function streamingCell(item, now) {
  const s = item.streaming || {};
  const v = VERDICTS[verdictOf(item)] || VERDICTS.unknown;
  const wrap = el("div", { class: "stream" },
    el("span", { class: "verdict verdict-" + verdictOf(item) }, v.icon + " " + v.label));
  const flags = [];
  if (s.method === "manual") flags.push(el("span", { class: "flag flag-manual" }, "手動確認済み"));
  else if (s.method === "auto") flags.push(el("span", { class: "flag" }, "自動判定"));
  else flags.push(el("span", { class: "flag" }, "未判定"));
  if (needsRecheck(item, now)) flags.push(el("span", { class: "flag flag-warn" }, "要再確認"));
  wrap.append(el("span", { class: "flags" }, ...flags));

  if (s.evidence && s.evidence.length) {
    const list = el("ul");
    for (const ev of s.evidence) {
      list.append(el("li", {},
        ev.excerpt ? el("q", {}, ev.excerpt) : null, " ",
        link(ev.url, "根拠"),
      ));
    }
    const when = s.checked_at ? el("p", { class: "checked" }, "判定日時: " + s.checked_at.slice(0, 10)) : null;
    wrap.append(el("details", {}, el("summary", {}, "根拠"), list, when));
  }
  if (verdictOf(item) === "unknown") {
    wrap.append(el("span", { class: "check-links" }, "確認: ",
      link(item.url, item.source === "itch" ? "itchページ" : "ストア"),
      item.website ? " / " : null, item.website ? link(item.website, "公式サイト") : null));
  }
  return wrap;
}

function renderRow(item, tab, now) {
  const title = el("div", { class: "title" },
    item.is_new ? el("span", { class: "new-badge" }, "NEW") : null,
    item.stale ? el("span", { class: "flag flag-warn" }, "前回データ") : null,
    link(item.url, item.title) || item.title,
  );
  const sub = [];
  if (item.developers && item.developers.length) sub.push(item.developers.join(", "));
  if (item.release_date) sub.push(item.release_date);
  if (item.early_access) sub.push("早期アクセス");
  const desc = item.description_ja || item.description;

  const thumb = item.thumbnail && safeUrl(item.thumbnail)
    ? el("img", { src: safeUrl(item.thumbnail), alt: "", loading: "lazy", width: 184, height: 86 })
    : el("div", { class: "no-thumb" });

  const tags = el("ul", { class: "tags" },
    ...item.tags.slice(0, MAX_TAGS_SHOWN).map((t) => el("li", {}, t)));
  const langs = item.languages.map((l) => LANG_LABELS[l] || l).join(" / ") || "—";
  const score = scoreOf(item, tab);

  return el("tr", {},
    el("td", { class: "c-thumb" }, link(item.url, thumb) || thumb),
    cell("タイトル", "c-title", title,
      sub.length ? el("div", { class: "sub" }, sub.join(" ・ ")) : null,
      desc ? el("p", { class: "desc" }, desc) : null),
    cell("ソース", "c-source", sourceCell(item)),
    cell("価格", "c-num", formatPrice(item.price_jpy)),
    cell("好評率", "c-num", formatRatio(item.positive_ratio)),
    cell("レビュー数", "c-num", item.reviews == null ? "—" : item.reviews.toLocaleString("ja-JP")),
    cell("タグ", "c-tags", tags),
    cell("デモ", "c-demo", item.has_demo ? "あり" : "—"),
    cell("言語", "c-lang", langs),
    cell("配信可否", "c-stream", streamingCell(item, now)),
    cell("スコア", "c-num c-score", Number.isFinite(score) ? score.toFixed(1) : "—",
      item.score_basis === "itch" ? el("span", { class: "basis", title: "itch はレビュー数がないため、新しさ・価格・タグ一致度による別基準" }, "itch基準") : null),
  );
}

function sourceCell(item) {
  const label = SOURCE_LABELS[item.source] || item.source;
  if (!item.itch_url) return label;
  // Steam と itch の両方にある作品（統合済み）
  return el("span", {}, label, " / ", link(item.itch_url, "itch.io") || "itch.io");
}

function init() {
  const data = JSON.parse(document.getElementById("data").textContent);
  const items = data.items;
  const tabs = [...Object.keys(data.categories), "all"];
  const defaults = { ...DEFAULT_STATE, tab: tabs[0] };
  const form = document.getElementById("controls");
  const rows = document.getElementById("rows");
  const countEl = document.getElementById("result-count");
  const tagSelect = form.elements.tag;
  const now = Date.now();
  let state = stateFromHash(location.hash, defaults);
  if (!tabs.includes(state.tab)) state.tab = defaults.tab;

  function syncTags() {
    const current = state.tag;
    tagSelect.replaceChildren(el("option", { value: "" }, "すべて"));
    for (const [t, n] of tagOptions(items, state.tab)) {
      tagSelect.append(el("option", { value: t }, `${t} (${n})`));
    }
    if (current && ![...tagSelect.options].some((o) => o.value === current)) {
      tagSelect.append(el("option", { value: current }, current));
    }
    tagSelect.value = current;
  }

  function syncForm() {
    for (const name of ["q", "verdict", "lang", "sort", "dir"]) form.elements[name].value = state[name];
    form.elements.demo.checked = state.demo;
    form.elements.new.checked = state.new;
    for (const b of document.querySelectorAll(".tab")) {
      b.setAttribute("aria-selected", String(b.dataset.tab === state.tab));
    }
    for (const b of document.querySelectorAll("th button[data-sort]")) {
      const th = b.closest("th");
      if (b.dataset.sort === state.sort) th.setAttribute("aria-sort", state.dir === "asc" ? "ascending" : "descending");
      else th.removeAttribute("aria-sort");
    }
  }

  function render() {
    const selected = selectItems(items, state);
    const frag = document.createDocumentFragment();
    for (const item of selected) frag.append(renderRow(item, state.tab, now));
    rows.replaceChildren(frag);
    countEl.textContent = `${selected.length}件を表示`;
    if (!selected.length) {
      rows.append(el("tr", { class: "empty" }, el("td", { colspan: 11 }, "条件に合う作品はありません。")));
    }
    const hash = stateToHash(state, defaults);
    if (hash !== location.hash) history.replaceState(null, "", hash || location.pathname + location.search);
  }

  function update(patch) {
    const tabChanged = patch.tab !== undefined && patch.tab !== state.tab;
    state = { ...state, ...patch };
    if (tabChanged) syncTags();
    syncForm();
    render();
  }

  for (const b of document.querySelectorAll(".tab")) {
    b.addEventListener("click", () => update({ tab: b.dataset.tab }));
  }
  form.addEventListener("input", (e) => {
    const t = e.target;
    if (!t.name) return;
    update({ [t.name]: t.type === "checkbox" ? t.checked : t.value });
  });
  for (const b of document.querySelectorAll("th button[data-sort]")) {
    b.addEventListener("click", () => {
      const key = b.dataset.sort;
      const dir = state.sort === key
        ? (state.dir === "desc" ? "asc" : "desc")
        : (key === "price" || key === "title" ? "asc" : "desc");
      update({ sort: key, dir });
    });
  }
  document.getElementById("reset").addEventListener("click", () => update({ ...defaults, tab: state.tab }));

  syncTags();
  syncForm();
  render();
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    scoreOf, verdictOf, needsRecheck, matches, selectItems, tagOptions,
    formatPrice, formatRatio, stateFromHash, stateToHash, safeUrl, DEFAULT_STATE,
  };
} else if (typeof document !== "undefined") {
  init();
}
