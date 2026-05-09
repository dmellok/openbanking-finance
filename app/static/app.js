// pyFinance dashboard — vanilla JS, ECharts, no build step.

const BASE = '';
const REFRESH_MS = 60_000;

// ── Global state ──────────────────────────────────────────────────────────────

const state = {
    activeTab: 'spending',
    from: null,
    to: null,
    accountIds: new Set(),
    accounts: [],
    networthMode: 'series',
    txCategoryFilter: null,
    txOffset: 0,
    txLimit: 50,
};

const charts = {};

// ── Utilities ─────────────────────────────────────────────────────────────────

function $(sel) { return document.querySelector(sel); }
function $$(sel) { return Array.from(document.querySelectorAll(sel)); }

function isoToday() { return new Date().toISOString().slice(0, 10); }
function isoNDaysAgo(n) {
    const d = new Date();
    d.setDate(d.getDate() - n);
    return d.toISOString().slice(0, 10);
}

function fmtMoney(value, currency = 'AUD') {
    if (value == null || value === '') return '—';
    const num = Number(value);
    if (Number.isNaN(num)) return String(value);
    return num.toLocaleString('en-AU', { style: 'currency', currency, maximumFractionDigits: 2 });
}

function fmtAmountSigned(value, currency = 'AUD') {
    if (value == null || value === '') return '—';
    const num = Number(value);
    return num.toLocaleString('en-AU', { style: 'currency', currency, signDisplay: 'always' });
}

function fmtRelative(iso) {
    if (!iso) return '—';
    const then = new Date(iso);
    const diffSec = Math.floor((Date.now() - then.getTime()) / 1000);
    if (diffSec < 60) return `${diffSec}s ago`;
    if (diffSec < 3600) return `${Math.floor(diffSec / 60)} min ago`;
    if (diffSec < 86400) return `${Math.floor(diffSec / 3600)} h ago`;
    return then.toLocaleDateString();
}

function setSpinner(on) { $('#spinner').hidden = !on; }

async function fetchJSON(path, params = {}) {
    const url = new URL(path, location.origin);
    for (const [k, v] of Object.entries(params)) {
        if (v == null) continue;
        if (Array.isArray(v)) v.forEach(item => url.searchParams.append(k, item));
        else url.searchParams.set(k, v);
    }
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(`${url}: ${resp.status} ${await resp.text()}`);
    return resp.json();
}

function commonParams() {
    const params = { from: state.from, to: state.to };
    const ids = Array.from(state.accountIds);
    if (ids.length > 0 && ids.length < state.accounts.length) {
        params.account_ids = ids;
    }
    return params;
}

function setEmpty(id, isEmpty) { const el = $(`#${id}`); if (el) el.hidden = !isEmpty; }

// ── Tabs ──────────────────────────────────────────────────────────────────────

function activateTab(name) {
    state.activeTab = name;
    $$('.tab').forEach(b => b.classList.toggle('is-active', b.dataset.tab === name));
    $$('.tab-panel').forEach(p => p.classList.toggle('is-active', p.dataset.panel === name));
    // ECharts needs resize when its panel becomes visible.
    Object.values(charts).forEach(c => c && c.resize());
    refreshCurrentTab();
}

// ── Filters: date + accounts ──────────────────────────────────────────────────

function initFilters() {
    state.from = isoNDaysAgo(30);
    state.to = isoToday();
    $('#filter-from').value = state.from;
    $('#filter-to').value = state.to;

    $('#filter-from').addEventListener('change', e => {
        state.from = e.target.value || null;
        state.txOffset = 0;
        refreshAll();
    });
    $('#filter-to').addEventListener('change', e => {
        state.to = e.target.value || null;
        state.txOffset = 0;
        refreshAll();
    });
}

