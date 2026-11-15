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
/** 一度に描く行数。件数が多いと行の生成が重いので、残りはスクロールに合わせて足していく。 */
const PAGE_SIZE = 200;
/* 配列の項目は複数選択。verdict/source はいずれかに一致（OR）、lang はすべて対応（AND）、
 * tag は tagMode（all=すべて含む / any=いずれか含む）、xtag は1つでも含めば除外。 */
const DEFAULT_STATE = {
  tab: null, q: "", verdict: [], source: [], tag: [], tagMode: "all", xtag: [], lang: [],
  priceMax: "", minRatio: "", minReviews: "", maxReviews: "",
  sort: "score", dir: "desc", demo: false, new: false,
};
/** 絞り込み条件（タブ・並べ替え以外）のキー。 */
const FILTER_KEYS = ["q", "verdict", "source", "tag", "xtag", "lang", "priceMax", "minRatio", "minReviews", "maxReviews", "demo", "new"];

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

/** Steam と itch の両方にある作品（統合済み）は両方のソースに属する。
 * itch にしかない作品は、さらに "itch_only"（itch.io 限定）にも属する。 */
function sourcesOf(item) {
  if (item.itch_url) return [item.source, "itch"];
  if (item.source === "itch") return ["itch", "itch_only"];
  return [item.source];
}

