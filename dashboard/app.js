/* ================================================================
   GHA Optimizer Dashboard — Application Logic
   ================================================================ */

const API_BASE = 'http://localhost:8000';
let currentView = 'overview';
let lastOptimizationResult = null;
let charts = {};

// ─────────────────────────────────────────────────────────────────
// Navigation
// ─────────────────────────────────────────────────────────────────

const VIEW_META = {
    overview:    { title: 'Overview',     subtitle: 'Dashboard summary and key metrics' },
    pipeline:    { title: 'Pipeline',     subtitle: 'Job dependency graph and per-job predictions' },
    predictions: { title: 'Predictions',  subtitle: 'Predicted vs actual outcomes over time' },
    schedule:    { title: 'Schedule',     subtitle: 'Baseline vs optimized timeline comparison' },
    models:      { title: 'Models',       subtitle: 'ML model performance and feature importance' },
};

document.querySelectorAll('.nav-item').forEach(item => {
    item.addEventListener('click', () => {
        const view = item.dataset.view;
        switchView(view);
    });
});

function switchView(viewName) {
    document.querySelectorAll('.nav-item').forEach(i => i.classList.remove('active'));
    document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));

    document.querySelector(`[data-view="${viewName}"]`)?.classList.add('active');
    document.getElementById(`view-${viewName}`)?.classList.add('active');

    const meta = VIEW_META[viewName] || {};
    document.getElementById('viewTitle').textContent = meta.title || viewName;
    document.getElementById('viewSubtitle').textContent = meta.subtitle || '';

    currentView = viewName;
}

// ─────────────────────────────────────────────────────────────────
// Server Health Check
// ─────────────────────────────────────────────────────────────────

async function checkServerHealth() {
    const badge = document.getElementById('serverStatus');
    const dot = badge.querySelector('.status-dot');
    const text = badge.querySelector('span:last-child');

    try {
        const resp = await fetch(`${API_BASE}/health`, { signal: AbortSignal.timeout(3000) });
        if (resp.ok) {
            const data = await resp.json();
            dot.className = 'status-dot connected';
            text.textContent = `Server v${data.version}`;
            return true;
        }
    } catch (e) { /* server not running */ }

    dot.className = 'status-dot error';
    text.textContent = 'Server offline';
    return false;
}

// ─────────────────────────────────────────────────────────────────
// Run Optimization
// ─────────────────────────────────────────────────────────────────

async function runOptimization() {
    const btn = document.getElementById('btnOptimize');
    btn.classList.add('loading');
    btn.innerHTML = `
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="spin">
            <path d="M21 12a9 9 0 11-6.219-8.56"/>
        </svg>
        Running...`;

    try {
        const serverOnline = await checkServerHealth();

        if (serverOnline) {
            const resp = await fetch(`${API_BASE}/optimize`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    repo: 'demo/sample-repo',
                    changed_files: ['src/api/routes.py', 'requirements.txt'],
                    model_backend: 'tabular',
                }),
            });
            if (resp.ok) {
                lastOptimizationResult = await resp.json();
            }
        }

        // If no server, use demo data
        if (!lastOptimizationResult) {
            lastOptimizationResult = generateDemoData();
        }

        updateAllViews(lastOptimizationResult);

    } catch (e) {
        console.error('Optimization failed:', e);
        lastOptimizationResult = generateDemoData();
        updateAllViews(lastOptimizationResult);
    }

    btn.classList.remove('loading');
    btn.innerHTML = `
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <polygon points="5 3 19 12 5 21 5 3"/>
        </svg>
        Run Optimization`;
}

// ─────────────────────────────────────────────────────────────────
// Demo Data (when server is offline)
// ─────────────────────────────────────────────────────────────────