function renderAccountPicker() {
    const list = $('#account-picker-list');
    list.innerHTML = '';
    if (state.accounts.length === 0) {
        list.textContent = 'No accounts loaded yet.';
        $('#account-picker-summary').textContent = 'all';
        return;
    }
    for (const a of state.accounts) {
        const id = `acc-${a.id}`;
        const checked = state.accountIds.size === 0 || state.accountIds.has(a.id);
        const lbl = document.createElement('label');
        lbl.innerHTML = `<input type="checkbox" id="${id}" data-id="${a.id}" ${checked ? 'checked' : ''}>` +
            `<span>${a.institution_name || ''} — ${a.name}</span>`;
        list.appendChild(lbl);
    }
    list.querySelectorAll('input[type=checkbox]').forEach(input => {
        input.addEventListener('change', () => {
            const id = input.dataset.id;
            if (input.checked) state.accountIds.add(id);
            else state.accountIds.delete(id);
            updateAccountPickerSummary();
            state.txOffset = 0;
            refreshAll();
        });
    });
    updateAccountPickerSummary();
}

function updateAccountPickerSummary() {
    if (state.accountIds.size === 0 || state.accountIds.size === state.accounts.length) {
        $('#account-picker-summary').textContent = 'all';
    } else {
        $('#account-picker-summary').textContent = `${state.accountIds.size} of ${state.accounts.length}`;
    }
}

// ── Poll status ───────────────────────────────────────────────────────────────

async function refreshPollStatus() {
    try {
        const runs = await fetchJSON(`${BASE}/api/sync-runs`, { limit: 5 });
        const el = $('#poll-status');
        if (runs.length === 0) {
            el.textContent = 'Last poll: none yet';
            el.className = 'poll-status';
            return;
        }
        const latest = runs[0];
        const ago = fmtRelative(latest.finished_at || latest.started_at);
        const symbol = latest.status === 'ok' ? '✓' : latest.status === 'error' ? '✗' : '⟳';
        el.textContent = `Last ${latest.kind}: ${ago} — ${symbol}`;
        el.className = 'poll-status is-' + (latest.status === 'ok' ? 'ok' : latest.status === 'error' ? 'error' : 'running');
    } catch (e) {
        const el = $('#poll-status');
        el.textContent = 'Poll status unavailable';
        el.className = 'poll-status is-error';
    }
}

// ── Accounts (used for both picker + Net worth tab) ───────────────────────────

async function refreshAccounts() {
    const accounts = await fetchJSON(`${BASE}/api/accounts`);
    state.accounts = accounts;
    renderAccountPicker();
    renderAccountsTable(accounts);
}

