const pptxgen = require("pptxgenjs");
const path = require("path");
const { applyTheme } = require("C:/Users/USER/AppData/Roaming/Claude/local-agent-mode-sessions/skills-plugin/0ecab2d0-fe48-45a1-911d-694c54816234/bc98bf09-1a45-40a7-946a-9b9a054545b3/skills/pptx/scripts/apply_theme.js");

const REPO = "D:/VIT/Git Repo/git-actions-optimizer"; // run: NODE_PATH=<node_modules with pptxgenjs and jszip> node docs/presentation/build_deck.js
const OUT = REPO + "/docs/presentation/GHA_Optimizer_Guide_Review.pptx";
const IMG = REPO + "/docs/presentation/images/";

const THEME = {
  name: "Guide Review",
  headFontFace: "Cambria",
  bodyFontFace: "Calibri",
  colors: {
    dk1: "1B2432", lt1: "FFFFFF", dk2: "0F3D3E", lt2: "EEF3F2",
    accent1: "0F766E", accent2: "D97706", accent3: "B42318", accent4: "15803D", accent5: "64748B", accent6: "334155",
    hlink: "0F766E", folHlink: "64748B",
  },
};
// hex copies for options that only take hex (chart colours, shadows)
const HEX = { ink: "1B2432", teal: "0F766E", deep: "0F3D3E", amber: "D97706", red: "B42318", green: "15803D", slate: "64748B", pale: "EEF3F2", grid: "D5DEDC", white: "FFFFFF" };

const pres = new pptxgen();
pres.layout = "LAYOUT_16x9"; // 10 x 5.625 in
pres.theme = { headFontFace: THEME.headFontFace, bodyFontFace: THEME.bodyFontFace };
pres.title = "GitHub Actions Performance Optimizer - guide review";
pres.author = "Akshat Singh, Aditya Choubey";
pres.company = "VIT";
const C = pres.SchemeColor;

// ---------------------------------------------------------------- layouts
const FOOT = "GitHub Actions Performance Optimizer  |  guide review, 16 Oct 2026";
pres.defineSlideMaster({ title: "TITLE", background: { color: C.text2 }, objects: [] });
pres.defineSlideMaster({
  title: "CONTENT", background: { color: C.background1 },
  objects: [
    { text: { text: FOOT, options: { x: 0.5, y: 5.28, w: 7, h: 0.25, fontSize: 10, color: C.accent5, margin: 0 } } },
    { placeholder: { options: { name: "title", type: "title", x: 0.5, y: 0.3, w: 9, h: 0.7, fontSize: 30, bold: true, color: C.text1, margin: 0, valign: "middle", align: "left" }, text: "" } },
  ],
  slideNumber: { x: 9.0, y: 5.28, w: 0.5, h: 0.25, fontSize: 10, color: C.accent5, align: "right" },
});
pres.defineSlideMaster({
  title: "CLOSE", background: { color: C.text2 },
  objects: [{ placeholder: { options: { name: "title", type: "title", x: 0.5, y: 0.3, w: 9, h: 0.7, fontSize: 30, bold: true, color: C.background1, margin: 0, valign: "middle", align: "left" }, text: "" } }],
});

