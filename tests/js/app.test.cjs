// app.js の純粋関数のテスト（node --test tests/js）
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const app = require(path.join(__dirname, "..", "..", "src", "yamo", "web", "app.js"));

function item(id, kw = {}) {
  return {
    id, title: `Game ${id}`, developers: ["Dev"], description: "", description_ja: null,
    tags: ["Horror"], languages: ["english"], has_demo: false, is_new: false,
    categories: { horror: 50 }, score: 50, reviews: 100, positive_ratio: 0.9, price_jpy: 500,
    release_date: "2024-01-01", streaming: { verdict: "unknown" }, ...kw,
  };
}
const S = (patch) => ({ ...app.DEFAULT_STATE, tab: "all", ...patch });

test("カテゴリタブではそのカテゴリのスコアで並ぶ", () => {
  const a = item("a", { categories: { horror: 90, bakage: 10 }, score: 90 });
  const b = item("b", { categories: { horror: 20, bakage: 80 }, score: 80 });
  assert.deepEqual(app.selectItems([a, b], S({ tab: "bakage" })).map((i) => i.id), ["b", "a"]);
  assert.deepEqual(app.selectItems([a, b], S({ tab: "horror" })).map((i) => i.id), ["a", "b"]);
  assert.deepEqual(app.selectItems([a, b], S({ tab: "all" })).map((i) => i.id), ["a", "b"]);
});

test("タブに属さない作品は出ない", () => {
  const a = item("a", { categories: { horror: 50 } });
  assert.equal(app.selectItems([a], S({ tab: "bakage" })).length, 0);
});

test("配信可否で絞り込める（NG 以外を含む）", () => {
  const items = ["ok", "conditional", "ng", "unknown"].map((v) => item(v, { streaming: { verdict: v } }));
  assert.deepEqual(app.selectItems(items, S({ verdict: "ok" })).map((i) => i.id), ["ok"]);
  assert.deepEqual(app.selectItems(items, S({ verdict: "not_ng" })).map((i) => i.id).sort(),
    ["conditional", "ok", "unknown"]);
  assert.equal(app.verdictOf(item("x", { streaming: undefined })), "unknown");
});

test("タグ・言語・デモ・NEW で絞り込める", () => {
  const a = item("a", { tags: ["Horror", "Short"], languages: ["english", "japanese"], has_demo: true, is_new: true });
  const b = item("b");
  const ids = (st) => app.selectItems([a, b], S(st)).map((i) => i.id);
  assert.deepEqual(ids({ tag: "Short" }), ["a"]);
  assert.deepEqual(ids({ lang: "japanese" }), ["a"]);
  assert.deepEqual(ids({ demo: true }), ["a"]);
  assert.deepEqual(ids({ new: true }), ["a"]);
});

test("検索は空白区切りの AND・大文字小文字を区別しない・日本語説明も対象", () => {
  const a = item("a", { title: "Quiet Hallway", description_ja: "廊下を歩く短いホラー" });
  const b = item("b", { title: "Loud Room" });
  const ids = (q) => app.selectItems([a, b], S({ q })).map((i) => i.id);
  assert.deepEqual(ids("quiet"), ["a"]);
  assert.deepEqual(ids("hallway 廊下"), ["a"]);
  assert.deepEqual(ids("hallway room"), []);
});

test("並べ替え: 価格昇順・欠損値は末尾", () => {
  const items = [item("a", { price_jpy: 900 }), item("b", { price_jpy: null }), item("c", { price_jpy: 0 })];
  assert.deepEqual(app.selectItems(items, S({ sort: "price", dir: "asc" })).map((i) => i.id), ["c", "a", "b"]);
});

test("並べ替え: 同値はスコア降順→ID で安定", () => {
  const items = [item("b", { reviews: 10, score: 40 }), item("a", { reviews: 10, score: 40 }), item("c", { reviews: 10, score: 60 })];
  assert.deepEqual(app.selectItems(items, S({ sort: "reviews" })).map((i) => i.id), ["c", "a", "b"]);
});

test("タグの選択肢は現在のタブの作品から多い順", () => {
  const items = [
    item("a", { tags: ["Horror", "Short"], categories: { horror: 1 } }),
    item("b", { tags: ["Short"], categories: { horror: 1 } }),
    item("c", { tags: ["Funny"], categories: { bakage: 1 } }),
  ];
  assert.deepEqual(app.tagOptions(items, "horror"), [["Short", 2], ["Horror", 1]]);
  assert.equal(app.tagOptions(items, "all").length, 3);
});

test("30日以上前の判定は要再確認", () => {
  const now = Date.parse("2026-11-15T00:00:00Z");
  assert.equal(app.needsRecheck(item("a", { streaming: { checked_at: "2026-10-01T00:00:00Z" } }), now), true);
  assert.equal(app.needsRecheck(item("a", { streaming: { checked_at: "2026-11-01T00:00:00Z" } }), now), false);
  assert.equal(app.needsRecheck(item("a"), now), false);
});

test("表示用の整形", () => {
  assert.equal(app.formatPrice(0), "無料");
  assert.equal(app.formatPrice(null), "—");
  assert.equal(app.formatPrice(1980), "¥1,980");
  assert.equal(app.formatRatio(0.956), "96%");
});

test("URL hash と状態の往復（既定値は省略）", () => {
  const defaults = { ...app.DEFAULT_STATE, tab: "horror" };
  const st = { ...defaults, tab: "bakage", tag: "Dark Humor", demo: true };
  const hash = app.stateToHash(st, defaults);
  assert.equal(hash, "#tab=bakage&tag=Dark+Humor&demo=1");
  assert.deepEqual(app.stateFromHash(hash, defaults), st);
  assert.equal(app.stateToHash(defaults, defaults), "");
  // 未知のキーは無視
  assert.deepEqual(app.stateFromHash("#evil=1", defaults), defaults);
});

test("http(s) 以外の URL はリンクにしない", () => {
  assert.equal(app.safeUrl("javascript:alert(1)"), null);
  assert.equal(app.safeUrl("data:text/html,x"), null);
  assert.equal(app.safeUrl("not a url"), null);
  assert.equal(app.safeUrl("https://example.com/a"), "https://example.com/a");
});

test("根拠URLのない手動確定（url: null）でも落ちない", () => {
  assert.equal(app.safeUrl(null), null);
  assert.equal(app.safeUrl(undefined), null);
});
