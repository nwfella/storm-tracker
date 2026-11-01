#!/usr/bin/env node
/**
 * Render gate for storm-tracker.
 *
 * Two stages, both of which must pass before anything is pushed:
 *   1. syntax  - extract the inline scripts and `node --check` them. One
 *                unbalanced bracket silently kills the whole page.
 *   2. render  - load the built page in headless Chrome and assert on the
 *                RENDERED containers (never on document text, because
 *                --dump-dom echoes the page's own <script> source and turns
 *                every string match into a false positive).
 *
 * Exit 0 = shippable. Exit 1 = do not push.
 */
"use strict";
const fs = require("fs");
const os = require("os");
const path = require("path");
const { execFileSync, spawnSync } = require("child_process");

const ROOT = path.resolve(__dirname, "..");
const PAGE = path.join(ROOT, "index.html");
const DATA = path.join(ROOT, "data", "storms.json");

let fails = [];
const ok = (m) => console.log("  ok   " + m);
const bad = (m) => { fails.push(m); console.log("  FAIL " + m); };

/* ---------------------------------------------------------------- stage 1 */
function syntax() {
  const html = fs.readFileSync(PAGE, "utf8");
  const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
  if (!scripts.length) return bad("no inline <script> found");
  const tmp = path.join(os.tmpdir(), "storm-check-" + process.pid + ".js");
  fs.writeFileSync(tmp, scripts.join("\n;\n"), { encoding: "utf8" });
  const r = spawnSync(process.execPath, ["--check", tmp], { encoding: "utf8" });
  fs.unlinkSync(tmp);
  if (r.status !== 0) return bad("inline script does not parse:\n" + (r.stderr || "").slice(0, 800));
  ok(`inline script parses (${scripts.length} blocks, ${(scripts.join("").length / 1024).toFixed(0)} KB)`);

  // the payload must actually be embedded, not fetched
  if (!/window\.__STORM_DATA__=/.test(html)) bad("storm payload not inlined");
  else ok("storm payload inlined");
  if (!/window\.__WORLD__=/.test(html)) bad("world geometry not inlined");
  else ok("world geometry inlined");
  if (/__DATA__|__WORLD__/.test(html.replace(/window\.__WORLD__/g, ""))) bad("unreplaced build placeholder");
  else ok("no unreplaced placeholders");
}