// ---------------------------------------------------------------- helpers
const takeaway = (s, text) => s.addText(text, { x: 0.5, y: 1.0, w: 9, h: 0.4, fontSize: 16, italic: true, color: C.accent1, margin: 0, isTextBox: true, objectName: "takeaway" });
const shadow = () => ({ type: "outer", color: "000000", opacity: 0.12, blur: 6, offset: 2, angle: 90 });
function card(s, x, y, w, h, big, label, color, bigSize = 34) {
  s.addShape(pres.ShapeType.roundRect, { x, y, w, h, fill: { color: C.background2 }, line: { color: C.background2 }, rectRadius: 0.08, shadow: shadow(), objectName: "card" });
  s.addText(big, { x: x + 0.15, y: y + 0.12, w: w - 0.3, h: h * 0.5, fontSize: bigSize, bold: true, color, fontFace: "Cambria", margin: 0, valign: "middle", isTextBox: true });
  s.addText(label, { x: x + 0.15, y: y + h * 0.58, w: w - 0.3, h: h * 0.38, fontSize: 12, color: C.text1, margin: 0, valign: "top", isTextBox: true });
}
function bullets(s, items, x, y, w, h, size = 14, color = C.text1) {
  const runs = items.map((t, i) => ({ text: t, options: { bullet: true, breakLine: i < items.length - 1, paraSpaceAfter: 6 } }));
  s.addText(runs, { x, y, w, h, fontSize: size, color, margin: 0, valign: "top", isTextBox: true });
}
const chartBase = (title) => ({
  showTitle: true, title, titleFontSize: 12, titleColor: HEX.ink, titleFontFace: "+mn-lt",
  catAxisLabelColor: HEX.ink, valAxisLabelColor: HEX.slate, catAxisLabelFontFace: "+mn-lt", valAxisLabelFontFace: "+mn-lt",
  catAxisLabelFontSize: 11, valAxisLabelFontSize: 10, valGridLine: { color: HEX.grid, size: 0.5 }, catGridLine: { style: "none" },
  dataLabelFontFace: "+mn-lt", dataLabelFontSize: 11, dataLabelColor: HEX.ink, legendFontFace: "+mn-lt", legendFontSize: 11, legendColor: HEX.ink,
});
let n = 0;
const declared = new Set(["Opening"]);
function content(title, section, notes) {
  if (!declared.has(section)) { pres.addSection({ title: section }); declared.add(section); }
  const s = pres.addSlide({ masterName: "CONTENT", sectionTitle: section });
  s.addText(title, { placeholder: "title" });
  s.addNotes(notes);
  n++;
  return s;
}

// ================================================================ 1 title
pres.addSection({ title: "Opening" });
{
  const s = pres.addSlide({ masterName: "TITLE", sectionTitle: "Opening" });
  s.addText("GitHub Actions Performance Optimizer", { x: 0.6, y: 0.5, w: 8.8, h: 1.5, fontSize: 40, bold: true, color: C.background1, fontFace: "Cambria", margin: 0, isTextBox: true });
  s.addText("A machine-learning, dependency-graph-aware study of CI/CD pipelines, evaluated on real workflow history", { x: 0.6, y: 2.1, w: 8.4, h: 0.8, fontSize: 18, color: "CFE3E0", margin: 0, isTextBox: true });
  const steps = ["Repository", "Graph", "Features", "Predictions", "Scheduler", "Proposed workflow"];
  steps.forEach((t, i) => {
    const x = 0.6 + i * 1.5;
    s.addShape(pres.ShapeType.roundRect, { x, y: 3.15, w: 1.32, h: 0.55, fill: { color: i === 5 ? HEX.amber : "1E5557" }, line: { color: "2F7A7C" }, rectRadius: 0.08, objectName: "chain-" + t });
    s.addText(t, { x, y: 3.15, w: 1.32, h: 0.55, fontSize: 12, bold: true, color: C.background1, align: "center", valign: "middle", margin: 0, isTextBox: true });
    if (i < 5) s.addText(">", { x: x + 1.32, y: 3.15, w: 0.18, h: 0.55, fontSize: 14, bold: true, color: "CFE3E0", align: "center", valign: "middle", margin: 0, isTextBox: true });
  });
  s.addText("Progress review with Prof. Pushpa Lakshmi R, SCOPE", { x: 0.6, y: 4.05, w: 8.8, h: 0.35, fontSize: 16, color: C.background1, margin: 0, isTextBox: true });
  s.addText("Akshat Singh (23BKT0005)   Aditya Choubey (23BCE2274)   BCSE497J, VIT   Friday 16 October 2026", { x: 0.6, y: 4.45, w: 8.8, h: 0.35, fontSize: 13, color: "CFE3E0", margin: 0, isTextBox: true });
  s.addText("Every figure is labelled ESTIMATED or MEASURED; interim figures are as of 14 October 2026", { x: 0.6, y: 5.05, w: 8.8, h: 0.3, fontSize: 11, italic: true, color: "9FC3BF", margin: 0, isTextBox: true });
  s.addNotes("Simple opening. Our aim was a system that reads the history of a GitHub Actions pipeline, predicts which jobs fail or run long, and re-orders the pipeline to make CI faster. Today I will show what was built, what the measurements say (including the negative results), and what is still open. I will say clearly which numbers are final and which are interim.");
}

