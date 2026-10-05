/* Front-end tests — run with:  node tests/test_frontend.js
 *
 * index.html has no build step and no module system, so these lift the
 * functions straight out of the file by source markers and evaluate them
 * against stubs. Three things are worth locking down:
 *
 *   1. the LaTeX guards — "$5 to $10" and "$HOME/src" must never typeset;
 *   2. pipe tables, including the ragged ones Claude actually emits;
 *   3. the file explorer's follow/remember rule across terminal tabs.
 *
 * Node is only needed for the tests. The dashboard itself stays dependency-free.
 */
"use strict";
const fs = require("fs");
const path = require("path");

const ROOT = path.join(__dirname, "..");
const html = fs.readFileSync(path.join(ROOT, "index.html"), "utf8");

function slice(from, to) {
  const a = html.indexOf(from), b = html.indexOf(to);
  if (a < 0 || b < 0 || b < a) throw new Error(`source marker moved: ${from}`);
  return html.slice(a, b);
}

let fails = 0;
function report(name, ok, detail) {
  if (ok) { console.log("ok   " + name); return; }
  fails++;
  console.log("FAIL " + name + (detail ? "\n     " + detail : ""));
}

/* ---------------- the whole inline script parses ---------------- */
// the other tests lift slices; a syntax error anywhere else would ship
// unnoticed (and the page would be blank). Parse only: nothing runs.
{
  const m = html.match(/<script>([\s\S]*?)<\/script>/);
  let err = null;
  try { new Function(m[1]); } catch (e) { err = e; }
  report("index.html inline script parses", m && !err, err && err.message);
}

/* ---------------- markdown + KaTeX ---------------- */

global.katex = require(path.join(ROOT, "vendor", "katex.min.js"));
const { md, esc, isAbsPath, baseName, joinPath, shQuote, cleanTitle } = (0, eval)(
  slice("const esc = s =>", "/* ---------- keyboard ----") +
  "\n({md, esc, isAbsPath, baseName, joinPath, shQuote, mathify, inlineDollarOk, cleanTitle});");

const check = (name, input, pred) => {
  const out = md(input);
  report(name, pred(out), "out: " + out.slice(0, 200));
};
const isMath = o => o.includes('class="katex');
const noMath = o => !o.includes("katex");

// renders
check("display $$", "$$\\hat\\beta = (X'X)^{-1}X'y$$", isMath);
check("display \\[ \\]", "\\[ \\sum_{i=1}^N w_i \\]", isMath);
check("inline \\( \\)", "where \\(\\alpha > 0\\) holds", isMath);
check("inline $ with a command", "assume $\\varepsilon_i \\sim N(0,\\sigma^2)$", isMath);
check("inline $ with a subscript", "the term $x_{it}$ enters", isMath);
check("multiline display", "$$\n\\begin{pmatrix} a & b \\\\ c & d \\end{pmatrix}\n$$", isMath);
check("two display blocks", "$$a=1$$ and $$b=2$$",
      o => (o.match(/katex-display/g) || []).length === 2);
check("< inside math is not an entity", "$a < b$", isMath);

