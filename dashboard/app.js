/* ================================================================
   GHA Optimizer Dashboard — Application Logic

   The page shows ONLY what the API returns.  It never invents numbers:
   with no server, no query parameters or a refused request it shows an
   explicit empty state.  Everything in a plan is ESTIMATED from predicted
   durations — none of it is a measured execution.

   Usage: open index.html?repo=owner/repo&workflow=<path of the workflow file
   on the machine running the server>[&changed=a.py,b.py][&larger_runners=1]
   ================================================================ */

const API_BASE = 'http://localhost:8000';
let currentView = 'overview';

// ─────────────────────────────────────────────────────────────────
// Navigation
// ─────────────────────────────────────────────────────────────────

const VIEW_META = {
    overview:    { title: 'Overview',     subtitle: 'ESTIMATED plan summary (from predicted durations, not measured)' },
    pipeline:    { title: 'Pipeline',     subtitle: 'Job dependency graph and per-job predictions' },
    predictions: { title: 'Predictions',  subtitle: 'Predicted vs actual outcomes over time' },
    schedule:    { title: 'Schedule',     subtitle: 'Explanation of the proposed workflow changes' },
    models:      { title: 'Models',       subtitle: 'ML model performance and feature importance' },
};

document.querySelectorAll('.nav-item').forEach(item => {
    item.addEventListener('click', () => switchView(item.dataset.view));
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
// Run Optimization (real API only)
// ─────────────────────────────────────────────────────────────────

const BUTTON_IDLE_HTML = `
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <polygon points="5 3 19 12 5 21 5 3"/>
    </svg>
    Run Optimization`;

async function runOptimization() {
    const btn = document.getElementById('btnOptimize');
    btn.classList.add('loading');
    btn.textContent = 'Running...';

    try {
        const q = new URLSearchParams(window.location.search);
        const repo = q.get('repo');
        const workflow = q.get('workflow');
        if (!repo || !workflow) {
            showEmpty('Nothing to optimize yet. Open this page as ' +
                'index.html?repo=owner/repo&workflow=<path of the workflow file on the server>' +
                '[&changed=a.py,b.py][&larger_runners=1] — the repository must have been ingested ' +
                '(gha-optimizer ingest / build-dataset).');
            return;
        }
        if (!(await checkServerHealth())) {
            showEmpty('The API server is offline (start it with: gha-optimizer serve). No data is shown.');
            return;
        }
        const body = {
            repo,
            workflow_path: workflow,
            changed_files: (q.get('changed') || '').split(',').filter(Boolean),
        };
        if (q.get('larger_runners') === '1') body.allow_runner_upsizing = true;

        const resp = await fetch(`${API_BASE}/api/v1/optimize`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const payload = await resp.json().catch(() => ({}));
        if (!resp.ok) {
            const d = payload.detail;
            const reason = typeof d === 'string' ? d : (d && (d.reason || d.error)) || `HTTP ${resp.status}`;
            showEmpty(`The API declined this request: ${reason}` + (d && d.hint ? ` — ${d.hint}` : ''));
            return;
        }
        hideBanner();
        updateAllViews(fromApiResult(payload));
    } catch (e) {
        console.error('Optimization failed:', e);
        showEmpty(`Request failed: ${e.message}`);
    } finally {
        btn.classList.remove('loading');
        btn.innerHTML = BUTTON_IDLE_HTML;
    }
}

// The API's OptimizationResultSchema -> what this page renders (nothing is added or defaulted)
function fromApiResult(r) {
    const s = r.plan_summary || {};
    return {
        workflow: r.workflow_name,
        jobs_total: r.jobs_total,
        jobs_skipped: r.jobs_skipped,
        wall_clock: s.wall_clock,
        cost: s.cost,
        energy: s.energy,
        co2: s.co2,
        runner_upsizing: s.runner_upsizing,
        skip_only_pct: s.estimated_time_saved_by_skipping_pct,
        whatif: s.assumed_whatif_larger_runners,
        explanation: r.explanation,
    };
}

// ─────────────────────────────────────────────────────────────────
// Views
// ─────────────────────────────────────────────────────────────────

const UNAVAILABLE_HTML = `
    <div class="card">
        <h3 class="card-title">Not available</h3>
        <p class="card-desc">The API does not return per-job predictions, model metrics, feature importances or
        predicted-vs-actual history yet, so this view shows nothing rather than invented numbers. The same
        information is available from the CLI: <code>gha-optimizer analyze</code> (per-job plan and model
        metrics) and <code>gha-optimizer eval-models</code> (rolling-origin model evaluation).</p>
    </div>`;

function markUnavailable() {
    ['pipeline', 'predictions', 'models'].forEach(v => {
        const el = document.getElementById(`view-${v}`);
        if (el) el.innerHTML = UNAVAILABLE_HTML;
    });
}

function setCard(id, value) {
    document.getElementById(id).textContent = value;
}

function pct(x) { return `${x}%`; }

function updateAllViews(data) {
    markUnavailable();
    const t = data.wall_clock, c = data.cost, e = data.energy;

    setCard('metricTimeSaved', pct(t.saved_pct));
    document.getElementById('metricTimeDetail').textContent = `${t.baseline_s}s → ${t.optimized_s}s (estimated)`;
    setCard('metricCostSaved', pct(c.saved_pct));
    document.getElementById('metricCostDetail').textContent =
        `$${c.baseline_usd.toFixed(4)} → $${c.optimized_usd.toFixed(4)} (estimated; negative = more expensive)`;
    setCard('metricEnergySaved', pct(e.saved_pct));
    document.getElementById('metricEnergyDetail').textContent =
        `${e.baseline_wh.toFixed(2)}Wh → ${e.optimized_wh.toFixed(2)}Wh (estimated)`;
    setCard('metricAccuracy', '—');
    document.getElementById('metricAccuracyDetail').textContent = 'model metrics are not part of the API response';

    const w = data.whatif;
    const extra = [
        `skip recommendations alone: ${data.skip_only_pct}% time`,
        `runner upsizing in this plan: ${data.runner_upsizing ? 'ON' : 'off'}`,
        w ? `what-if larger runners (ASSUMED speed-up exponent ${w.speedup_exponent}): ${w.time_saved_pct}% time, ${w.cost_saved_pct}% cost` : null,
    ].filter(Boolean).join(' · ');

    document.getElementById('recentTableBody').innerHTML = `
        <tr>
            <td>${data.workflow}</td>
            <td>${t.saved_pct}%</td>
            <td>${c.saved_pct}%</td>
            <td>${data.jobs_skipped}/${data.jobs_total}</td>
            <td>${new Date().toLocaleDateString()}</td>
        </tr>
        <tr><td colspan="5"><small>${extra}</small></td></tr>`;

    document.getElementById('explanationText').textContent = data.explanation || 'No explanation returned.';
}

function showEmpty(message) {
    markUnavailable();
    ['metricTimeSaved', 'metricCostSaved', 'metricEnergySaved', 'metricAccuracy'].forEach(id => setCard(id, '—'));
    ['metricTimeDetail', 'metricCostDetail', 'metricEnergyDetail', 'metricAccuracyDetail'].forEach(id => {
        document.getElementById(id).textContent = 'no data';
    });
    document.getElementById('recentTableBody').innerHTML =
        '<tr><td colspan="5">No optimization result yet.</td></tr>';
    document.getElementById('explanationText').textContent = message;
    const banner = document.getElementById('dataBanner');
    banner.textContent = message;
    banner.hidden = false;
}

function hideBanner() {
    document.getElementById('dataBanner').hidden = true;
}

// ─────────────────────────────────────────────────────────────────
// Initialization: real data if the URL says what to optimize, otherwise an explicit empty state
// ─────────────────────────────────────────────────────────────────

async function init() {
    await checkServerHealth();
    await runOptimization();
}

// Spin animation for loading button
const style = document.createElement('style');
style.textContent = `
    @keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
    .spin { animation: spin 1s linear infinite; }
    .data-banner { margin: 0 0 16px; padding: 12px 16px; border-radius: 8px; background: rgba(245,158,11,0.12);
                   border: 1px solid rgba(245,158,11,0.4); color: #f59e0b; font-size: 14px; }
`;
document.head.appendChild(style);

document.addEventListener('DOMContentLoaded', init);