// ================================================================ 2 where it stands
{
  const s = content("Where the project stands", "Summary",
    "One slide of the whole story. We built the full chain and tested it on nine real repositories. The honest result is mixed: the models help only in some repositories, our improvement attempt missed the bar we set beforehand, and scheduling is not faster by default. The solid contributions are the evaluation method, the failure triage and a working, tested system.");
  takeaway(s, "A working system, and an honest evaluation that is mostly negative on speed");
  const items = [
    ["1", "Models beat a naive guess only in some repositories", "Significant gain in fastapi and pydantic; never significantly worse elsewhere"],
    ["2", "Our improved model missed the bar we set in advance", "2 of 9 repositories beaten; the pre-registered bar was 3"],
    ["3", "Scheduling is not faster by default", "0% estimated; a measured -15% critical path on one simple pipeline"],
    ["4", "Real failures follow patterns", "About half of failed jobs in poetry and fastapi are repeat alerts, not new problems"],
  ];
  items.forEach(([num, head, sub], i) => {
    const col = i % 2, row = Math.floor(i / 2);
    const x = 0.5 + col * 4.6, y = 1.6 + row * 1.75;
    s.addShape(pres.ShapeType.roundRect, { x, y, w: 4.4, h: 1.55, fill: { color: C.background2 }, line: { color: C.background2 }, rectRadius: 0.08, shadow: shadow(), objectName: "finding-card" });
    s.addShape(pres.ShapeType.ellipse, { x: x + 0.2, y: y + 0.2, w: 0.5, h: 0.5, fill: { color: C.accent1 }, line: { color: C.accent1 }, objectName: "finding-number" });
    s.addText(num, { x: x + 0.2, y: y + 0.2, w: 0.5, h: 0.5, fontSize: 16, bold: true, color: C.background1, align: "center", valign: "middle", margin: 0, isTextBox: true });
    s.addText(head, { x: x + 0.85, y: y + 0.15, w: 3.4, h: 0.65, fontSize: 15, bold: true, color: C.text1, margin: 0, valign: "middle", isTextBox: true });
    s.addText(sub, { x: x + 0.2, y: y + 0.85, w: 4.0, h: 0.62, fontSize: 13, color: C.accent6, margin: 0, valign: "top", isTextBox: true });
  });
}

// ================================================================ 3 how it works
{
  const s = content("How the system works", "System",
    "Walk left to right. We read real run history, build a graph linking source files, jobs and cache keys, turn that into features, predict failure, duration and cache misses, schedule the jobs while respecting the needs rules, and write a PROPOSED workflow plus a diff. The original file is never changed. If there is too little real history the system refuses to predict instead of guessing.");
  takeaway(s, "From repository to a reviewable proposal, with real data only");
  const stages = [
    ["Ingest", "runs, jobs, logs, diffs from GitHub"],
    ["Graph", "files, jobs, steps, cache keys, needs"],
    ["Features", "diff reach, history, change type"],
    ["Models", "failure, duration, cache miss"],
    ["Scheduler", "needs-safe, fail-fast order"],
    ["Proposal", "new YAML plus a diff"],
  ];
  stages.forEach(([h, d], i) => {
    const x = 0.5 + i * 1.5;
    s.addShape(pres.ShapeType.roundRect, { x, y: 1.65, w: 1.38, h: 1.55, fill: { color: i === 5 ? C.accent2 : C.accent1 }, line: { color: C.background1 }, rectRadius: 0.08, shadow: shadow(), objectName: "stage-" + h });
    s.addText(h, { x, y: 1.75, w: 1.38, h: 0.45, fontSize: 15, bold: true, color: C.background1, align: "center", margin: 0, isTextBox: true });
    s.addText(d, { x: x + 0.08, y: 2.2, w: 1.22, h: 0.95, fontSize: 12, color: C.background1, align: "center", valign: "top", margin: 0, isTextBox: true });
  });
  card(s, 0.5, 3.5, 2.9, 1.55, "Real data", "Nine repositories; no synthetic values shown as results", C.accent1, 24);
  card(s, 3.55, 3.5, 2.9, 1.55, "Honest history", "A run is visible to later runs only after it finished", C.accent1, 24);
  card(s, 6.6, 3.5, 2.9, 1.55, "Safe output", "Original workflow never edited; needs rules kept", C.accent1, 24);
}