// injection: transcript text must never break out of an attribute
// an href value must hold no raw quote and no restored code/math placeholder
const hrefs = o => [...o.matchAll(/<a href="([^"]*)"/g)].map(m => m[1]);
const attrsOk = o => !/<a [^>]*\son\w+=/.test(o) && hrefs(o).every(h => !/[<>'"]/.test(h));
check("link cannot break out of href", '[x](https://a/"onmouseover="alert`1`)', attrsOk);
check("single-quote link payload stays inert", "[x](https://a/'onmouseover='y)", attrsOk);
check("code/math never restored inside an href", "[x](https://a/`q\"z`) [y](https://b/$\\alpha$)",
      o => attrsOk(o) && hrefs(o).length === 0);
check("plain link still renders", "see [docs](https://example.com/a?b=1&c=2)",
      o => o.includes('<a href="https://example.com/a?b=1&amp;c=2"'));
report("esc() is attribute-safe", esc(`a"b'c<d>&`) === "a&quot;b&#39;c&lt;d&gt;&amp;", esc(`a"b'c<d>&`));

// paths from a Windows server must work like POSIX ones
report("Windows drive path is absolute", isAbsPath("C:\\Users\\x\\proj") && isAbsPath("/Users/x") && !isAbsPath("proj"));
report("baseName handles both separators", baseName("C:\\Users\\x\\proj") === "proj" && baseName("/a/b/") === "b");
report("joinPath keeps the path's own separator",
       joinPath("C:\\Users\\x", "f.png") === "C:\\Users\\x\\f.png" && joinPath("/a/b", "f") === "/a/b/f" && joinPath("/", "f") === "/f");

report("shQuote leaves plain paths alone", shQuote("/a/b-c_d.txt") === "/a/b-c_d.txt");
report("shQuote quotes spaces, $ and quotes for POSIX", shQuote("/a/My $HOME/it's") === "'/a/My $HOME/it'\\''s'", shQuote("/a/My $HOME/it's"));
report("shQuote double-quotes Windows paths", shQuote("C:\\My Docs\\f.txt") === '"C:\\My Docs\\f.txt"', shQuote("C:\\My Docs\\f.txt"));

// must NOT render — transcripts are full of these
check("price range", "costs $5 to $10 per unit", noMath);
check("env vars", "export $PATH and $HOME are set", noMath);
check("var followed by a path", "check $HOME/src and $PATH now", noMath);
check("escaped dollar", "it costs \\$9.99 total", noMath);
check("bare identifier", "the $foo$ variable", noMath);
check("inside inline code", "run `echo $HOME$USER` now", noMath);
check("inside a fenced block", "```sh\nawk '{print $1}' f\necho $$x^2$$\n```", noMath);

// escaping and markdown must survive the math pass
check("no raw script tag", "$$\\text{a}$$ <script>alert(1)</script>",
      o => !o.includes("<script>"));
check("unparseable tex does not throw", "$$\\frobnicate{x}$$",
      o => o.length > 0 && !o.includes("<script>"));
check("code is still escaped", "`a < b`", o => o.includes("&lt;") && o.includes("<code>"));
check("bold/italic/code intact", "**b** and *i* and `c`",
      o => o.includes("<b>b</b>") && o.includes("<i>i</i>") && o.includes("<code>c</code>"));
check("bullets intact", "- one\n- two", o => o.includes("•"));
check("fences intact", "```py\nx=1\n```", o => o.includes("<pre><code>"));
check("links intact", "[x](https://a.b)", o => o.includes('href="https://a.b"'));

/* ---------------- pipe tables ---------------- */

const T3 =
  "| Reading | What moves | Source of the extra adaptation $ |\n" +
  "|---|---|\n" +
  "| Pure within-country composition (strict H1) | $O\\downarrow$, $S$ flat | $i$ converts its own other-aid |\n" +
  "| Reallocation into $i$ / scale (H2-flavored) | $S\\uparrow$, $O$ flat | money drawn from the global pool |\n";

check("table renders as a table", T3, o => o.includes("<table>") && o.includes("<tbody>"));
check("no raw pipes leak into the text", T3, o => !/\|/.test(o.replace(/<[^>]*>/g, "")));
check("short separator still yields 3 columns", T3,
      o => (o.match(/<th\b/g) || []).length === 3 &&
           (o.match(/<\/tr>/g) || []).length === 3);
check("math inside cells is typeset", T3, o => o.includes('class="katex'));
check("no leftover placeholders", T3, o => !o.includes("\x00"));
check("cell text survives", T3, o => o.includes("Pure within-country composition"));
check("separator row is not a body row", T3, o => !o.includes("<td>---"));

check("alignment from the separator",
      "| a | b | c |\n|:--|:-:|--:|\n| 1 | 2 | 3 |\n",
      o => o.includes('text-align:center') && o.includes('text-align:right'));
check("ragged rows are padded, not dropped",
      "| a | b | c |\n|---|---|---|\n| 1 |\n",
      o => (o.match(/<td\b/g) || []).length === 3);
check("escaped pipe stays a pipe",
      "| a | b |\n|---|---|\n| x \\| y | z |\n",
      o => o.includes("x | y"));
check("bold inside a cell", "| a |\n|---|\n| **hi** |\n", o => o.includes("<b>hi</b>"));
check("table inside a fence is left alone",
      "```\n| a |\n|---|\n| 1 |\n```", o => !o.includes("<table>"));
check("prose after a table resumes",
      "| a |\n|---|\n| 1 |\n\nAfter the table.",
      o => o.includes("<table>") && o.includes("After the table."));
check("a lone pipe line is not a table", "a | b\nnot a table", o => !o.includes("<table>"));
check("a horizontal rule is not a separator", "text\n---\nmore", o => !o.includes("<table>"));

/* ---------------- session titles ---------------- */

eq0("slash-command wrapper becomes the command",
    cleanTitle("<command-message>graphify</command-message> <command-name>/graphify</command-name> <command-args>--update src</command-args>"),
    "/graphify --update src");
eq0("truncated wrapper (no closing tag) still cleans",
    cleanTitle("<command-message>improve</command-message>\n<command-name>/improve</command-name>\n<command-args>look at the hooks and"),
    "/improve look at the hooks and");
eq0("message-only wrapper gets a slash",
    cleanTitle("<command-message>review</command-message>"), "/review");
eq0("other hyphenated tags are stripped",
    cleanTitle("<system-reminder>x</system-reminder> fix the chart"), "x fix the chart");
eq0("a pasted block's wrapper tag goes", cleanTitle('<pasted_content id="60bf"> # Plan'), "# Plan");
eq0("ordinary titles are untouched, even with <b>", cleanTitle("fix <b> in the  table"), "fix <b> in the table");
eq0("empty title stays empty", cleanTitle(undefined), "");
report("cleaned title is still escaped where it lands",
       esc(cleanTitle('<command-name>/x"><img src=x onerror=1></command-name>')).indexOf("<img") < 0);

function eq0(name, got, want) { report(name, got === want, `got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`); }

/* ---------------- theme contrast (WCAG 2.x) ---------------- */
// text tokens must clear 4.5:1 on every surface they are drawn on; control
// outlines (--border-strong) and chart bars 3:1 (1.4.11 non-text contrast)
{
  const THEMES = (0, eval)(slice("const THEMES = {", "const DEFAULT_THEME") + ";THEMES");
  const lum = h => {
    const c = [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16) / 255)
      .map(v => v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4);
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
  };
  const ratio = (a, b) => { const x = lum(a), y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); };
  const bad = [];
  for (const [n, t] of Object.entries(THEMES)) {
    for (const k of ["fg", "dim", "accent", "user", "think", "tool", "ok", "err"])
      for (const s of ["bg", "panel", "panel2"])
        if (ratio(t[k], t[s]) < 4.5) bad.push(`${n} ${k}/${s} ${ratio(t[k], t[s]).toFixed(2)}`);
    for (const s of ["bg", "panel"])
      if (!t.borderStrong || ratio(t.borderStrong, t[s]) < 3) bad.push(`${n} borderStrong/${s}`);
    if (!t.onErr || ratio(t.onErr, t.err) < 4.5) bad.push(`${n} onErr/err`);
    // --on-accent (text on primary buttons) is the theme's bg unless it sets onAccent
    if (ratio(t.onAccent || t.bg, t.accent) < 4.5) bad.push(`${n} onAccent/accent`);
    if (ratio(t.bar, t.bg) < 3) bad.push(`${n} bar/bg ${ratio(t.bar, t.bg).toFixed(2)}`);   // bars sit on --bg
    if (ratio(t.focus || t.accent, t.bg) < 3) bad.push(`${n} focus/bg`);
  }
  // diff lines: addfg/delfg on their fill (--ok/--err at 14% over --bg)
  const mix = (a, b, t) => "#" + [1, 3, 5].map(i => Math.round(parseInt(a.slice(i, i + 2), 16) * (1 - t)
    + parseInt(b.slice(i, i + 2), 16) * t).toString(16).padStart(2, "0")).join("");
  const dbad = [];
  for (const [n, t] of Object.entries(THEMES))
    for (const [fg, base] of [["addfg", "ok"], ["delfg", "err"]]) {
      const fill = mix(t.bg, t[base], 0.14);
      if (ratio(t[fg], fill) < 4.5) dbad.push(`${n} ${fg} on fill ${ratio(t[fg], fill).toFixed(2)}`);
    }
  report("diff lines: added/deleted text >= 4.5:1 on its tinted fill", !dbad.length, dbad.join("; "));
  report("every theme passes WCAG AA contrast (" + Object.keys(THEMES).length + " themes)", !bad.length, bad.join("; "));
  // the stylesheet defaults are Ember Dark: keep them in step with the table
  const gd = THEMES["Ember Dark"], root = slice(":root {", "color-scheme: dark;");
  const drift = ["bg", "panel", "panel2", "border", "fg", "dim", "accent", "user", "think", "tool",
                 "ok", "err", "termbg", "bar", "addfg", "delfg"].filter(k => !root.includes(`--${k}: ${gd[k]};`))
    .concat(root.includes(`--border-strong: ${gd.borderStrong};`) ? [] : ["border-strong"])
    .concat(root.includes(`--on-err: ${gd.onErr};`) ? [] : ["on-err"]);
  report(":root defaults match the Ember Dark theme", !drift.length, drift.join(", "));
}