function generateDemoData() {
    return {
        status: 'success',
        summary: {
            jobs_total: 5,
            jobs_skipped: 1,
            wall_clock: { baseline_s: 540, optimized_s: 312, saved_pct: 42.2 },
            cost: { baseline_usd: 0.07200, optimized_usd: 0.04160, saved_pct: 42.2 },
            energy: { baseline_wh: 2.25, optimized_wh: 1.42, saved_pct: 36.9 },
            co2: { baseline_g: 0.675, optimized_g: 0.426, saved_pct: 36.9 },
        },
        explanation: '## 🚀 gha-optimizer: proposed workflow changes\n\n' +
            '| Metric | Baseline | Optimized | Savings |\n' +
            '|--------|----------|-----------|--------|\n' +
            '| ⏱ Wall-clock | 540s | 312s | 42.2% faster |\n' +
            '| 💰 Cost | $0.0720 | $0.0416 | 42.2% cheaper |\n' +
            '| ⚡ Energy | 2.25 Wh | 1.42 Wh | 36.9% less |\n' +
            '| 🌿 CO₂ | 0.675g | 0.426g | 36.9% |\n',
        jobs: [
            { job_id: 'lint', order: 0, runner: 'ubuntu-latest', dag_level: 0, failure_prob: 0.05, cache_miss_prob: 0.18, duration_pred: 30, duration_est: 30, cost_est: 0.004, energy_est: 0.125, co2_est: 0.0375, skip: false, skip_reason: null },
            { job_id: 'integration_tests', order: 1, runner: 'ubuntu-latest', dag_level: 1, failure_prob: 0.22, cache_miss_prob: 0.65, duration_pred: 240, duration_est: 240, cost_est: 0.032, energy_est: 1.0, co2_est: 0.3, skip: false, skip_reason: null },
            { job_id: 'unit_tests', order: 2, runner: 'ubuntu-latest', dag_level: 1, failure_prob: 0.08, cache_miss_prob: 0.18, duration_pred: 90, duration_est: 90, cost_est: 0.012, energy_est: 0.375, co2_est: 0.1125, skip: false, skip_reason: null },
            { job_id: 'build', order: 3, runner: 'ubuntu-latest', dag_level: 2, failure_prob: 0.06, cache_miss_prob: 0.45, duration_pred: 120, duration_est: 120, cost_est: 0.016, energy_est: 0.5, co2_est: 0.15, skip: false, skip_reason: null },
            { job_id: 'deploy', order: 4, runner: 'ubuntu-latest', dag_level: 3, failure_prob: 0.88, cache_miss_prob: 0.12, duration_pred: 60, duration_est: 60, cost_est: 0.008, energy_est: 0.25, co2_est: 0.075, skip: true, skip_reason: 'downstream of high failure prob job' },
        ],
        model_metrics: {
            cache_miss: { backend: 'sklearn', precision: 0.82, recall: 0.78, f1: 0.80, roc_auc: 0.89 },
            failure: { backend: 'sklearn', precision: 0.71, recall: 0.65, f1: 0.68, optimal_threshold: 0.38 },
            duration: { backend: 'sklearn', mae_seconds: 18.5, mape: 0.14, r2: 0.87 },
        },
        feature_importance: {
            cache_key_touched: 0.28,
            historical_failure_rate: 0.22,
            files_changed_count: 0.15,
            dep_graph_distance: 0.12,
            cache_key_stability: 0.08,
            loc_added: 0.06,
            author_failure_rate: 0.04,
            hour_of_day: 0.03,
            recent_failure_streak: 0.02,
        },
    };
}

// ─────────────────────────────────────────────────────────────────
// Update Views
// ─────────────────────────────────────────────────────────────────

function updateAllViews(data) {
    updateOverview(data);
    updatePipeline(data);
    updateSchedule(data);
    updateModels(data);
    updatePredictions(data);
}

function updateOverview(data) {
    const s = data.summary;

    document.getElementById('metricTimeSaved').textContent = `${s.wall_clock.saved_pct}%`;
    document.getElementById('metricTimeDetail').textContent = `${s.wall_clock.baseline_s}s → ${s.wall_clock.optimized_s}s`;

    document.getElementById('metricCostSaved').textContent = `${s.cost.saved_pct}%`;
    document.getElementById('metricCostDetail').textContent = `$${s.cost.baseline_usd.toFixed(4)} → $${s.cost.optimized_usd.toFixed(4)}`;

    document.getElementById('metricEnergySaved').textContent = `${s.energy.saved_pct}%`;
    document.getElementById('metricEnergyDetail').textContent = `${s.energy.baseline_wh.toFixed(2)}Wh → ${s.energy.optimized_wh.toFixed(2)}Wh`;

    const acc = data.model_metrics?.cache_miss?.roc_auc || 0.89;
    document.getElementById('metricAccuracy').textContent = `${(acc * 100).toFixed(1)}%`;
    document.getElementById('metricAccuracyDetail').textContent = 'Cache-miss ROC AUC';

    // History chart
    renderHistoryChart(data);
    renderCostChart(data);

    // Recent table
    const tbody = document.getElementById('recentTableBody');
    tbody.innerHTML = `
        <tr>
            <td>CI</td>
            <td><span class="badge badge-success">${s.wall_clock.saved_pct}%</span></td>
            <td><span class="badge badge-success">${s.cost.saved_pct}%</span></td>
            <td>${s.jobs_skipped}/${s.jobs_total}</td>
            <td>${new Date().toLocaleDateString()}</td>
        </tr>`;
}