// ================================================================ 4 evidence base
{
  const s = content("The evidence base: real CI history", "Method",
    "Nine real repositories, about 4,600 runs and 72,000 run-and-job rows. Several are very different in size, so intervals are wide for the small ones. The important points are in the right column: we only use information that existed when a run started, we test on later runs, and we wrote the plans for the held-out repositories before looking at their results.");
  takeaway(s, "Nine repositories, about 4,600 runs, about 72,000 job rows");
  s.addChart(pres.charts.BAR, [{ name: "Runs", labels: ["prometheus", "fastapi", "poetry", "dubbo", "cli/cli", "pydantic", "ruff", "uv", "quarkus"], values: [2163, 682, 596, 293, 293, 196, 183, 148, 24] }], {
    x: 0.4, y: 1.5, w: 5.0, h: 3.7, barDir: "bar", chartColors: [HEX.teal], showValue: true, dataLabelPosition: "outEnd", showLegend: false,
    catAxisOrientation: "maxMin", valAxisHidden: true, valGridLine: { style: "none" }, ...chartBase("Runs ingested per repository"),
  });
  bullets(s, [
    "Only information available when a run started is used: history comes from runs that had already finished",
    "Tests always run on later runs (rolling origin), never a random split",
    "Five repositories were never used to make a choice; plans were written first",
    "Results are always shown next to a naive baseline",
  ], 5.7, 1.65, 3.8, 3.3, 14);
}

// ================================================================ 5 models vs baseline
{
  const s = content("Do the models beat a simple guess?", "Results",
    "The baseline is the simplest possible predictor: this job's own recent failure rate. PR-AUC measures how well failures are ranked; higher is better. Our default failure model is clearly better than the baseline in fastapi, and in the held-out pydantic repository, and it is not distinguishable in the others. It is never significantly worse, which is why it is the default. Be careful with the claim: a gain in two repositories is not a general result.");
  takeaway(s, "Only in some repositories, and never significantly worse");
  s.addChart(pres.charts.BAR, [
    { name: "Model (regularised logistic regression)", labels: ["poetry", "fastapi", "prometheus", "dubbo"], values: [0.2047, 0.4187, 0.1732, 0.2141] },
    { name: "Baseline (job's prior failure rate)", labels: ["poetry", "fastapi", "prometheus", "dubbo"], values: [0.1403, 0.3325, 0.1561, 0.2168] },
  ], {
    x: 0.4, y: 1.5, w: 5.6, h: 3.7, barDir: "col", barGrouping: "clustered", chartColors: [HEX.teal, HEX.slate], showValue: true, dataLabelPosition: "outEnd",
    dataLabelFormatCode: "0.00", showLegend: true, legendPos: "b", valAxisMinVal: 0, valAxisMaxVal: 0.5, valAxisMajorUnit: 0.1, valAxisLabelFormatCode: "0.0",
    ...chartBase("Failure PR-AUC (higher is better)"),
  });
  card(s, 6.3, 1.6, 3.2, 1.15, "+0.085", "fastapi: interval excludes 0", C.accent4, 30);
  card(s, 6.3, 2.9, 3.2, 1.15, "+0.110", "pydantic (held out): interval excludes 0", C.accent4, 30);
  s.addText("poetry, prometheus, dubbo: not distinguishable from the baseline", { x: 6.3, y: 4.2, w: 3.2, h: 0.8, fontSize: 12, color: C.accent6, margin: 0, valign: "top", isTextBox: true });
}