/* Colours come from the theme tokens: a raw #hex is allowed only in the
   :root defaults (Ember Dark, kept in step above) and the theme tables. */
{
  const rest = html.replace(slice(":root {", "color-scheme: dark;"), "")
                   .replace(slice("const LIGHT_ANSI = {", "const DEFAULT_THEME"), "");
  const hits = [...rest.matchAll(/#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b(?![\w-])/g)].map(m => m[0]);
  report("no hard-coded colours outside the theme tables", !hits.length, hits.join(", "));
}

/* Rebranding must not replace an existing user's chosen theme. */
{
  const getTheme = new Function("localStorage",
    slice("const THEMES = {", "/* Claude Code draws its diffs") + ";return themeName();");
  report("new users get Ember Dark", getTheme({getItem: () => null}) === "Ember Dark");
  report("unknown saved theme falls back to Ember Dark", getTheme({getItem: () => "missing"}) === "Ember Dark");
  for (const name of ["GitHub Dark", "GitHub Light", "Solarized Dark", "Solarized Light",
                      "Dracula", "Monokai", "Tomorrow Night", "Cobalt", "Ember Light"])
    report("saved theme preserved: " + name, getTheme({getItem: () => name}) === name);
}

/* ---------------- CSP-safe markup ---------------- */
// script-src is 'self' + the hash of ONE inline script: inline handlers,
// javascript: URLs and eval would all be blocked at runtime
{
  const inline = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)];
  report("exactly one inline <script>", inline.length === 1, inline.length + " found");
  report("no inline on*= handler attributes", !/<[a-z][^>]*\son[a-z]+\s*=/i.test(html.replace(/<script>[\s\S]*?<\/script>/g, "")));
  const js = inline[0][1];
  report("no javascript: URLs", !/javascript:/i.test(html));
  report("no eval / new Function in the page", !/\beval\(|new Function\(/.test(js));
  report("no native confirm()/alert() dialogs", !/\b(confirm|alert)\(/.test(js.replace(/\/\/.*$/gm, "")));
  report("markup builders put no on*= attributes into strings", !/`[^`]*\son(click|load|error|mouse\w+|key\w+)=/.test(js));
}

/* ---------------- file explorer follows the active terminal ---------------- */

const TM = { terms: new Map(), active: null };
const app = (0, eval)("(TM)=>{" +
  "let fsPath = null; const loaded = [];" +
  "function loadFs(p){ loaded.push(p); fsPath = p; }" +
  "function renderTabs(){} function fitActive(){} function setPlanProject(){}" +
  "function $(s){ return {style:{display:'flex'}}; }" +   // Files tab visible
  slice("/* Switching terminal tabs moves the file explorer",
        "function fitActive(force)") +
  "\nreturn {activateTerm, loadFs, loaded, get fsPath(){return fsPath;}};}")(TM);

const mkTab = (id, cwd) => TM.terms.set(id,
  { id, cwd, fsPath: cwd, container: { style: {} }, term: { focus() {} } });

const eq = (name, got, want) =>
  report(name, got === want, `got ${got}, want ${want}`);

const A = "/Users/x/proj-a", B = "/Users/x/proj-b";
mkTab("a", A); mkTab("b", B);

app.activateTerm("a");
eq("opening a session points the explorer at its root", app.fsPath, A);
app.activateTerm("b");
eq("switching sessions jumps to the other root", app.fsPath, B);
app.activateTerm("a");
eq("switching back returns to the first root", app.fsPath, A);

app.loadFs(A + "/scripts/estimation");     // browse deeper, then leave and return
app.activateTerm("b");
eq("leaving goes to the other root, not the subdir", app.fsPath, B);
app.activateTerm("a");
eq("returning remembers where you had browsed", app.fsPath, A + "/scripts/estimation");

mkTab("c", null);
app.activateTerm("c");
eq("a tab with no cwd leaves the explorer alone", app.fsPath, A + "/scripts/estimation");

const before = app.loaded.length;
app.activateTerm("c");
eq("re-activating the same tab does not reload", app.loaded.length, before);

/* ---------------- command palette: fuzzy match + ranking ---------------- */
{
  const { fuzzy, rankItems } = (0, eval)("(esc)=>{" +
    slice("function fuzzy(q, s){", "/* ---------- palette UI") + "\nreturn {fuzzy, rankItems};}")(esc);
  report("fuzzy: subsequence matches", !!fuzzy("rsm", "Resume current session") && !fuzzy("xyz", "Resume"));
  report("fuzzy: case-insensitive, marks the hits",
         fuzzy("RES", "resume").html === "<mark>r</mark><mark>e</mark><mark>s</mark>ume", fuzzy("RES", "resume").html);
  report("fuzzy: escapes before marking", (() => {
    const h = fuzzy("<b", "a <b> tag").html;
    return h === "a <mark>&lt;</mark><mark>b</mark>&gt; tag";
  })(), fuzzy("<b", "a <b> tag").html);
  report("fuzzy: no query = escaped label, score 0",
         fuzzy("", "<i>x</i>").html === "&lt;i&gt;x&lt;/i&gt;" && fuzzy("", "a").score === 0);
  report("fuzzy: a contiguous hit beats a scattered one",
         fuzzy("sess", "Focus Session pane").score > fuzzy("sess", "show settings search").score);
  report("fuzzy: a prefix hit beats a mid-word hit",
         fuzzy("the", "Theme: Dracula").score > fuzzy("the", "Toggle the header").score);
  const items = [
    {label: "Toggle right column"},
    {label: "Theme: Dracula"},
    {label: "old session", mtime: 1},
    {label: "new session", mtime: 9},
  ];
  const r1 = rankItems(items, "the").map(x => x.label);
  eq("rank: prefix hit first", r1[0], "Theme: Dracula");
  const r2 = rankItems(items, "session").map(x => x.label);
  eq("rank: equal scores fall back to recency", r2.join("|"), "new session|old session");
  eq("rank: empty query keeps everything, newest first", rankItems(items, "").map(x => x.label)[0], "new session");
  eq("rank: caps the list", rankItems(Array.from({length: 80}, (_, i) => ({label: "a" + i})), "a").length, 50);
}

/* ---------------- live-follow: tail splice ---------------- */
{
  const { spliceTail } = (0, eval)(slice("const spliceTail =", "const sessMtime =") + ";({spliceTail})");
  const old = [{i: 0}, {i: 1}, {i: 2, result: null}, {i: 3}];
  const r = {start: 2, entries: [{i: 2, result: "done"}, {i: 3}, {i: 4}]};
  const next = spliceTail(old, r);
  eq("tail splice keeps the head and appends", JSON.stringify(next.map(x => x.i)), "[0,1,2,3,4]");
  eq("tail splice replaces the overlap", next[2].result, "done");
  eq("tail splice does not mutate the old array", old.length, 4);
}

/* ---------------- turns, context attribution, cache timer ---------------- */
{
  const T = (0, eval)(slice("/* ---------- turns ----", "/* ---------- end of the pure turn helpers") +
    ";({isPrompt, groupTurns, turnOf, shortModel, turnInfo, turnSummary, contextIn, visibleContext, firstChange})");
  const E = [
    {kind: "hook", ts: "2026-10-01T10:00:00Z", text: "x"},                                   // 0 preamble
    {kind: "user", ts: "2026-10-01T10:00:01Z", text: "do it", tok: 2},                       // 1 prompt
    {kind: "thinking", ts: "2026-10-01T10:00:02Z", text: "hm", tok: 5},
    {kind: "tool", ts: "2026-10-01T10:00:03Z", name: "Bash", tok: 40, result: "ok"},
    {kind: "user", queued: true, ts: "2026-10-01T10:00:04Z", text: "also this", tok: 3},     // typed mid-turn
    {kind: "user", ts: "2026-10-01T10:00:05Z", text: "<system-reminder>r</system-reminder>"},
    {kind: "assistant", ts: "2026-10-01T10:00:09Z", text: "done", tok: 1},                   // 6 final
    {kind: "command", cmd: "/improve", ts: "2026-10-01T10:01:00Z", text: "<command-name>", tok: 9}, // 7 prompt
    {kind: "meta", sub: "skill", ts: "2026-10-01T10:01:00Z", text: "# Skill", tok: 100},
    {kind: "tool", ts: "2026-10-01T10:01:02Z", name: "Task", agent_id: "a1", is_error: true, tok: 10},
    {kind: "assistant", ts: "2026-10-01T10:01:03Z", text: "partial", tok: 1},
    {kind: "tool", ts: "2026-10-01T10:01:04Z", name: "Read", tok: 30},
    {kind: "command", ts: "2026-10-01T10:02:00Z", out: "stdout"},                            // output: not a prompt
  ];
  const turns = T.groupTurns(E);
  eq("turns: preamble + one per prompt", JSON.stringify(turns.map(t => [t.start, t.end, t.prompt])),
     "[[0,1,null],[1,7,1],[7,13,7]]");
  report("queued messages, reminders and command output never open a turn",
         !T.isPrompt(E[4]) && !T.isPrompt(E[5]) && !T.isPrompt(E[12]) && T.isPrompt(E[7]));
  eq("turnOf finds the holding turn", [0, 1, 6, 7, 12].map(i => T.turnOf(turns, i)).join(","), "0,1,1,2,2");
  eq("short model names", T.shortModel("claude-opus-5-5") + " " + T.shortModel("claude-haiku-4-5-20251001"), "opus-5.5 haiku-4.5");
  const s = {entries: E, context_series: [
    {entry: 2, context: 1000, output: 50, model: "claude-opus-5-5"},
    {entry: 6, context: 1300, output: 20, model: "claude-opus-5-5"},
    {entry: 8, context: 2000, output: 10, model: "claude-sonnet-5-5"}],
    context: [{entry: 0, cat: "claude-md", label: "/u/.claude/CLAUDE.md", tok: 500},
              {entry: 8, cat: "mentions", label: "a.py", tok: 70},
              {entry: 0, cat: "system", label: "System prompt", tok: 4000},
              {entry: 7, cat: "system", label: "System prompt", tok: 4100}]};
  const i1 = T.turnInfo(s, turns[1]), i2 = T.turnInfo(s, turns[2]);
  eq("turn summary in words", T.turnSummary(s, turns[1]), "Ran 1 command");
  eq("turn summary: unnamed read + agent", T.turnSummary(s, turns[2]), "Read 1 file, started 1 agent");
  eq("turn summary names the one file", T.turnSummary({entries: [
    {kind: "tool", name: "Edit", input: {file_path: "/a/b/server.py"}},
    {kind: "tool", name: "Read", input: {file_path: "/a/x.py"}}, {kind: "tool", name: "Read", input: {file_path: "/a/y.py"}}]},
    {start: 0, end: 3}), "Read 2 files, edited server.py");
  eq("turn summary without tools", T.turnSummary({entries: [{kind: "assistant"}]}, {start: 0, end: 1}), "Answered");
  eq("turn counts", [i1.thinking, i1.tools, i1.messages, i1.errors].join(","), "1,1,1,0");
  eq("the last text is the final answer", i1.final, 6);
  eq("a text followed by tools is not an answer", i2.final, -1);
  eq("agents and their errors are counted", [i2.agents, i2.errors].join(","), "1,1");
  eq("turn tokens come from its requests", [i1.out, i1.ctx, i1.models.join()].join(" "), "70 1300 opus-5.5");
  eq("turn wall time", i1.secs, 8);
  const c2 = T.contextIn(s, turns[2].start, turns[2].end);
  eq("context in a turn: attachments one by one", c2.items.map(x => x.label).join(), "a.py,System prompt");
  eq("context in a turn: entries grouped", c2.groups.map(g => g.cat + ":" + g.tok + "/" + g.n).join(), "user:9/1,skills:100/1,tools:40/2,text:1/1");
  const vc = T.visibleContext(s, turns);
  const sys = vc.cats.find(c => c.cat === "system");
  eq("a re-sent attachment counts once (the latest)", sys.rows.length + ":" + sys.tok, "1:4100");
  eq("visible total vs the last request", vc.real, 2000);
  const cut = {...s, entries: [...E.slice(0, 9), {kind: "compact", text: "x"}, ...E.slice(9)]};
  eq("only what follows the last compaction is visible",
     T.visibleContext(cut, T.groupTurns(cut.entries)).cats.map(c => c.cat).join(), "tools,text");
  eq("firstChange: the overlap that differs", T.firstChange([{a: 1}, {a: 2}, {a: 3}], [{a: 1}, {a: 9}, {a: 3}, {a: 4}], 0), 1);
  eq("firstChange: pure append", T.firstChange([{a: 1}], [{a: 1}, {a: 2}], 0), 1);

  const { cacheState } = (0, eval)(slice("function cacheState(t, now){", "function tickCache(){") + ";({cacheState})");
  const t0 = Date.parse("2026-10-01T10:00:00Z");
  eq("cache: warm within the TTL", cacheState({last_request_ts: "2026-10-01T10:00:00Z", cache_ttl: 300}, t0 + 60e3).left, 240);
  report("cache: cold after it", cacheState({last_request_ts: "2026-10-01T10:00:00Z", cache_ttl: 300}, t0 + 400e3).left < 0);
  report("cache: unknown without a TTL", cacheState({last_request_ts: "2026-10-01T10:00:00Z"}, t0) === null);
}

/* ---------------- usage heatmap levels ---------------- */
{
  const { heatLevel } = (0, eval)(slice("function heatLevel(values){", "function historyHtml(h){") + ";({heatLevel})");
  const lv = heatLevel([0, 10, 20, 30, 40, 0]);
  eq("heat: idle day is level 0", lv(0), 0);
  eq("heat: quartiles of active days", [10, 20, 30, 40].map(lv).join(), "1,2,3,4");
  eq("heat: all idle", heatLevel([0, 0])(0), 0);
}

/* ---------------- hook events: "now doing" per project ---------------- */
{
  const { applyEvents, nowMap, projectForCwd } = (0, eval)(
    slice('/* ---------- "now doing" per project', "/* The tree is one tab stop") + ";({applyEvents, nowMap, projectForCwd})");
  const P = [{slug: "a", path: "/u/x/proj"}, {slug: "b", path: "/u/x/proj/sub"}, {slug: "w", path: "C:\\u\\win"}];
  eq("cwd maps to its project", (projectForCwd(P, "/u/x/proj") || {}).slug, "a");
  eq("deepest project wins", (projectForCwd(P, "/u/x/proj/sub/deep") || {}).slug, "b");
  eq("sibling with a shared prefix does not match", projectForCwd(P, "/u/x/project2"), null);
  eq("Windows cwd maps", (projectForCwd(P, "C:\\u\\win\\src") || {}).slug, "w");
  let st = applyEvents({}, [{hook_event_name: "PreToolUse", cwd: "/u/x/proj", tool_name: "Bash"}]);
  eq("PreToolUse sets the tool", JSON.stringify(nowMap(P, st.now)), '{"a":"Bash"}');
  for (const end of ["PostToolUse", "Stop", "SessionEnd"]) {
    const s2 = applyEvents(st.now, [{hook_event_name: end, cwd: "/u/x/proj"}]);
    eq(end + " clears it", JSON.stringify(s2.now), "{}");
  }
  eq("applyEvents does not mutate the previous state", JSON.stringify(st.now), '{"/u/x/proj":"Bash"}');
  const al = applyEvents({}, [
    {hook_event_name: "PermissionRequest", cwd: "/u/x/proj"},
    {hook_event_name: "Notification", cwd: "/u/x/proj", notification_type: "idle_prompt"},
    {hook_event_name: "Notification", cwd: "/u/x/proj", notification_type: "auth_success"},
    {hook_event_name: "SubagentStart", cwd: "/u/x/proj"}]).alerts;
  eq("permission and idle notifications alert, others do not", al.length, 2);
  eq("an event from an unknown folder shows no chip",
     JSON.stringify(nowMap(P, applyEvents({}, [{hook_event_name: "PreToolUse", cwd: "/elsewhere", tool_name: "Read"}]).now)), "{}");
}

/* ---------------- OS notifications: which events, how often ---------------- */
{
  const { osNotices } = (0, eval)(
    slice('/* ---------- "now doing" per project', "/* The tree is one tab stop") + ";({osNotices})");
  const last = {};
  const ev = [{hook_event_name: "PermissionRequest", cwd: "/p", session_id: "s"},
              {hook_event_name: "Stop", cwd: "/p", session_id: "s"},
              {hook_event_name: "PreToolUse", cwd: "/p", session_id: "s"},
              {hook_event_name: "Stop", cwd: "/p", session_id: "s"}];
  eq("waiting and finished notify, once each", JSON.stringify(osNotices(ev, last, 1e6).map(n => n.kind)),
     '["waiting","finished"]');
  eq("the same session within 15 s stays quiet", osNotices(ev, last, 1e6 + 5000).length, 0);
  eq("after 15 s it notifies again", osNotices(ev, last, 1e6 + 16000).length, 2);
  eq("another session is not rate-limited by the first",
     osNotices([{hook_event_name: "Stop", cwd: "/q", session_id: "t"}], last, 1e6 + 16500).length, 1);
}

/* ---------------- session-end card + export redaction ---------------- */
{
  const fmtTok = n => String(n || 0);
  const { endCardHtml } = (0, eval)("(function(esc, fmtTok){" +
    slice("function endCardHtml(s){", "async function showEndCard(id){") + "; return {endCardHtml};})")(esc, fmtTok);
  const base = {project: "paper", started: 0, ended: 600, tokens: {output_tokens: 5}, tools: 3,
                title: "fix <b>tables</b>", cost_usd: null, session_id: "s1", improve: null};
  const h = endCardHtml(Object.assign({}, base, {
    git: {files: [{path: "a.R", added: "3", removed: "1"}, {path: "img.png", added: "-", removed: "-"}],
          commits: ["abc1234 add y"]},
    plan: {done: 2, total: 3, ticked: 1}}));
  report("card sums the diff (binary files count as 0)", h.includes("+3") && h.includes("−1"), h);
  report("card shows ticks and minutes", h.includes("+1</b> ticked") && h.includes("10 min"), h);
  report("card escapes the session title", h.includes("fix &lt;b&gt;") && !h.includes("<b>tables"), h);
  report("no cost line without Claude Code's figure", !h.includes("cost"), h);
  report("no git: said plainly", endCardHtml(Object.assign({}, base, {git: null, plan: null}))
         .includes("Not a git repository"));
  report("no transcript: no fake zeros", !endCardHtml(Object.assign({}, base, {git: null, tokens: null}))
         .includes("output"));
  const { redactHome } = (0, eval)(slice("function redactHome(html){", "const EXPORT_DLG") + ";({redactHome})");
  eq("home paths are redacted (mac, linux, windows)",
     redactHome('/Users/pierre/x /home/ann/y C:\\Users\\Bob\\z'), "~/x ~/y ~\\z");
  eq("other paths are left alone", redactHome("/opt/Users/x and /usr/lib"), "/opt/Users/x and /usr/lib");
  eq("the user name goes too (ls -l owner column)",
     redactHome("/Users/pierre/x\n-rw-r--r-- 1 pierre staff"), "~/x\n-rw-r--r-- 1 user staff");
}

/* ---------------- 💬 Comment follows the preview in front ---------------- */
{
  const btn = {style: {display: "none"}}, files = {style: {display: "none"}};
  const fake = {style: {}, removeAttribute(){}, classList: {toggle(){}, remove(){}, add(){}}};
  const $ = s => s === "#vizReview" ? btn : s === "#filesview" ? files : fake;
  const IMG_URL = /\.(png|jpe?g|gif|webp|svg)(&|$)/i;
  const previewWrap = () => null, previewImg = () => fake;
  const api = (0, eval)("(function($, IMG_URL, previewWrap, previewImg){"
    + slice("const REVIEWABLE = {};", "const isPdfName =")
    + "; return {setPreview, clearPreview, REVIEWABLE, syncReviewBtn};})")($, IMG_URL, previewWrap, previewImg);
  const url = p => "/api/fs/file?path=" + encodeURIComponent(p) + "&t=1";
  api.setPreview("#fsframe", "#fspdf", url("/u/p/paper.pdf"), true, false);
  eq("a PDF in Files is commentable", api.REVIEWABLE["#fsframe"], "/u/p/paper.pdf");
  eq("…but the button follows the tab in front (Viz)", btn.style.display, "none");
  files.style.display = "flex"; api.syncReviewBtn();
  eq("Files in front: the button shows", btn.style.display, "");
  api.setPreview("#fsframe", "#fspdf", url("/u/p/fig one.png"), false, false);
  eq("an image path survives the URL round trip", api.REVIEWABLE["#fsframe"], "/u/p/fig one.png");
  api.setPreview("#fsframe", "#fspdf", url("/u/p/notes.md"), false, false);
  eq("a markdown file is not commentable", api.REVIEWABLE["#fsframe"], null);
  eq("…and hides the button", btn.style.display, "none");
  api.setPreview("#fsframe", "#fspdf", url("/u/p/a.png"), false, false);
  api.clearPreview("#fsframe", "#fspdf");
  eq("clearing the preview hides it again", btn.style.display, "none");
}

/* ---------------- polling: unchanged data -> no repaint ---------------- */
{
  const dataStamp = (0, eval)(slice("const dataStamp =", "poll(async () => {   // badges") + ";dataStamp");
  const a = [{slug: "p", mtime: 1, sessions: 2}], ss = [{id: "s", mtime: 5}];
  report("same data gives the same stamp (skip renderTree)", dataStamp(a, ss) === dataStamp(JSON.parse(JSON.stringify(a)), [...ss]));
  report("a moved mtime changes the stamp (repaint)", dataStamp(a, ss) !== dataStamp([{slug: "p", mtime: 2, sessions: 2}], ss));
  report("a new session in the open project changes the stamp", dataStamp(a, ss) !== dataStamp(a, [...ss, {id: "t", mtime: 6}]));
  const js = html.match(/<script>([\s\S]*?)<\/script>/)[1];
  report("every timer goes through poll() (paused while hidden)",
         [...js.matchAll(/setInterval\(/g)].every(m => js.slice(m.index, m.index + 90).includes("document.hidden"))
         && /pollAway\(pollEvents, 3000\)/.test(js) && /poll\(liveTick, 3000\)/.test(js));
  // the one exception: with notifications on, events and guard blocks keep
  // being read while hidden, since that is when a notification matters
  report("only opted-in notification polls run while hidden",
         /if\(!document\.hidden \|\| NOTIFY\.on\) fn\(\)/.test(js)
         && [...js.matchAll(/\npollAway\((\w+)/g)].map(m => m[1]).sort().join() === "pollEvents,pollGuards");
}

/* ---------------- memory: front matter + cross-file links ---------------- */
{
  const { memFront, memLinks, memLint } = (0, eval)("esc => {" +
    slice("/* ---------- memory ---------- */", "async function showMemory") + "\nreturn {memFront, memLinks, memLint};}")(esc);
  {
    const P = [["MEMORY.md", "- [A](a.md)\n- [[b-slug]]"],
               ["a.md", "---\nname: a\ndescription: d\nmetadata:\n  type: user\n---\nsee [[c]] and `[[code]]`"],
               ["b.md", "---\nname: b-slug\ndescription: d\ntype: project\n---\nx"],
               ["orphan.md", "no front matter, links [[a]]"]]
      .map(([name, content]) => ({f: {name, content}, ...memFront(content)}));
    const L = memLint(P);
    eq("lint: a file outside the index", L.unindexed.join(), "orphan.md");
    eq("lint: missing front matter", L.nofront.join(), "orphan.md");
    eq("lint: an unwritten link is listed, code is skipped", JSON.stringify(L.unwritten), '[{"from":"a.md","to":"c"}]');
  }
  const f = memFront("---\nname: no-trailer\ndescription: \"omit it\"\nmetadata:\n  type: feedback\n---\n\nbody [[x]]\n");
  eq("front matter: quoted value", f.meta.description, "omit it");
  eq("front matter: nested key lifted", f.meta.type, "feedback");
  eq("front matter: YAML escapes decoded", memFront('---\ndescription: "no \\"X\\" here"\n---\n').meta.description, 'no "X" here');
  eq("front matter: empty parent key dropped", "metadata" in f.meta, false);
  eq("front matter: body keeps the rest", f.body.trim(), "body [[x]]");
  eq("no front matter: body untouched", memFront("# hi\n---\n").body, "# hi\n---\n");
  const files = { "no-trailer": "no-trailer.md", "no-trailer.md": "no-trailer.md" };
  const out = memLinks(md("see [[no-trailer]], [the rule](no-trailer.md), [[ghost]] and `[[no-trailer]]`"), files);
  eq("wikilink resolves", (out.match(/data-mem="no-trailer.md"/g) || []).length, 2);
  eq("unknown wikilink is marked missing", out.includes('class="memref missing"'), true);
  eq("code span left alone", out.includes("<code>[[no-trailer]]</code>"), true);
}

/* ---------------- context chart: stacked + clickable ---------------- */
{
  const { renderChart } = (0, eval)(
    "const document = {createElement: () => ({})}, fmtTok = String;" +
    slice("function renderChart(series, onJump)", "/* ---------- memory ---------- */") + "\n({renderChart});");
  const series = [{context: 100, cache_read: 60, cache_creation: 30, input: 10, output: 5, entry: 7},
                  {context: 50, cache_read: 50, cache_creation: 0, input: 0, output: 1, entry: 9, compaction: true}];
  let jumped = null;
  const w = renderChart(series, i => { jumped = i; });
  eq("one bar group per request", (w.innerHTML.match(/class="cbar/g) || []).length, 2);
  eq("zero-height segments skipped", (w.innerHTML.match(/<rect class="s-/g) || []).length, 4);
  w.onclick({ target: { closest: () => ({ dataset: { i: "1" } }) }, preventDefault() {} });
  eq("click jumps to the request's first timeline row", jumped, 9);
}

/* ---------------- terminal input ordering ---------------- */
// fetch that resolves after a random delay: without the queue, bodies land
// out of order (that was the scrambled key-repeat bug)
(async () => {
  const got = [];
  const io = (0, eval)(`(function(fetch, authHeaders, strToB64){` +
    slice("function sendInput(t, s){", "/* Keep the PTY's size") +
    `; return {sendInput}; })`)(
      (url, o) => new Promise(r => setTimeout(() => {
        got.push(JSON.parse(o.body).data); r({ok: true});
      }, Math.random() * 8)),
      h => h, s => s);
  const t = {id: "t1"};
  const typed = "the quick brown fox jumps over the lazy dog 0123456789";
  let last;
  for (const ch of typed) { last = io.sendInput(t, ch); await new Promise(r => setTimeout(r, Math.random() * 3)); }
  await last; while (t.inFlight) await t.inFlight;
  eq("terminal input arrives in order", got.join(""), typed);
  report("fast typing is coalesced into fewer requests", got.length < typed.length, `${got.length} requests`);

  // Failed requests must stop the queue, report failure to callers (including
  // the add-on installer), and never replay text after an explicit resume.
  const notices = [], sent = [];
  let respond;
  const failedIO = (0, eval)(`(function(fetch, authHeaders, strToB64, toast){` +
    slice("function sendInput(t, s){", "/* Keep the PTY's size") +
    `; return {sendInput}; })`)(
      (url, o) => { sent.push(JSON.parse(o.body).data); return new Promise(r => { respond = r; }); },
      h => h, s => s, (message, opts) => { notices.push({message, opts}); return {isConnected: true}; });
  const failedTerm = {id: "t2", term: {focus(){}}};
  const failedWrite = failedIO.sendInput(failedTerm, "first");
  failedIO.sendInput(failedTerm, "queued");
  respond({ok: false, status: 504, json: async () => ({error: "blocked"})});
  eq("failed input returns false", await failedWrite, false);
  eq("failure discards queued typing", failedTerm.inbuf, "");
  eq("typing stays paused after failure", await failedIO.sendInput(failedTerm, "more"), false);
  eq("queued input never sent", sent.join(""), "first");
  report("input failure is visible", notices.length === 1 && notices[0].message.includes("blocked"));
  failedTerm.inputNotice.isConnected = false;
  await failedIO.sendInput(failedTerm, "discard this too");
  eq("dismissed pause warning can be recovered by typing", notices.length, 2);
  notices[0].opts.action.run();
  const resumed = failedIO.sendInput(failedTerm, "fresh");
  respond({ok: true});
  eq("explicit resume accepts fresh input", await resumed, true);
  eq("resume does not replay discarded typing", sent.join(""), "firstfresh");

  const timedIO = (0, eval)(`(function(fetch, authHeaders, strToB64, toast, setTimeout){` +
    slice("function sendInput(t, s){", "/* Keep the PTY's size") +
    `; return {sendInput}; })`)(
      (url, o) => new Promise((resolve, reject) => o.signal.addEventListener("abort", () => {
        const e = new Error("aborted"); e.name = "AbortError"; reject(e);
      })), h => h, s => s, (message, opts) => notices.push({message, opts}),
      fn => setTimeout(fn, 1));
  const timedTerm = {id: "t3"};
  eq("unresponsive request is aborted", await timedIO.sendInput(timedTerm, "x"), false);
  report("timeout pauses typing and releases queue", timedTerm.inputPaused && !timedTerm.inFlight);

  // A slow filesystem gets at most one scan; warnings preserve the preview
  // and disappear after recovery even when the file list has not changed.
  const warning = {hidden: true, textContent: ""};
  let scans = 0, complete;
  const viz = (0, eval)(`(function(api, $, vizOverride, dataStamp, renderStatus){
    let vizRefreshing = false, vizStamp = "same", vizDir = "", vizPinned = null,
        vizActive = null, vizGraphOffer = null;
    const addonInstalled = () => false;` +
    slice("async function refreshViz(){", "/* a project without graphify-out") +
    `; return refreshViz; })`)(
      () => { scans++; return new Promise((resolve, reject) => { complete = {resolve, reject}; }); },
      () => warning, () => "", () => "same", () => {});
  const pendingScan = viz();
  await viz();
  eq("viz scans do not overlap", scans, 1);
  complete.reject(new Error("Interrupted system call"));
  await pendingScan;
  report("viz failure shows persistent warning", !warning.hidden && warning.textContent.includes("Interrupted"));
  const recovered = viz();
  complete.resolve({dir: "/viz", files: []});
  await recovered;
  report("unchanged successful scan clears warning", warning.hidden);

  // Beginners' help: every ? button names a glossary entry, and every tour
  // step points at an element that exists (a renamed id would skip a step)
  const GLOSS = (0, eval)("(" + slice("const GLOSS = {", "document.addEventListener(\"click\", e => {")
    .replace("const GLOSS = ", "").trim().replace(/;$/, "") + ")");
  const glUsed = [...html.matchAll(/data-gl="([a-z]+)"/g)].map(m => m[1]);
  const glMissing = glUsed.filter(k => !GLOSS[k]);
  report("every ? button has a glossary entry", glUsed.length > 5 && !glMissing.length, glMissing.join(", "));
  const tourIds = [...slice("const TOUR = [", "let tourAt").matchAll(/\["#([\w-]+)"/g)].map(m => m[1]);
  const tourMissing = tourIds.filter(id => !html.includes(`id="${id}"`));
  report("every tour step targets an existing element", tourIds.length === 8 && !tourMissing.length, tourMissing.join(", "));

  console.log(fails ? `\n${fails} failed` : `\nall passed`);
  process.exit(fails ? 1 : 0);
})();