/* ---------------------------------------------------------------- stage 2 */
function findChrome() {
  const cands = [
    process.env.CHROME_BIN,
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium-browser",
    "/usr/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  ].filter(Boolean);
  for (const c of cands) { try { if (fs.existsSync(c)) return c; } catch (_) {} }
  return null;
}

function render() {
  const chrome = findChrome();
  if (!chrome) return bad("no Chrome/Chromium binary found (set CHROME_BIN)");

  const url = "file:///" + PAGE.replace(/\\/g, "/");
  // --no-sandbox / --disable-dev-shm-usage are required for Chrome >= 120 in
  // containers (a CI runner has no user namespace and a 64 MB /dev/shm);
  // without them the process dies instantly and dump-dom returns 0 bytes.
  const r = spawnSync(chrome, [
    "--headless=new", "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu-sandbox",
    "--disable-gpu", "--enable-unsafe-swiftshader",
    "--hide-scrollbars", "--window-size=1600,1000",
    "--virtual-time-budget=12000", "--dump-dom", url,
  ], { encoding: "utf8", maxBuffer: 96 * 1024 * 1024, timeout: 180000 });

  const dom = r.stdout || "";
  if (dom.length < 2000) {
    const why = (r.stderr || "").split("
").filter((l) => /error|Error|FATAL/.test(l)).slice(0, 4).join(" | ");
    return bad(`headless render produced no DOM (${dom.length} bytes, exit ${r.status}) ${why}`.slice(0, 600));
  }
  ok(`headless DOM captured (${(dom.length / 1024).toFixed(0)} KB)`);

  // Extract a container's inner HTML by scanning literal tags and counting
  // nesting. No RegExp for the tags: an escaped  inside a string-built
  // pattern silently became a backspace and the walk stopped at the first child.
  const inner = (id) => {
    const m = new RegExp('<([a-z]+)[^>]*id="' + id + '"[^>]*>', 'i').exec(dom);
    if (!m) return null;
    const tag = m[1];
    const start = m.index + m[0].length;
    let i = start, depth = 1;
    while (depth > 0) {
      const o = dom.indexOf('<' + tag, i);
      const c = dom.indexOf('</' + tag + '>', i);
      if (c < 0) return dom.slice(start);
      if (o >= 0 && o < c) { depth++; i = o + tag.length + 1; }
      else { depth--; i = c + tag.length + 3; if (!depth) return dom.slice(start, c); }
    }
    return dom.slice(start);
  };

  const data = JSON.parse(fs.readFileSync(DATA, "utf8"));
  const active = (data.storms || []).filter((s) => s.active !== false).length;
  const total = (data.storms || []).length;

  // storm rows (or the explicit empty state)
  const rows = inner("rows");
  if (rows === null) bad("#rows not rendered");
  else if (total > 0 && !/class="row/.test(rows)) bad(`#rows rendered no .row for ${total} systems`);
  else if (total === 0 && !/id="empty"/.test(rows) && !/No active/.test(rows)) bad("empty state missing");
  else ok(`#rows rendered ${(rows.match(/class="row/g) || []).length} rows (${total} systems, ${active} active)`);

  // storm count readout must agree with the payload
  const cnt = inner("stCount");
  if (cnt === null) bad("#stCount missing");
  else if (String(cnt).trim() !== String(active)) bad(`#stCount=${cnt} but payload has ${active} active`);
  else ok(`#stCount = ${cnt}`);

  // markers exist for every system on the globe
  const mk = (dom.match(/class="marker/g) || []).length;
  if (mk !== total) bad(`${mk} marker elements for ${total} systems`);
  else ok(`${mk} globe markers built`);

  // status bar must show a real bake time, not a placeholder
  const gen = inner("stGen");
  if (gen === null || /—|undefined|Invalid/.test(gen)) bad("#stGen shows no bake time");
  else ok(`#stGen = ${gen}`);

  // a busted payload shows up as NaN/undefined in rendered text
  const surfaces = ["rows", "dBody", "counts"].map(inner).join(" ");
  if (/undefined|NaN|\[object Object\]/.test(surfaces)) bad("rendered text contains undefined/NaN/[object Object]");
  else ok("no undefined/NaN in rendered panels");

  // the detail panel must be populated on first paint
  const body = inner("dBody");
  if (!body || body.length < 120) bad("detail panel not populated");
  else ok(`detail panel populated (${body.length} chars)`);

  // every active storm must carry a position and a category
  const missing = (data.storms || []).filter(
    (s) => s.active !== false && (typeof s.lat !== "number" || typeof s.lon !== "number" || !s.cat_class)
  );
  if (missing.length) bad("active systems missing coordinates/category: " + missing.map((s) => s.name).join(", "));
  else ok("every active system has coordinates + category");

  if (r.stderr && /Uncaught|TypeError|ReferenceError/.test(r.stderr)) {
    bad("console error during render: " + (r.stderr.match(/(Uncaught.*|TypeError.*|ReferenceError.*)/) || [])[0]);
  }
}

/* ------------------------------------------------------------------- run  */
console.log("storm-tracker render gate");
if (!fs.existsSync(PAGE)) { console.error("index.html missing - run scripts/build.py"); process.exit(1); }
console.log("\n1. syntax");
syntax();
console.log("\n2. render");
try { render(); } catch (e) { bad("render threw: " + e.message); }

console.log("");
if (fails.length) { console.error(`GATE FAILED (${fails.length})`); process.exit(1); }
console.log("GATE PASSED");