function updatePipeline(data) {
    renderDAG(data.jobs);

    const tbody = document.getElementById('jobTableBody');
    tbody.innerHTML = data.jobs.map(j => `
        <tr>
            <td><strong>${j.job_id}</strong></td>
            <td>${j.dag_level}</td>
            <td><code>${j.runner}</code></td>
            <td><span class="badge ${j.failure_prob > 0.5 ? 'badge-danger' : j.failure_prob > 0.2 ? 'badge-warning' : 'badge-success'}">${j.failure_prob.toFixed(2)}</span></td>
            <td><span class="badge ${j.cache_miss_prob > 0.5 ? 'badge-warning' : 'badge-info'}">${j.cache_miss_prob.toFixed(2)}</span></td>
            <td>${j.duration_est.toFixed(0)}s</td>
            <td>$${j.cost_est.toFixed(5)}</td>
            <td>${j.co2_est.toFixed(3)}g</td>
            <td>${j.skip ? '<span class="badge badge-danger">SKIP</span>' : '<span class="badge badge-success">RUN</span>'}</td>
        </tr>`).join('');
}

function updateSchedule(data) {
    renderGanttChart(data);
    document.getElementById('explanationText').textContent = data.explanation || 'No explanation available.';
}

function updateModels(data) {
    const cm = data.model_metrics?.cache_miss || {};
    const fm = data.model_metrics?.failure || {};
    const dm = data.model_metrics?.duration || {};

    setModelMetrics('cacheModelMetrics', [
        ['Backend', cm.backend || '—'],
        ['Precision', cm.precision?.toFixed(4) || '—'],
        ['Recall', cm.recall?.toFixed(4) || '—'],
        ['F1', cm.f1?.toFixed(4) || '—'],
        ['ROC AUC', cm.roc_auc?.toFixed(4) || '—'],
    ]);

    setModelMetrics('failureModelMetrics', [
        ['Backend', fm.backend || '—'],
        ['Precision', fm.precision?.toFixed(4) || '—'],
        ['Recall', fm.recall?.toFixed(4) || '—'],
        ['F1', fm.f1?.toFixed(4) || '—'],
        ['Threshold', fm.optimal_threshold?.toFixed(4) || '—'],
    ]);

    setModelMetrics('durationModelMetrics', [
        ['Backend', dm.backend || '—'],
        ['MAE', dm.mae_seconds ? `${dm.mae_seconds.toFixed(1)}s` : '—'],
        ['MAPE', dm.mape?.toFixed(4) || '—'],
        ['R²', dm.r2?.toFixed(4) || '—'],
    ]);

    renderFeatureImportanceChart(data);
}

function updatePredictions(data) {
    renderCachePredictionsChart(data);
    renderDurationPredictionsChart(data);
    renderAccuracyTrendChart(data);
}

function setModelMetrics(containerId, rows) {
    const container = document.getElementById(containerId);
    container.innerHTML = rows.map(([label, value]) =>
        `<div class="metric-row"><span>${label}</span><span>${value}</span></div>`
    ).join('');
}

// ─────────────────────────────────────────────────────────────────
// Chart Rendering
// ─────────────────────────────────────────────────────────────────

const CHART_COLORS = {
    primary: '#6366f1',
    secondary: '#8b5cf6',
    success: '#10b981',
    warning: '#f59e0b',
    danger: '#ef4444',
    info: '#3b82f6',
    muted: '#6868a0',
    grid: 'rgba(255,255,255,0.05)',
    text: '#9898b8',
};

const CHART_DEFAULTS = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
        legend: { labels: { color: CHART_COLORS.text, font: { family: 'Inter', size: 12 } } },
    },
    scales: {
        x: { ticks: { color: CHART_COLORS.text }, grid: { color: CHART_COLORS.grid } },
        y: { ticks: { color: CHART_COLORS.text }, grid: { color: CHART_COLORS.grid } },
    },
};