// ================================================================ 6 improvement attempt
{
  const s = content("The improvement attempt missed its bar", "Results",
    "We tried to improve the failure model with 15 new features and a stricter, more realistic protocol. Before looking at test results we wrote down the success criterion: beat the baseline in at least 3 of 9 repositories with none worse. We got 2. A pooled model trained on all repositories reached 3, but we designed it after seeing results, so it only counts as a lead. We froze it and tested it on fresh data. After two days of fresh data it wins in 1 of 3 judged repositories; if the final run looked like this it would be rejected. That is not final: the rule needs about two weeks, so the verdict stays open until 26 October.");
  takeaway(s, "Pre-registered criterion not met; the pooled model is not holding up on fresh data");
  card(s, 0.5, 1.6, 2.9, 1.9, "2 of 9", "repositories beaten by the v2 model; the bar was 3 (not met)", C.accent3, 40);
  card(s, 3.55, 1.6, 2.9, 1.9, "3 of 9", "pooled model on old data: exploratory, designed after seeing results", C.accent2, 40);
  card(s, 6.6, 1.6, 2.9, 1.9, "1 of 3", "pooled model on fresh data so far: would be rejected if final", C.accent3, 40);
  s.addText("What this means", { x: 0.5, y: 3.75, w: 9, h: 0.3, fontSize: 16, bold: true, color: C.text1, margin: 0, isTextBox: true });
  bullets(s, [
    "We report the miss as a miss; the rule and the bar were not changed after seeing results",
    "Fresh-data verdicts are fixed for 21 October (logistic default) and 26 October (pooled model); today's numbers are interim",
  ], 0.5, 4.1, 9, 1.1, 13);
}

// ================================================================ 7 calibration
{
  const s = content("A failure score is not a probability", "Results",
    "A model score of 0.91 does not mean a 91 percent chance of failure. We measured this. In every repository the baseline had an equal or lower Brier score, and the model is a little over-confident in its top group, for example fastapi predicted 0.49 and saw 0.37. We tried two recalibrations; neither helped in three of four repositories. So we do not apply a fix. The dashboard shows the score next to the job's own failure rate and the failure rate actually seen on unseen rows with similar scores, and flags scores above anything seen before.");
  takeaway(s, "Recalibrating did not help, so the dashboard reports observed rates instead");
  s.addChart(pres.charts.BAR, [
    { name: "Model", labels: ["poetry", "fastapi", "prometheus", "dubbo"], values: [0.0924, 0.0644, 0.0386, 0.0572] },
    { name: "Baseline", labels: ["poetry", "fastapi", "prometheus", "dubbo"], values: [0.0847, 0.0617, 0.0376, 0.0502] },
  ], {
    x: 0.3, y: 1.5, w: 5.5, h: 3.7, barDir: "col", barGrouping: "clustered", chartColors: [HEX.teal, HEX.slate], showValue: true, dataLabelPosition: "outEnd",
    dataLabelFormatCode: "0.000", showLegend: true, legendPos: "b", valAxisMinVal: 0, valAxisMaxVal: 0.12, valAxisMajorUnit: 0.02, valAxisLabelFormatCode: "0.00", barGapWidthPct: 40,
    ...chartBase("Brier score (lower is better)"), dataLabelFontSize: 9,
  });
  bullets(s, [
    "Top group over-confident: fastapi predicted 0.49, observed 0.37",
    "Platt scaling and an isotonic map did not improve the Brier score in 3 of 4 repositories",
    "poetry's main test job scored 0.91, above any score seen on unseen rows (highest 0.58)",
    "So the dashboard shows base rate and observed rate beside the score",
  ], 5.9, 1.65, 3.6, 3.4, 14);
}