function renderAccountsTable(accounts) {
    const tbody = $('#table-accounts tbody');
    tbody.innerHTML = '';
    setEmpty('empty-accounts', accounts.length === 0);
    for (const a of accounts) {
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td>${a.institution_name || '—'} — ${a.name}</td>
            <td>${a.type}</td>
            <td>${a.currency}</td>
            <td class="num">${fmtMoney(a.latest_balance, a.currency)}</td>
            <td>${fmtRelative(a.latest_balance_at)}</td>
        `;
        tbody.appendChild(tr);
    }
}

// ── Spending tab ──────────────────────────────────────────────────────────────

async function refreshSpending() {
    const [byCat, topMerchants] = await Promise.all([
        fetchJSON(`${BASE}/api/spending/by-category`, commonParams()),
        fetchJSON(`${BASE}/api/spending/top-merchants`, { ...commonParams(), limit: 10 }),
    ]);
    renderByCategoryChart(byCat);
    renderTopMerchants(topMerchants);
    await refreshTransactionsTable();
}

function renderByCategoryChart(data) {
    setEmpty('empty-by-category', data.length === 0);
    const chart = charts.byCategory ||= echarts.init($('#chart-by-category'), 'dark');
    if (data.length === 0) { chart.clear(); return; }
    const sorted = [...data].sort((a, b) => Number(b.total) - Number(a.total));
    chart.setOption({
        backgroundColor: 'transparent',
        tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
        grid: { left: 60, right: 20, top: 20, bottom: 60 },
        xAxis: {
            type: 'category',
            data: sorted.map(d => d.category),
            axisLabel: { interval: 0, rotate: 35, color: '#8b949e' },
        },
        yAxis: { type: 'value', axisLabel: { color: '#8b949e' } },
        series: [{
            type: 'bar',
            data: sorted.map(d => Number(d.total)),
            itemStyle: { color: '#4f9cff' },
            emphasis: { itemStyle: { color: '#79b3ff' } },
        }],
    }, true);
    chart.off('click');
    chart.on('click', params => {
        if (params.componentType !== 'series') return;
        state.txCategoryFilter = sorted[params.dataIndex].category;
        state.txOffset = 0;
        const pill = $('#tx-filter-pill');
        pill.textContent = state.txCategoryFilter;
        pill.hidden = false;
        $('#tx-filter-clear').hidden = false;
        refreshTransactionsTable();
    });
}

function renderTopMerchants(data) {
    setEmpty('empty-top-merchants', data.length === 0);
    const tbody = $('#table-top-merchants tbody');
    tbody.innerHTML = '';
    for (const r of data) {
        const tr = document.createElement('tr');
        tr.innerHTML = `<td>${r.merchant_name}</td><td class="num">${fmtMoney(r.total)}</td><td class="num">${r.count}</td>`;
        tbody.appendChild(tr);
    }
}

async function refreshTransactionsTable() {
    const params = {
        ...commonParams(),
        limit: state.txLimit,
        offset: state.txOffset,
    };
    delete params.account_ids; // /api/transactions takes account_id (singular)
    const ids = Array.from(state.accountIds);
    if (ids.length === 1) params.account_id = ids[0];
    if (state.txCategoryFilter) params.category = state.txCategoryFilter;

    const body = await fetchJSON(`${BASE}/api/transactions`, params);
    const tbody = $('#table-transactions tbody');
    tbody.innerHTML = '';
    setEmpty('empty-transactions', body.data.length === 0);
    for (const tx of body.data) {
        const cls = tx.direction === 'debit' ? 'amount-debit' : 'amount-credit';
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td>${tx.local_date}</td>
            <td>${tx.description ?? ''}</td>
            <td>${tx.merchant_name ?? '—'}</td>
            <td>${tx.category ?? '—'}</td>
            <td class="num ${cls}">${fmtAmountSigned(tx.amount, tx.currency)}</td>
        `;
        tbody.appendChild(tr);
    }
    const pag = body.pagination;
    $('#tx-pager-info').textContent = pag.total === 0
        ? '0 transactions'
        : `${pag.offset + 1}–${Math.min(pag.offset + body.data.length, pag.total)} of ${pag.total}`;
    $('#tx-prev').disabled = state.txOffset === 0;
    $('#tx-next').disabled = !pag.hasMore;
}

// ── Net worth tab ─────────────────────────────────────────────────────────────

async function refreshNetWorth() {
    const path = state.networthMode === 'series'
        ? '/api/net-worth/series'
        : '/api/net-worth/per-account';
    const points = await fetchJSON(`${BASE}${path}`, commonParams());
    setEmpty('empty-networth', points.length === 0);
    const chart = charts.networth ||= echarts.init($('#chart-networth'), 'dark');
    if (points.length === 0) { chart.clear(); return; }

    if (state.networthMode === 'series') {
        chart.setOption({
            backgroundColor: 'transparent',
            tooltip: { trigger: 'axis' },
            grid: { left: 70, right: 20, top: 20, bottom: 50 },
            xAxis: { type: 'time', axisLabel: { color: '#8b949e' } },
            yAxis: { type: 'value', axisLabel: { color: '#8b949e' } },
            series: [{
                type: 'line',
                showSymbol: false,
                smooth: false,
                data: points.map(p => [p.taken_at, Number(p.total)]),
                lineStyle: { color: '#4f9cff', width: 2 },
                areaStyle: { color: 'rgba(79, 156, 255, 0.15)' },
            }],
        }, true);
    } else {
        const accountsById = new Map(state.accounts.map(a => [a.id, a]));
        const groups = new Map();
        for (const p of points) {
            if (!groups.has(p.account_id)) groups.set(p.account_id, []);
            groups.get(p.account_id).push([p.taken_at, Number(p.balance)]);
        }
        chart.setOption({
            backgroundColor: 'transparent',
            tooltip: { trigger: 'axis' },
            legend: { textStyle: { color: '#8b949e' }, top: 0 },
            grid: { left: 70, right: 20, top: 40, bottom: 50 },
            xAxis: { type: 'time', axisLabel: { color: '#8b949e' } },
            yAxis: { type: 'value', axisLabel: { color: '#8b949e' } },
            series: Array.from(groups.entries()).map(([id, data]) => ({
                name: accountsById.get(id)?.name || id.slice(0, 8),
                type: 'line',
                stack: 'total',
                showSymbol: false,
                areaStyle: {},
                data,
            })),
        }, true);
    }
}