function getOrCreate(canvasId, type, config) {
    if (charts[canvasId]) {
        charts[canvasId].destroy();
    }
    const ctx = document.getElementById(canvasId)?.getContext('2d');
    if (!ctx) return null;
    charts[canvasId] = new Chart(ctx, { type, ...config });
    return charts[canvasId];
}

function renderHistoryChart(data) {
    const jobs = data.jobs || [];
    getOrCreate('chartHistory', 'bar', {
        data: {
            labels: jobs.map(j => j.job_id),
            datasets: [
                {
                    label: 'Baseline (s)',
                    data: jobs.map(j => j.duration_pred),
                    backgroundColor: 'rgba(99, 102, 241, 0.3)',
                    borderColor: CHART_COLORS.primary,
                    borderWidth: 1,
                    borderRadius: 4,
                },
                {
                    label: 'Optimized (s)',
                    data: jobs.map(j => j.skip ? 0 : j.duration_est),
                    backgroundColor: 'rgba(16, 185, 129, 0.3)',
                    borderColor: CHART_COLORS.success,
                    borderWidth: 1,
                    borderRadius: 4,
                },
            ],
        },
        options: {
            ...CHART_DEFAULTS,
            plugins: { ...CHART_DEFAULTS.plugins, title: { display: false } },
        },
    });
}