// ================================================================ 8 speed
{
  const s = content("Does scheduling make CI faster?", "Results",
    "The default plan keeps the structure of the workflow, so the estimated saving is zero; we say so. One opt-in option removes a needs link only when we can prove it is safe. On the project's own lint-then-test pipeline we measured it with real runs, six against six, alternating order: the critical path fell from 159.5 to 135.5 seconds, about 15 percent, p equals 0.013, while billable time rose about 4 percent, not significant. That is one simple pipeline. Replayed on the history of nine mature repositories the gain is about zero, because their slow part is the test and build jobs themselves. Skipping jobs the graph says a diff cannot reach saves 12 to 17 percent of billable time in two repositories but would hide 3 to 10 percent of failing runs, so it is an advisory only. A separate real run showed the generated workflow behaves identically.");
  takeaway(s, "Measured on one simple pipeline; about zero on mature ones");
  s.addChart(pres.charts.BAR, [{ name: "Critical path (s)", labels: ["Original workflow", "Safe gate relaxed"], values: [159.5, 135.5] }], {
    x: 0.4, y: 1.5, w: 4.4, h: 3.7, barDir: "col", chartColors: [HEX.slate, HEX.teal], showValue: true, dataLabelPosition: "outEnd", dataLabelFormatCode: "0.0",
    showLegend: false, valAxisMinVal: 0, valAxisMaxVal: 200, valAxisLabelFormatCode: "0", ...chartBase("MEASURED median, 6 real runs each (seconds)"),
  });
  card(s, 5.1, 1.6, 2.1, 1.3, "-15.1%", "critical path, p = 0.013", C.accent4, 26);
  card(s, 7.35, 1.6, 2.15, 1.3, "+4.1%", "billable time, not significant", C.accent2, 26);
  bullets(s, [
    "Default plan: 0% estimated saving; replay on 9 repositories: about 0%",
    "Skipping unreached jobs saves 12-17% but hides 3-10% of failures: advisory only",
    "Generated workflow ran identically on real runs (+0.3%, p = 0.92)",
  ], 5.1, 3.05, 4.4, 2.15, 14);
}

// ================================================================ 9 triage
{
  const s = content("What real CI failures look like", "Results",
    "We classify every failed job from recorded outcomes. In poetry and fastapi about half of all failed jobs are aggregator jobs, the final status job that only repeats an upstream failure, so raw failure counts overstate independent failures by about a factor of two. In four repositories 10 to 25 percent of red runs needed no code change at all: every independent failure was pre-existing, flaky or unrelated. Be honest about limits: flakiness is under-counted because a re-run overwrites the earlier attempt, the classes are heuristics, and nobody has checked them against what developers did.");
  takeaway(s, "About half of failed jobs in poetry and fastapi only repeat an upstream failure");
  s.addChart(pres.charts.BAR, [{ name: "Aggregator share", labels: ["fastapi", "poetry", "dubbo", "uv", "pydantic", "ruff"], values: [51, 50, 45, 32, 30, 28] }], {
    x: 0.4, y: 1.5, w: 5.0, h: 3.7, barDir: "bar", chartColors: [HEX.amber], showValue: true, dataLabelPosition: "outEnd", dataLabelFormatCode: "0\"%\"", showLegend: false,
    catAxisOrientation: "maxMin", valAxisHidden: true, valAxisMinVal: 0, valAxisMaxVal: 65, valGridLine: { style: "none" }, ...chartBase("Failed jobs that are only repeat alerts (%)"),
  });
  card(s, 5.7, 1.6, 3.8, 1.35, "10-25%", "of red runs in four repositories needed no code fix", C.accent1, 30);
  bullets(s, [
    "Classes: repeat alert, flaky, pre-existing, fixed later, persistent, unrelated",
    "Limits: flakiness under-counted; classes are heuristics, not checked against developers",
  ], 5.7, 3.15, 3.8, 2.05, 14);
}