function initNetWorthToggle() {
    $$('.toggle__btn').forEach(b => {
        b.addEventListener('click', () => {
            state.networthMode = b.dataset.mode;
            $$('.toggle__btn').forEach(x => x.classList.toggle('is-active', x === b));
            refreshNetWorth();
        });
    });
}

// ── Cash flow tab ─────────────────────────────────────────────────────────────

async function refreshCashflow() {
    const months = await fetchJSON(`${BASE}/api/cashflow/monthly`, commonParams());
    setEmpty('empty-cashflow', months.length === 0);
    const chart = charts.cashflow ||= echarts.init($('#chart-cashflow'), 'dark');
    if (months.length === 0) {
        chart.clear();
        $('#savings-rate').textContent = '—';
    } else {
        chart.setOption({
            backgroundColor: 'transparent',
            tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
            legend: { textStyle: { color: '#8b949e' }, top: 0 },
            grid: { left: 70, right: 20, top: 40, bottom: 50 },
            xAxis: { type: 'category', data: months.map(m => m.month), axisLabel: { color: '#8b949e' } },
            yAxis: { type: 'value', axisLabel: { color: '#8b949e' } },
            series: [
                { name: 'Income', type: 'bar', data: months.map(m => Number(m.income)), itemStyle: { color: '#3fb950' } },
                { name: 'Expenses', type: 'bar', data: months.map(m => Number(m.expenses)), itemStyle: { color: '#f85149' } },
            ],
        }, true);
        const latest = months[months.length - 1];
        $('#savings-rate').textContent = `${(latest.savings_rate * 100).toFixed(1)}%`;
        $('#savings-rate').style.color = latest.savings_rate >= 0 ? 'var(--good)' : 'var(--bad)';
    }
    await refreshBudgets(months);
}

async function refreshBudgets(months) {
    const [budgets, byCategory] = await Promise.all([
        fetchJSON(`${BASE}/api/budgets`),
        fetchJSON(`${BASE}/api/spending/by-category`, {
            from: months.length > 0
                ? `${months[months.length - 1].month}-01`
                : isoNDaysAgo(30),
            to: state.to,
        }),
    ]);
    const spendBy = new Map(byCategory.map(r => [r.category, Number(r.total)]));
    const wrap = $('#budgets');
    wrap.innerHTML = '';
    if (budgets.length === 0) {
        wrap.innerHTML = '<p class="empty">No budgets set yet.</p>';
        return;
    }
    for (const b of budgets) {
        const limit = Number(b.monthly_limit);
        const spent = spendBy.get(b.category) || 0;
        const ratio = limit > 0 ? Math.min(spent / limit, 1.5) : 0;
        const fillCls = ratio >= 1 ? 'is-over' : ratio >= 0.8 ? 'is-warn' : '';
        const div = document.createElement('div');
        div.className = 'budget';
        div.innerHTML = `
            <span><strong>${b.category}</strong></span>
            <span class="budget__amounts">${fmtMoney(spent, b.currency)} / ${fmtMoney(limit, b.currency)}</span>
            <div class="budget__bar">
                <div class="budget__bar-fill ${fillCls}" style="width: ${Math.min(ratio * 100, 100)}%"></div>
            </div>
        `;
        wrap.appendChild(div);
    }
}