function matches(item, state) {
  if (state.tab && state.tab !== "all" && !(state.tab in item.categories)) return false;
  if (state.verdict.length && !state.verdict.includes(verdictOf(item))) return false;
  if (state.source.length && !sourcesOf(item).some((s) => state.source.includes(s))) return false;
  if (state.tag.length) {
    const has = (t) => item.tags.includes(t);
    if (state.tagMode === "any" ? !state.tag.some(has) : !state.tag.every(has)) return false;
  }
  if (state.xtag.some((t) => item.tags.includes(t))) return false;
  if (!state.lang.every((l) => item.languages.includes(l))) return false;
  // 数値条件: 値が不明（null）の作品は、条件を指定したときは出さない
  if (state.priceMax !== "" && !(item.price_jpy != null && item.price_jpy <= Number(state.priceMax))) return false;
  if (state.minRatio !== "" && !(item.positive_ratio != null && item.positive_ratio >= Number(state.minRatio))) return false;
  if (state.minReviews !== "" && !(item.reviews != null && item.reviews >= Number(state.minReviews))) return false;
  if (state.maxReviews !== "" && !(item.reviews != null && item.reviews <= Number(state.maxReviews))) return false;
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

/** 既定値から変えている絞り込み条件の数（配列は選んだ値の数）。 */
function activeFilterCount(state, defaults) {
  let n = 0;
  for (const k of FILTER_KEYS) {
    if (Array.isArray(state[k])) n += state[k].length;
    else if (state[k] !== defaults[k]) n += 1;
  }
  return n;
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

/** 件数の表示。一部だけ描いているときは「全体◯件中 ◯件」と出す。 */
function countText(total, shown, filterCount) {
  const fmt = (n) => n.toLocaleString("ja-JP");
  const base = shown < total ? `${fmt(total)}件中 ${fmt(shown)}件を表示` : `${fmt(total)}件を表示`;
  return base + (filterCount ? `（絞り込み条件 ${filterCount}件）` : "");
}

function formatPrice(yen) {
  if (yen === null || yen === undefined) return "—";
  if (yen === 0) return "無料";
  return "¥" + Math.round(yen).toLocaleString("ja-JP");
}

function formatRatio(r) {
  return r === null || r === undefined ? "—" : Math.round(r * 100) + "%";
}

/** URL の hash（#tab=horror&tag=Short&tag=Co-op ...）と状態の相互変換。配列はキーを繰り返す。 */
function stateFromHash(hash, defaults) {
  const state = { ...defaults };
  for (const [k, d] of Object.entries(defaults)) if (Array.isArray(d)) state[k] = [...d];
  const params = new URLSearchParams(hash.replace(/^#/, ""));
  for (const [k, v] of params) {
    if (!(k in state)) continue;
    const d = defaults[k];
    if (Array.isArray(d)) { if (v && !state[k].includes(v)) state[k].push(v); }
    else state[k] = typeof d === "boolean" ? v === "1" : v;
  }
  // 旧形式の「NG 以外」
  if (state.verdict.includes("not_ng")) {
    state.verdict = [...new Set([...state.verdict.filter((v) => v !== "not_ng"), "ok", "conditional", "unknown"])];
  }
  return state;
}

function stateToHash(state, defaults) {
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(state)) {
    if (Array.isArray(v)) { for (const x of v) params.append(k, x); continue; }
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
      desc ? el("p", { class: "desc" }, desc) : null,
      // overrides.yaml に書く ID（選択してコピーしやすいよう独立した要素にする）
      el("div", { class: "item-id", title: "overrides.yaml で使う作品ID" }, item.id)),
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
  const tagAdd = document.getElementById("tag-add");
  const xtagAdd = document.getElementById("xtag-add");
  const chipsEl = document.getElementById("tag-chips");
  const moreBtn = document.getElementById("more");
  const now = Date.now();
  const MULTI = ["verdict", "source", "lang"];
  const SINGLE = ["q", "tagMode", "priceMax", "minRatio", "minReviews", "maxReviews", "sort", "dir"];
  let state = stateFromHash(location.hash, defaults);
  if (!tabs.includes(state.tab)) state.tab = defaults.tab;

  /** タグ追加用の select を、現在のタブのタグ（選択済みは除く）で作り直す。 */
  function syncTags() {
    const chosen = new Set([...state.tag, ...state.xtag]);
    const options = tagOptions(items, state.tab).filter(([t]) => !chosen.has(t));
    for (const [select, label] of [[tagAdd, "＋ 含むタグを追加"], [xtagAdd, "＋ 除外タグを追加"]]) {
      select.replaceChildren(el("option", { value: "" }, label),
        ...options.map(([t, n]) => el("option", { value: t }, `${t} (${n})`)));
      select.value = "";
    }
    const chip = (key, t, cls, prefix) => {
      const b = el("button", { type: "button", class: "chip " + cls, "aria-label": `${prefix}タグ「${t}」を外す` },
        el("span", { class: "chip-mark", "aria-hidden": "true" }, key === "xtag" ? "−" : "＋"), t,
        el("span", { class: "chip-x", "aria-hidden": "true" }, "×"));
      b.addEventListener("click", () => update({ [key]: state[key].filter((x) => x !== t) }));
      return b;
    };
    chipsEl.replaceChildren(
      ...state.tag.map((t) => chip("tag", t, "chip-include", "含む")),
      ...state.xtag.map((t) => chip("xtag", t, "chip-exclude", "除外")));
    chipsEl.hidden = !state.tag.length && !state.xtag.length;
    form.elements.tagMode.disabled = state.tag.length < 2;
  }

  function syncForm() {
    for (const name of SINGLE) form.elements[name].value = state[name];
    for (const name of MULTI) {
      for (const box of form.querySelectorAll(`input[name="${name}"]`)) box.checked = state[name].includes(box.value);
    }
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

  let selected = [];
  let shown = 0;

  /** 次の PAGE_SIZE 件を表の末尾に足す。 */
  function appendMore() {
    const next = selected.slice(shown, shown + PAGE_SIZE);
    if (!next.length) return;
    const frag = document.createDocumentFragment();
    for (const item of next) frag.append(renderRow(item, state.tab, now));
    rows.append(frag);
    shown += next.length;
    const rest = selected.length - shown;
    countEl.textContent = countText(selected.length, shown, activeFilterCount(state, defaults));
    moreBtn.hidden = rest <= 0;
    moreBtn.textContent = `さらに表示（残り ${rest.toLocaleString("ja-JP")}件）`;
  }

  function render() {
    selected = selectItems(items, state);
    shown = 0;
    rows.replaceChildren();
    countEl.textContent = countText(0, 0, activeFilterCount(state, defaults));
    moreBtn.hidden = true;
    if (selected.length) appendMore();
    else rows.append(el("tr", { class: "empty" }, el("td", { colspan: 11 }, "条件に合う作品はありません。")));
    const hash = stateToHash(state, defaults);
    if (hash !== location.hash) history.replaceState(null, "", hash || location.pathname + location.search);
    watchMore();
  }

  // ボタンが画面に近づいたら自動で続きを足す。observe し直すと現在の交差状態で必ず1回呼ばれるので、
  // 足したあともまだ画面内にある（画面が縦に長い等）場合はそのまま続けて足される。
  const observer = "IntersectionObserver" in window
    ? new IntersectionObserver((entries) => {
      if (!entries.some((e) => e.isIntersecting) || moreBtn.hidden) return;
      appendMore();
      watchMore();
    }, { rootMargin: "0px 0px 800px 0px" })
    : null;
  function watchMore() {
    if (!observer) return;
    observer.unobserve(moreBtn);
    if (!moreBtn.hidden) observer.observe(moreBtn);
  }
  moreBtn.addEventListener("click", () => { appendMore(); watchMore(); });

  function update(patch) {
    state = { ...state, ...patch };
    syncTags();
    syncForm();
    render();
  }

  for (const b of document.querySelectorAll(".tab")) {
    b.addEventListener("click", () => update({ tab: b.dataset.tab }));
  }
  form.addEventListener("input", (e) => {
    const t = e.target;
    if (!t.name) return;
    if (MULTI.includes(t.name)) {
      update({ [t.name]: [...form.querySelectorAll(`input[name="${t.name}"]:checked`)].map((b) => b.value) });
    } else {
      update({ [t.name]: t.type === "checkbox" ? t.checked : t.value });
    }
  });
  for (const [select, key] of [[tagAdd, "tag"], [xtagAdd, "xtag"]]) {
    select.addEventListener("change", () => {
      if (select.value) update({ [key]: [...state[key], select.value] });
    });
  }
  for (const b of document.querySelectorAll("th button[data-sort]")) {
    b.addEventListener("click", () => {
      const key = b.dataset.sort;
      const dir = state.sort === key
        ? (state.dir === "desc" ? "asc" : "desc")
        : (key === "price" || key === "title" ? "asc" : "desc");
      update({ sort: key, dir });
    });
  }
  // 状態の配列はその場で書き換えないので、既定値の配列をそのまま入れてよい
  document.getElementById("reset").addEventListener("click", () => update({ ...defaults, tab: state.tab }));

  syncTags();
  syncForm();
  render();
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    scoreOf, verdictOf, sourcesOf, needsRecheck, matches, selectItems, tagOptions, activeFilterCount,
    countText, PAGE_SIZE, formatPrice, formatRatio, stateFromHash, stateToHash, safeUrl, DEFAULT_STATE,
  };
} else if (typeof document !== "undefined") {
  init();
}