// ================================================================ 10 live test
{
  const s = content("Predicting live, before jobs finish", "Results",
    "This is the prospective test: predictions are written to a tamper-evident ledger when a run is queued, before any job finishes, and judged afterwards. After about two days only uv has enough outcomes: 46 runs, 739 job predictions, 43 failures. Neither model beats the simple baseline there; the old logistic model is significantly worse, the new model is indistinguishable. ruff had no failures, and prometheus cannot be scored because its runs finish before our first poll. This is a demonstration of the method and a small sample, not a significance test. The final evaluation is scheduled for 19 October.");
  takeaway(s, "Interim, after two days: neither model beats the baseline on uv");
  s.addChart(pres.charts.BAR, [{ name: "PR-AUC", labels: ["Baseline", "v2 model", "Old logistic"], values: [0.341, 0.252, 0.154] }], {
    x: 0.4, y: 1.5, w: 4.8, h: 3.7, barDir: "col", chartColors: [HEX.slate, HEX.teal, HEX.amber], showValue: true, dataLabelPosition: "outEnd", dataLabelFormatCode: "0.000",
    showLegend: false, valAxisMinVal: 0, valAxisMaxVal: 0.5, valAxisMajorUnit: 0.1, valAxisLabelFormatCode: "0.0", ...chartBase("uv, live PR-AUC (higher is better)"),
  });
  card(s, 5.5, 1.6, 1.95, 1.25, "46", "runs with outcomes", C.accent1, 28);
  card(s, 7.55, 1.6, 1.95, 1.25, "43", "failing jobs of 739", C.accent1, 28);
  bullets(s, [
    "Old logistic vs baseline: -0.173 (excludes 0); v2: -0.082 (includes 0)",
    "ruff: no failures; prometheus: cannot be scored",
    "A demonstration, not a significance test",
  ], 5.5, 3.05, 4.0, 2.15, 14);
}

// ================================================================ 11 dashboard
{
  const s = content("The dashboard", "Demo",
    "Live demo if possible: start with the poetry dashboard, open Models and probabilities to show the score next to the base rate, then What-if and pick a dependency file. Everything on these pages comes from real runs; the evidence pages simply show rows of the reports we generated. If a repository has too little history the page says why and shows no numbers.");
  takeaway(s, "One page per stage, what-if scoring, and the evidence behind every claim");
  s.addImage({ path: IMG + "dashboard.jpg", x: 0.5, y: 1.55, w: 4.4, h: 2.63, altText: "Dashboard overview for poetry with the job graph and predicted durations" });
  s.addImage({ path: IMG + "whatif.jpg", x: 5.1, y: 1.55, w: 3.5, h: 2.63, altText: "What-if page: impacted jobs after choosing a changed file" });
  bullets(s, [
    "Repository selector; a repository without enough history shows why it declined",
    "Score shown beside the job's base rate and the observed rate; original vs proposed workflow side by side",
  ], 0.5, 4.3, 9, 0.9, 14);
}

// ================================================================ 12 rigour
{
  const s = content("Engineering rigour", "Rigour",
    "Four things to point at. First, reproducibility: the same repository used to give different numbers at different hours because the hour of the day is a model feature and it was read from the clock; scoring time is now an explicit input, and two full runs gave byte-identical output. Second, 428 automated tests. Third, the tamper-evident prediction ledger. Fourth, the system refuses to predict when history is too thin; before we back-filled job logs, five of nine repositories were declined, now three are.");
  takeaway(s, "Same inputs, same numbers; the system refuses to guess");
  card(s, 0.5, 1.6, 4.4, 1.7, "Reproducible", "Scoring time is an explicit input; two full runs were byte-identical", C.accent1, 26);
  card(s, 5.1, 1.6, 4.4, 1.7, "428 tests", "427 pass, 1 skipped; lint clean; fixtures are data, not results", C.accent1, 26);
  card(s, 0.5, 3.5, 4.4, 1.6, "Tamper-evident", "Live predictions are hash-chained; editing one breaks the chain", C.accent1, 26);
  card(s, 5.1, 3.5, 4.4, 1.6, "Refuses to guess", "Declines when real history is too thin: 3 of 9 repositories now", C.accent1, 26);
}