function initBudgetForm() {
    $('#budget-form').addEventListener('submit', async e => {
        e.preventDefault();
        const fd = new FormData(e.target);
        const body = {
            category: fd.get('category').toString().trim(),
            monthly_limit: fd.get('monthly_limit').toString().trim(),
            currency: fd.get('currency').toString().trim() || 'AUD',
        };
        const resp = await fetch(`${BASE}/api/budgets`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        if (resp.ok) {
            e.target.reset();
            $('input[name=currency]', e.target).value = 'AUD';
            refreshCashflow();
        } else {
            alert(`Save failed: ${await resp.text()}`);
        }
    });
}

// ── Insights tab ──────────────────────────────────────────────────────────────

async function refreshInsights() {
    const [calendar, sankey, treemap, timeHeat] = await Promise.all([
        fetchJSON(`${BASE}/api/insights/calendar`, commonParams()),
        fetchJSON(`${BASE}/api/insights/sankey`, commonParams()),
        fetchJSON(`${BASE}/api/insights/treemap`, commonParams()),
        fetchJSON(`${BASE}/api/insights/time-heatmap`, commonParams()),
    ]);
    renderCalendarHeatmap(calendar);
    renderSankey(sankey);
    renderTreemap(treemap);
    renderTimeHeatmap(timeHeat);
}

function renderCalendarHeatmap(points) {
    setEmpty('empty-calendar', points.length === 0);
    const chart = charts.calendar ||= echarts.init($('#chart-calendar'), 'dark');
    if (points.length === 0) { chart.clear(); return; }
    const values = points.map(p => Number(p.total));
    const max = Math.max(1, ...values);
    const dates = points.map(p => p.date);
    const sortedDates = [...dates].sort();
    const yearStart = sortedDates[0].slice(0, 4);
    const yearEnd = sortedDates[sortedDates.length - 1].slice(0, 4);
    chart.setOption({
        backgroundColor: 'transparent',
        tooltip: {
            formatter: p => `${p.value[0]}<br>${fmtMoney(p.value[1])}`,
        },
        visualMap: {
            min: 0, max,
            orient: 'horizontal',
            left: 'center',
            top: 0,
            textStyle: { color: '#8b949e' },
            inRange: { color: ['#0e1116', '#1f4068', '#3a72c4', '#79b3ff'] },
        },
        calendar: {
            range: yearStart === yearEnd ? yearStart : [sortedDates[0], sortedDates[sortedDates.length - 1]],
            cellSize: ['auto', 14],
            top: 50, bottom: 20, left: 40, right: 20,
            itemStyle: { color: '#0e1116', borderColor: '#1c232c', borderWidth: 1 },
            yearLabel: { color: '#8b949e' },
            monthLabel: { color: '#8b949e' },
            dayLabel: { color: '#8b949e' },
            splitLine: { lineStyle: { color: '#2a3340' } },
        },
        series: [{
            type: 'heatmap',
            coordinateSystem: 'calendar',
            data: points.map(p => [p.date, Number(p.total)]),
        }],
    }, true);
}

function renderSankey(graph) {
    const empty = !graph.nodes || graph.nodes.length === 0 || graph.links.length === 0;
    setEmpty('empty-sankey', empty);
    const chart = charts.sankey ||= echarts.init($('#chart-sankey'), 'dark');
    if (empty) { chart.clear(); return; }
    chart.setOption({
        backgroundColor: 'transparent',
        tooltip: {
            trigger: 'item',
            formatter: p => p.dataType === 'edge'
                ? `${p.data.source} → ${p.data.target}<br>${fmtMoney(p.data.value)}`
                : `${p.data.name}`,
        },
        series: [{
            type: 'sankey',
            data: graph.nodes,
            links: graph.links.map(l => ({ ...l, value: Number(l.value) })),
            emphasis: { focus: 'adjacency' },
            lineStyle: { color: 'gradient', curveness: 0.5 },
            label: { color: '#e6edf3' },
            nodeAlign: 'justify',
            nodeWidth: 14,
            nodeGap: 12,
            left: 10, right: 100, top: 20, bottom: 20,
        }],
    }, true);
}

function renderTreemap(data) {
    setEmpty('empty-treemap', data.length === 0);
    const chart = charts.treemap ||= echarts.init($('#chart-treemap'), 'dark');
    if (data.length === 0) { chart.clear(); return; }
    const toNumeric = nodes => nodes.map(n => ({
        name: n.name,
        value: Number(n.value),
        children: n.children && n.children.length ? toNumeric(n.children) : undefined,
    }));
    chart.setOption({
        backgroundColor: 'transparent',
        tooltip: { formatter: p => `${p.name}<br>${fmtMoney(p.value)}` },
        series: [{
            type: 'treemap',
            data: toNumeric(data),
            roam: false,
            nodeClick: 'zoomToNode',
            breadcrumb: { itemStyle: { color: '#161b22', borderColor: '#2a3340', textStyle: { color: '#8b949e' } } },
            label: { color: '#e6edf3', formatter: '{b}\n{c}' },
            upperLabel: { show: true, height: 24, color: '#e6edf3' },
            levels: [
                { itemStyle: { borderColor: '#0e1116', borderWidth: 0, gapWidth: 1 } },
                {
                    itemStyle: { borderColor: '#0e1116', borderWidth: 5, gapWidth: 1 },
                    upperLabel: { show: true },
                },
                {
                    itemStyle: { borderColor: '#1c232c', borderWidth: 2, gapWidth: 1 },
                    upperLabel: { show: false },
                },
            ],
        }],
    }, true);
}

function renderTimeHeatmap(points) {
    setEmpty('empty-time-heatmap', points.length === 0);
    const chart = charts.timeHeatmap ||= echarts.init($('#chart-time-heatmap'), 'dark');
    if (points.length === 0) { chart.clear(); return; }
    const days = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
    const hours = Array.from({ length: 24 }, (_, h) => `${String(h).padStart(2, '0')}:00`);
    const data = points.map(p => [p.hour, p.day_of_week, Number(p.total)]);
    const max = Math.max(1, ...data.map(d => d[2]));
    chart.setOption({
        backgroundColor: 'transparent',
        tooltip: {
            position: 'top',
            formatter: p => `${days[p.value[1]]} ${hours[p.value[0]]}<br>${fmtMoney(p.value[2])}`,
        },
        grid: { left: 50, right: 20, top: 40, bottom: 60 },
        xAxis: { type: 'category', data: hours, splitArea: { show: true }, axisLabel: { color: '#8b949e', rotate: 35 } },
        yAxis: { type: 'category', data: days, splitArea: { show: true }, axisLabel: { color: '#8b949e' } },
        visualMap: {
            min: 0, max,
            calculable: true,
            orient: 'horizontal',
            left: 'center',
            bottom: 0,
            textStyle: { color: '#8b949e' },
            inRange: { color: ['#0e1116', '#1f4068', '#3a72c4', '#79b3ff'] },
        },
        series: [{
            type: 'heatmap',
            data,
            label: { show: false },
            emphasis: { itemStyle: { shadowBlur: 6, shadowColor: 'rgba(79, 156, 255, 0.5)' } },
        }],
    }, true);
}

// ── Forecast tab ──────────────────────────────────────────────────────────────

let forecastTargetBalance = null;

async function refreshForecast() {
    const params = {};
    const ids = Array.from(state.accountIds);
    if (ids.length > 0 && ids.length < state.accounts.length) params.account_ids = ids;
    if (forecastTargetBalance != null) params.target_balance = forecastTargetBalance;

    const data = await fetchJSON(`${BASE}/api/forecast`, params);
    renderForecastMetrics(data);
    renderForecastChart(data);
    renderForecastTarget(data);
}

function renderForecastMetrics(d) {
    const cur = Number(d.current_net_worth);
    $('#forecast-current').textContent = fmtMoney(cur);
    $('#forecast-current-at').textContent = d.current_net_worth_at
        ? `as of ${fmtRelative(d.current_net_worth_at)}`
        : 'no balance snapshots yet';

    const monthlyNet = Number(d.monthly_net);
    const el = $('#forecast-monthly-net');
    el.textContent = fmtMoney(monthlyNet);
    el.style.color = monthlyNet >= 0 ? 'var(--good)' : 'var(--bad)';
    $('#forecast-monthly-detail').textContent =
        `Income ${fmtMoney(Number(d.monthly_income))} − Expenses ${fmtMoney(Number(d.monthly_expenses))}`;
}

function renderForecastChart(d) {
    const empty = d.history.length === 0 && Number(d.current_net_worth) === 0;
    setEmpty('empty-forecast', empty);
    const chart = charts.forecast ||= echarts.init($('#chart-forecast'), 'dark');
    if (empty) { chart.clear(); return; }
    const histData = d.history.map(p => [p.month, Number(p.balance)]);
    const projData = d.projection.map(p => [p.month, Number(p.balance)]);
    // Stitch the projection to the last history point so the line is continuous.
    if (histData.length > 0) projData.unshift(histData[histData.length - 1]);
    chart.setOption({
        backgroundColor: 'transparent',
        tooltip: { trigger: 'axis' },
        legend: { textStyle: { color: '#8b949e' }, top: 0 },
        grid: { left: 70, right: 20, top: 40, bottom: 50 },
        xAxis: { type: 'category', boundaryGap: false, axisLabel: { color: '#8b949e' } },
        yAxis: { type: 'value', axisLabel: { color: '#8b949e' } },
        series: [
            {
                name: 'Actual',
                type: 'line',
                showSymbol: true,
                data: histData,
                lineStyle: { color: '#4f9cff', width: 2 },
                itemStyle: { color: '#4f9cff' },
                areaStyle: { color: 'rgba(79, 156, 255, 0.15)' },
            },
            {
                name: 'Projected',
                type: 'line',
                showSymbol: false,
                data: projData,
                lineStyle: { color: '#3fb950', width: 2, type: 'dashed' },
                itemStyle: { color: '#3fb950' },
            },
        ],
    }, true);
}

function renderForecastTarget(d) {
    const result = $('#forecast-target-result');
    const detail = $('#forecast-target-detail');
    if (!d.target) {
        result.textContent = '—';
        detail.textContent = 'Enter a target balance to project a date.';
        return;
    }
    if (d.target.months_to_target == null) {
        result.textContent = 'Never';
        detail.textContent = 'Monthly net cashflow is not positive.';
        result.style.color = 'var(--bad)';
        return;
    }
    if (d.target.months_to_target === 0) {
        result.textContent = 'Already there';
        detail.textContent = `Target ${fmtMoney(Number(d.target.balance))} reached.`;
        result.style.color = 'var(--good)';
        return;
    }
    const months = d.target.months_to_target;
    const years = months / 12;
    result.textContent = years >= 1
        ? `${years.toFixed(1)} years`
        : `${months.toFixed(1)} months`;
    result.style.color = 'var(--accent)';
    detail.textContent = d.target.date_at_target
        ? `Reach ${fmtMoney(Number(d.target.balance))} around ${d.target.date_at_target}`
        : '';
}

function initForecastTargetForm() {
    $('#forecast-target-form').addEventListener('submit', e => {
        e.preventDefault();
        const v = e.target.elements.target.value.trim();
        forecastTargetBalance = v ? v : null;
        refreshForecast();
    });
}

// ── Refresh orchestration ─────────────────────────────────────────────────────

async function refreshCurrentTab() {
    setSpinner(true);
    try {
        if (state.activeTab === 'spending') await refreshSpending();
        else if (state.activeTab === 'networth') await refreshNetWorth();
        else if (state.activeTab === 'cashflow') await refreshCashflow();
        else if (state.activeTab === 'insights') await refreshInsights();
        else if (state.activeTab === 'forecast') await refreshForecast();
    } catch (e) {
        console.error(e);
    } finally {
        setSpinner(false);
    }
}

async function refreshAll() {
    setSpinner(true);
    try {
        await Promise.all([refreshAccounts(), refreshPollStatus()]);
        await refreshCurrentTab();
    } catch (e) {
        console.error(e);
    } finally {
        setSpinner(false);
    }
}

// ── Init ──────────────────────────────────────────────────────────────────────

function init() {
    initFilters();
    initNetWorthToggle();
    initBudgetForm();
    initForecastTargetForm();

    $$('.tab').forEach(b => b.addEventListener('click', () => activateTab(b.dataset.tab)));
    $('#btn-refresh').addEventListener('click', refreshAll);
    $('#tx-prev').addEventListener('click', () => {
        state.txOffset = Math.max(0, state.txOffset - state.txLimit);
        refreshTransactionsTable();
    });
    $('#tx-next').addEventListener('click', () => {
        state.txOffset += state.txLimit;
        refreshTransactionsTable();
    });
    $('#tx-filter-clear').addEventListener('click', () => {
        state.txCategoryFilter = null;
        $('#tx-filter-pill').hidden = true;
        $('#tx-filter-clear').hidden = true;
        state.txOffset = 0;
        refreshTransactionsTable();
    });

    window.addEventListener('resize', () => Object.values(charts).forEach(c => c && c.resize()));

    refreshAll();
    setInterval(refreshAll, REFRESH_MS);
}

document.addEventListener('DOMContentLoaded', init);