function renderCostChart(data) {
    const s = data.summary;
    getOrCreate('chartCost', 'doughnut', {
        data: {
            labels: ['Time Savings', 'Cost Savings', 'Energy Savings', 'CO₂ Savings'],
            datasets: [{
                data: [s.wall_clock.saved_pct, s.cost.saved_pct, s.energy.saved_pct, s.co2.saved_pct],
                backgroundColor: [
                    'rgba(99, 102, 241, 0.7)',
                    'rgba(16, 185, 129, 0.7)',
                    'rgba(245, 158, 11, 0.7)',
                    'rgba(139, 92, 246, 0.7)',
                ],
                borderColor: 'transparent',
                borderWidth: 0,
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            cutout: '65%',
            plugins: {
                legend: {
                    position: 'bottom',
                    labels: { color: CHART_COLORS.text, font: { family: 'Inter', size: 12 }, padding: 16 },
                },
            },
        },
    });
}

function renderGanttChart(data) {
    const jobs = data.jobs || [];
    const levels = {};
    jobs.forEach(j => {
        if (!levels[j.dag_level]) levels[j.dag_level] = [];
        levels[j.dag_level].push(j);
    });

    // Build baseline and optimized timelines
    const labels = [];
    const baselineData = [];
    const optimizedData = [];
    let baseOffset = 0;
    let optOffset = 0;

    Object.keys(levels).sort().forEach(lvl => {
        const lvlJobs = levels[lvl];
        const maxBaseDur = Math.max(...lvlJobs.map(j => j.duration_pred));
        const maxOptDur = Math.max(...lvlJobs.filter(j => !j.skip).map(j => j.duration_est).concat([0]));

        lvlJobs.forEach(j => {
            labels.push(j.job_id);
            baselineData.push([baseOffset, baseOffset + j.duration_pred]);
            optimizedData.push(j.skip ? [0, 0] : [optOffset, optOffset + j.duration_est]);
        });

        baseOffset += maxBaseDur;
        optOffset += maxOptDur;
    });

    getOrCreate('chartGantt', 'bar', {
        data: {
            labels,
            datasets: [
                {
                    label: 'Baseline',
                    data: baselineData.map(([s, e]) => e - s),
                    backgroundColor: 'rgba(99, 102, 241, 0.5)',
                    borderColor: CHART_COLORS.primary,
                    borderWidth: 1,
                    borderRadius: 3,
                },
                {
                    label: 'Optimized',
                    data: optimizedData.map(([s, e]) => e - s),
                    backgroundColor: 'rgba(16, 185, 129, 0.5)',
                    borderColor: CHART_COLORS.success,
                    borderWidth: 1,
                    borderRadius: 3,
                },
            ],
        },
        options: {
            ...CHART_DEFAULTS,
            indexAxis: 'y',
            plugins: { ...CHART_DEFAULTS.plugins },
            scales: {
                x: { ...CHART_DEFAULTS.scales.x, title: { display: true, text: 'Duration (s)', color: CHART_COLORS.text } },
                y: { ...CHART_DEFAULTS.scales.y },
            },
        },
    });
}

function renderFeatureImportanceChart(data) {
    const fi = data.feature_importance || {};
    const sorted = Object.entries(fi).sort((a, b) => b[1] - a[1]);

    getOrCreate('chartFeatureImportance', 'bar', {
        data: {
            labels: sorted.map(([k]) => k.replace(/_/g, ' ')),
            datasets: [{
                label: 'Importance',
                data: sorted.map(([, v]) => v),
                backgroundColor: sorted.map((_, i) =>
                    `hsla(${240 + i * 15}, 70%, 60%, 0.6)`
                ),
                borderColor: sorted.map((_, i) =>
                    `hsla(${240 + i * 15}, 70%, 60%, 1)`
                ),
                borderWidth: 1,
                borderRadius: 4,
            }],
        },
        options: {
            ...CHART_DEFAULTS,
            indexAxis: 'y',
            plugins: { ...CHART_DEFAULTS.plugins, legend: { display: false } },
        },
    });
}

function renderCachePredictionsChart(data) {
    const jobs = data.jobs || [];
    getOrCreate('chartCachePredictions', 'bar', {
        data: {
            labels: jobs.map(j => j.job_id),
            datasets: [{
                label: 'P(Cache Miss)',
                data: jobs.map(j => j.cache_miss_prob),
                backgroundColor: jobs.map(j => j.cache_miss_prob > 0.5 ? 'rgba(245, 158, 11, 0.5)' : 'rgba(59, 130, 246, 0.5)'),
                borderColor: jobs.map(j => j.cache_miss_prob > 0.5 ? CHART_COLORS.warning : CHART_COLORS.info),
                borderWidth: 1,
                borderRadius: 4,
            }],
        },
        options: { ...CHART_DEFAULTS, plugins: { ...CHART_DEFAULTS.plugins, legend: { display: false } } },
    });
}

function renderDurationPredictionsChart(data) {
    const jobs = data.jobs || [];
    getOrCreate('chartDurationPredictions', 'bar', {
        data: {
            labels: jobs.map(j => j.job_id),
            datasets: [
                {
                    label: 'Predicted (s)',
                    data: jobs.map(j => j.duration_pred),
                    backgroundColor: 'rgba(139, 92, 246, 0.4)',
                    borderColor: CHART_COLORS.secondary,
                    borderWidth: 1,
                    borderRadius: 4,
                },
                {
                    label: 'Estimated on Runner (s)',
                    data: jobs.map(j => j.duration_est),
                    backgroundColor: 'rgba(16, 185, 129, 0.4)',
                    borderColor: CHART_COLORS.success,
                    borderWidth: 1,
                    borderRadius: 4,
                },
            ],
        },
        options: CHART_DEFAULTS,
    });
}

function renderAccuracyTrendChart(data) {
    // Simulated trend data
    const labels = Array.from({ length: 10 }, (_, i) => `Week ${i + 1}`);
    const cacheAcc = labels.map(() => 0.75 + Math.random() * 0.15);
    const failAcc = labels.map(() => 0.65 + Math.random() * 0.15);

    getOrCreate('chartAccuracyTrend', 'line', {
        data: {
            labels,
            datasets: [
                {
                    label: 'Cache Accuracy',
                    data: cacheAcc,
                    borderColor: CHART_COLORS.primary,
                    backgroundColor: 'rgba(99, 102, 241, 0.1)',
                    fill: true,
                    tension: 0.4,
                    pointRadius: 3,
                },
                {
                    label: 'Failure Accuracy',
                    data: failAcc,
                    borderColor: CHART_COLORS.warning,
                    backgroundColor: 'rgba(245, 158, 11, 0.1)',
                    fill: true,
                    tension: 0.4,
                    pointRadius: 3,
                },
            ],
        },
        options: {
            ...CHART_DEFAULTS,
            scales: {
                ...CHART_DEFAULTS.scales,
                y: { ...CHART_DEFAULTS.scales.y, min: 0.5, max: 1.0 },
            },
        },
    });
}

// ─────────────────────────────────────────────────────────────────
// DAG Visualization (SVG)
// ─────────────────────────────────────────────────────────────────

function renderDAG(jobs) {
    const svg = document.getElementById('dagSvg');
    if (!svg) return;

    const w = svg.clientWidth || 800;
    const h = 400;
    svg.setAttribute('viewBox', `0 0 ${w} ${h}`);

    // Group by level
    const levels = {};
    jobs.forEach(j => {
        if (!levels[j.dag_level]) levels[j.dag_level] = [];
        levels[j.dag_level].push(j);
    });

    const levelKeys = Object.keys(levels).sort((a, b) => a - b);
    const nodeW = 130;
    const nodeH = 56;
    const positions = {};

    // Layout nodes
    levelKeys.forEach((lvl, li) => {
        const lvlJobs = levels[lvl];
        const x = 60 + li * (w - 120) / Math.max(levelKeys.length - 1, 1);
        lvlJobs.forEach((j, ji) => {
            const y = (h / 2) + (ji - (lvlJobs.length - 1) / 2) * (nodeH + 24);
            positions[j.job_id] = { x, y, job: j };
        });
    });

    // Define edges (based on typical CI DAG)
    const dagEdges = [
        ['lint', 'unit_tests'],
        ['lint', 'integration_tests'],
        ['unit_tests', 'build'],
        ['integration_tests', 'build'],
        ['build', 'deploy'],
    ];

    let svgContent = `
        <defs>
            <linearGradient id="nodeGrad" x1="0%" y1="0%" x2="100%" y2="100%">
                <stop offset="0%" stop-color="#6366f1" stop-opacity="0.3"/>
                <stop offset="100%" stop-color="#8b5cf6" stop-opacity="0.15"/>
            </linearGradient>
            <linearGradient id="skipGrad" x1="0%" y1="0%" x2="100%" y2="100%">
                <stop offset="0%" stop-color="#ef4444" stop-opacity="0.3"/>
                <stop offset="100%" stop-color="#ef4444" stop-opacity="0.1"/>
            </linearGradient>
            <marker id="arrowhead" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto">
                <polygon points="0 0, 8 3, 0 6" fill="#6868a0"/>
            </marker>
        </defs>`;

    // Draw edges
    dagEdges.forEach(([from, to]) => {
        const pFrom = positions[from];
        const pTo = positions[to];
        if (pFrom && pTo) {
            svgContent += `
                <line x1="${pFrom.x + nodeW / 2}" y1="${pFrom.y}"
                      x2="${pTo.x - nodeW / 2}" y2="${pTo.y}"
                      stroke="#6868a0" stroke-width="1.5" stroke-opacity="0.4"
                      marker-end="url(#arrowhead)"/>`;
        }
    });

    // Draw nodes
    Object.values(positions).forEach(({ x, y, job }) => {
        const nx = x - nodeW / 2;
        const ny = y - nodeH / 2;
        const fill = job.skip ? 'url(#skipGrad)' : 'url(#nodeGrad)';
        const stroke = job.skip ? '#ef4444' : (job.failure_prob > 0.2 ? '#f59e0b' : '#6366f1');
        const failColor = job.failure_prob > 0.5 ? '#ef4444' : (job.failure_prob > 0.2 ? '#f59e0b' : '#10b981');

        svgContent += `
            <g class="dag-node">
                <rect class="node-rect" x="${nx}" y="${ny}" width="${nodeW}" height="${nodeH}"
                      rx="10" fill="${fill}" stroke="${stroke}" stroke-width="1.5" stroke-opacity="0.6"/>
                <text x="${x}" y="${y - 6}" text-anchor="middle" fill="#eaeaff"
                      font-size="13" font-weight="600" font-family="Inter">${job.job_id}</text>
                <text x="${x}" y="${y + 14}" text-anchor="middle" fill="${failColor}"
                      font-size="11" font-family="Inter">
                    P(fail)=${job.failure_prob.toFixed(2)} ${job.skip ? '⛔' : ''}
                </text>
            </g>`;
    });

    svg.innerHTML = svgContent;
}

// ─────────────────────────────────────────────────────────────────
// Initialization
// ─────────────────────────────────────────────────────────────────

async function init() {
    await checkServerHealth();

    // Auto-run optimization with demo data on load
    lastOptimizationResult = generateDemoData();
    updateAllViews(lastOptimizationResult);
}

// Spin animation for loading button
const style = document.createElement('style');
style.textContent = `
    @keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
    .spin { animation: spin 1s linear infinite; }
`;
document.head.appendChild(style);

init();