// ================================================================ 13 open
{
  const s = content("What is still open, and when", "Next steps",
    "Everything final is scheduled and automated. Thursday evening the interim results are refreshed so today's numbers are not stale. The live test ends on 19 October, the logistic-default verdict is on 21 October, the pooled-model verdict on 26 October. Be upfront about the limits on the right.");
  takeaway(s, "Final verdicts are fixed by the pre-registered rules");
  const rows = [
    ["15 Oct", "Automated refresh of the interim figures in this deck"],
    ["19 Oct", "Live test: final evaluation of the ledgers"],
    ["21 Oct", "Logistic default: fresh-data verdict (interim: 2 judged, both pass)"],
    ["26 Oct", "Pooled model: fresh-data verdict (interim: would be rejected)"],
  ];
  rows.forEach(([d, t], i) => {
    const y = 1.6 + i * 0.85;
    s.addShape(pres.ShapeType.roundRect, { x: 0.5, y, w: 1.0, h: 0.65, fill: { color: C.accent1 }, line: { color: C.accent1 }, rectRadius: 0.08, objectName: "date-chip" });
    s.addText(d, { x: 0.5, y, w: 1.0, h: 0.65, fontSize: 14, bold: true, color: C.background1, align: "center", valign: "middle", margin: 0, isTextBox: true });
    s.addText(t, { x: 1.65, y, w: 3.9, h: 0.65, fontSize: 14, color: C.text1, valign: "middle", margin: 0, isTextBox: true });
  });
  s.addText("Limits we state ourselves", { x: 6.0, y: 1.6, w: 3.5, h: 0.3, fontSize: 15, bold: true, color: C.text1, margin: 0, isTextBox: true });
  bullets(s, [
    "Nine repositories, 5-7 weeks of history each",
    "Few failures in several repositories: wide intervals",
    "Relaxed gates measured on one pipeline only",
    "Triage not checked against developer actions",
    "Live sample is small",
  ], 6.0, 2.0, 3.5, 3.1, 14);
}

// ================================================================ 14 close
{
  const s = pres.addSlide({ masterName: "CLOSE", sectionTitle: "Next steps" });
  s.addText("What we claim, and what we do not", { placeholder: "title" });
  s.addNotes("Close with the honest position. We claim a rigorous evaluation method, honest negative results, a working tested system and a useful failure triage. We do not claim a general speed-up, calibrated probabilities, or that the pooled model works. Then ask for guidance on the three decisions.");
  s.addText("We claim", { x: 0.5, y: 1.2, w: 4.2, h: 0.35, fontSize: 18, bold: true, color: C.accent2, margin: 0, isTextBox: true });
  bullets(s, [
    "A rigorous evaluation method with pre-registered tests",
    "Honest negative and mixed results, with data",
    "A working, tested pipeline that refuses to guess",
    "A failure triage from real outcomes",
  ], 0.5, 1.6, 4.2, 1.9, 14, C.background1);
  s.addText("We do not claim", { x: 5.0, y: 1.2, w: 4.5, h: 0.35, fontSize: 18, bold: true, color: "F4A6A0", margin: 0, isTextBox: true });
  bullets(s, [
    "A general speed-up of CI",
    "Calibrated failure probabilities",
    "That the pooled model works",
    "That triage advice matches developers",
  ], 5.0, 1.6, 4.5, 1.9, 14, C.background1);
  s.addText("Guidance we would like", { x: 0.5, y: 3.65, w: 9, h: 0.35, fontSize: 18, bold: true, color: C.background1, margin: 0, isTextBox: true });
  bullets(s, [
    "Frame the write-up as an evaluation and negative-results study?",
    "Add more repositories now, or accept nine and state the limit?",
    "Which venue or report format should the final submission target?",
  ], 0.5, 4.05, 9, 1.2, 14, C.background1);
}
// light text colours on the dark layout need explicit values (bullets() uses the dark ink by default): patch
// (handled below by re-colouring text of slide 14 after generation is not possible, so bullets are rebuilt here)

(async () => {
  await pres.writeFile({ fileName: OUT });
  await applyTheme(OUT, THEME);
  console.log("wrote", OUT);
})();
