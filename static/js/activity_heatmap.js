(function () {
    'use strict';

    const DOW_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
    const HOUR_LABELS = Array.from({ length: 24 }, (_, i) => String(i).padStart(2, '0'));

    const METRIC_NOUNS = {
        posts: 'posts',
        edits: 'edits',
        deletes: 'deletions',
        joins: 'user joins',
        media: 'media posts',
    };
    function metricNoun(metric) { return METRIC_NOUNS[metric] || 'events'; }

    // Sequential amber palette for absolute counts. Diverging RdYlGn used in compare mode.
    function colorAbsolute(intensity /* 0..1 */) {
        const a = 0.10 + intensity * 0.80;
        return `rgba(251, 191, 36, ${a.toFixed(3)})`;
    }
    function colorDiverging(delta, maxAbs) {
        if (maxAbs === 0) return 'rgba(120, 120, 120, 0.15)';
        const ratio = Math.min(Math.abs(delta) / maxAbs, 1);
        const alpha = (0.15 + ratio * 0.75).toFixed(3);
        if (delta >= 0) return `rgba(34, 197, 94, ${alpha})`;   // green = increased
        return `rgba(239, 68, 68, ${alpha})`;                    // red = decreased
    }

    window.initActivityHeatmap = function (cardEl) {
        const state = {
            scope: cardEl.dataset.activityScope,
            scopeId: cardEl.dataset.activityId || null,
            apiUrl: cardEl.dataset.activityApi,
            days: '30',
            // Custom-range mode: when days==='custom', startDate/endDate (YYYY-MM-DD)
            // are sent to the API instead of computing the range from days.
            startDate: null,
            endDate: null,
            layout: 'calendar',
            metric: 'posts',
            tz: 'UTC',
            compare: false,
            chart: null,
            // For aggregate page: extra filters injected via cardEl.dataset
            aggregateFilters: { tags: [], channelType: [] },
            // For user page: channel filter injected via setUserChannelFilter()
            channelIds: [],
        };

        const customRangeEl = cardEl.querySelector('.activity-custom-range');
        const startDateInput = cardEl.querySelector('.activity-start-date');
        const endDateInput = cardEl.querySelector('.activity-end-date');
        const applyCustomBtn = cardEl.querySelector('.activity-apply-custom');

        const canvas = cardEl.querySelector('.activity-canvas');
        const loadingEl = cardEl.querySelector('.activity-loading');
        const emptyEl = cardEl.querySelector('.activity-empty');
        const summaryEl = cardEl.querySelector('.activity-summary');

        // Wire control buttons (segmented "btn-group" pattern with .active toggling)
        cardEl.querySelectorAll('[data-control]').forEach(group => {
            const ctrl = group.dataset.control;
            if (group.tagName === 'SELECT') {
                group.addEventListener('change', () => {
                    if (ctrl === 'tz') state.tz = group.value;
                    refresh();
                });
            } else if (group.tagName === 'INPUT' && group.type === 'checkbox') {
                group.addEventListener('change', () => {
                    if (ctrl === 'compare') state.compare = group.checked;
                    refresh();
                });
            } else {
                // Segmented btn-group: wire each button
                group.querySelectorAll('button').forEach(btn => {
                    btn.addEventListener('click', () => {
                        group.querySelectorAll('button').forEach(b => b.classList.remove('active'));
                        btn.classList.add('active');
                        if (ctrl === 'days') {
                            state.days = btn.dataset.days;
                            if (state.days === 'custom') {
                                // Show the date inputs; default to last 7 days if empty.
                                if (customRangeEl) customRangeEl.classList.replace('d-none', 'd-flex');
                                seedCustomDatesIfEmpty();
                                return;  // wait for user to click Apply
                            } else if (customRangeEl) {
                                customRangeEl.classList.replace('d-flex', 'd-none');
                                state.startDate = null;
                                state.endDate = null;
                            }
                        }
                        if (ctrl === 'layout') state.layout = btn.dataset.layout;
                        if (ctrl === 'metric') state.metric = btn.dataset.metric;
                        refresh();
                    });
                });
            }
        });

        // Seed the custom-range inputs with a sensible default (last 7 days) so
        // the user has something to apply if they don't pick anything.
        function seedCustomDatesIfEmpty() {
            if (!startDateInput || !endDateInput) return;
            if (!startDateInput.value || !endDateInput.value) {
                const today = new Date();
                const weekAgo = new Date(today);
                weekAgo.setDate(today.getDate() - 6);
                endDateInput.value = ymd(today);
                startDateInput.value = ymd(weekAgo);
            }
        }

        function ymd(d) {
            return d.toISOString().slice(0, 10);
        }

        if (applyCustomBtn) {
            applyCustomBtn.addEventListener('click', () => {
                if (!startDateInput.value || !endDateInput.value) return;
                if (startDateInput.value > endDateInput.value) {
                    if (window.showToast) window.showToast('Start date must be before end date', 'error');
                    return;
                }
                state.days = 'custom';
                state.startDate = startDateInput.value;
                state.endDate = endDateInput.value;
                refresh();
            });
        }

        // Public hook for aggregate page filter chips
        cardEl.activitySetAggregateFilters = function (filters) {
            state.aggregateFilters = Object.assign({ tags: [], channelType: [] }, filters);
            refresh();
        };

        // Public hook for user page channel checklist
        cardEl.activitySetChannelFilter = function (channelIds) {
            state.channelIds = channelIds || [];
            refresh();
        };

        function buildQuery() {
            const params = new URLSearchParams({
                scope: state.scope,
                days: state.days,
                tz: state.tz,
                layout: state.layout,
                metric: state.metric,
            });
            if (state.scopeId) params.set('id', state.scopeId);
            if (state.compare) params.set('compare', '1');
            if (state.days === 'custom' && state.startDate && state.endDate) {
                params.set('start', state.startDate);
                params.set('end', state.endDate);
            }
            if (state.scope === 'user' && state.channelIds.length) {
                params.set('channel_id', state.channelIds.join(','));
            }
            if (state.scope === 'aggregate') {
                if (state.aggregateFilters.tags.length) params.set('tags', state.aggregateFilters.tags.join(','));
                if (state.aggregateFilters.channelType.length) params.set('channel_type', state.aggregateFilters.channelType.join(','));
            }
            return params.toString();
        }

        async function refresh() {
            loadingEl.classList.remove('d-none');
            emptyEl.classList.add('d-none');
            try {
                const resp = await fetch(`${state.apiUrl}?${buildQuery()}`, {
                    headers: { 'X-Requested-With': 'XMLHttpRequest' },
                });
                if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                const data = await resp.json();
                render(data);
            } catch (err) {
                console.error('Activity heatmap fetch failed:', err);
                if (window.showToast) window.showToast('Failed to load activity data', 'error');
            } finally {
                loadingEl.classList.add('d-none');
            }
        }

        function render(data) {
            const buckets = data.buckets || [];
            if (!buckets.length) {
                if (state.chart) { state.chart.destroy(); state.chart = null; }
                emptyEl.classList.remove('d-none');
                summaryEl.textContent = '';
                return;
            }
            emptyEl.classList.add('d-none');

            if (state.chart) { state.chart.destroy(); state.chart = null; }
            if (data.layout === 'hourdow') renderHourDow(data);
            else renderCalendar(data);

            updateSummary(data);
        }

        function renderCalendar(data) {
            // Build a date-keyed map of compare deltas (current - baseline) when in compare mode.
            const byDate = new Map();
            data.buckets.forEach(b => byDate.set(b.date, b.count));

            let baselineByDate = null;
            if (state.compare && data.compare_buckets) {
                baselineByDate = new Map();
                data.compare_buckets.forEach(b => baselineByDate.set(b.date, b.count));
            }

            // Cells = one per day. x=ISO week index (relative to start), y=day-of-week.
            const dates = Array.from(byDate.keys()).sort();
            const minDate = new Date(dates[0]);
            const maxDate = new Date(dates[dates.length - 1]);

            // Anchor the cell grid to the API window. For custom range, the API
            // returns start/end ISO strings; for presets we fall back to "N days
            // back from today" as before.
            let startDay, endDay;
            if (data.days_label === 'custom' && data.start && data.end) {
                startDay = new Date(data.start);
                endDay = new Date(data.end);
            } else {
                const rangeDays = Math.max(parseInt(state.days, 10), 1);
                endDay = new Date();
                startDay = new Date(endDay);
                startDay.setDate(endDay.getDate() - rangeDays + 1);
            }

            const anomalySet = new Set((data.anomalies || []).map(d => d));

            const cells = [];
            const cursor = new Date(startDay);
            // Find first Monday on or before startDay so the grid is column-aligned by ISO week
            const startDow = (cursor.getDay() + 6) % 7;  // Mon=0..Sun=6
            cursor.setDate(cursor.getDate() - startDow);

            const todayStr = endDay.toISOString().slice(0, 10);
            let weekIdx = 0;
            const maxCount = Math.max(...data.buckets.map(b => b.count), 1);
            const maxAbsDelta = baselineByDate ?
                Math.max(...Array.from(byDate.entries()).map(([d, v]) => Math.abs(v - (baselineByDate.get(d) || 0))), 1) :
                1;

            while (cursor <= endDay) {
                const dStr = cursor.toISOString().slice(0, 10);
                const inRange = (cursor >= startDay && cursor <= endDay);
                const count = byDate.get(dStr) || 0;
                const baseline = baselineByDate ? (baselineByDate.get(dStr) || 0) : null;
                const delta = baseline !== null ? (count - baseline) : null;

                cells.push({
                    x: weekIdx,
                    y: (cursor.getDay() + 6) % 7,  // Mon=0..Sun=6
                    v: inRange ? count : null,
                    date: dStr,
                    baseline: baseline,
                    delta: delta,
                    anomaly: inRange && anomalySet.has(dStr),
                });

                cursor.setDate(cursor.getDate() + 1);
                if ((cursor.getDay() + 6) % 7 === 0) weekIdx++;
            }

            const numWeeks = weekIdx + 1;
            const weekLabels = Array.from({ length: numWeeks }, (_, i) => `w${i}`);
            const CELL_GAP = 3;

            // Convert cells to use category labels for x/y so chartjs-chart-matrix's
            // category-axis offset behavior places them inside their slot (no overlap).
            const visibleCells = cells
                .filter(c => c.v !== null)
                .map(c => ({
                    x: weekLabels[c.x],
                    y: DOW_LABELS[c.y],
                    v: c.v,
                    date: c.date,
                    baseline: c.baseline,
                    delta: c.delta,
                    anomaly: c.anomaly,
                }));

            state.chart = new Chart(canvas, {
                type: 'matrix',
                data: {
                    datasets: [{
                        label: 'Activity',
                        data: visibleCells,
                        backgroundColor: ctx => {
                            const c = ctx.raw;
                            if (c.v === null) return 'transparent';
                            if (state.compare && c.delta !== null) return colorDiverging(c.delta, maxAbsDelta);
                            return colorAbsolute(c.v / maxCount);
                        },
                        borderColor: ctx => ctx.raw && ctx.raw.anomaly ? 'rgb(239, 68, 68)' : 'rgba(255,255,255,0.04)',
                        borderWidth: ctx => ctx.raw && ctx.raw.anomaly ? 2 : 1,
                        width: ({ chart }) => {
                            const w = chart.chartArea && chart.chartArea.width;
                            return w ? Math.max(w / numWeeks - CELL_GAP, 2) : 0;
                        },
                        height: ({ chart }) => {
                            const h = chart.chartArea && chart.chartArea.height;
                            return h ? Math.max(h / 7 - CELL_GAP, 2) : 0;
                        },
                    }],
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    onClick: (evt, els) => {
                        if (!els.length || !data.drilldown) return;
                        const cell = state.chart.data.datasets[0].data[els[0].index];
                        const url = data.drilldown.replace('{start}', cell.date).replace('{end}', cell.date);
                        window.location.href = url;
                    },
                    plugins: {
                        legend: { display: false },
                        tooltip: {
                            callbacks: {
                                title: items => items[0].raw.date,
                                label: ctx => {
                                    const c = ctx.raw;
                                    const lines = [`${metricNoun(state.metric).replace(/^./, s => s.toUpperCase())}: ${c.v.toLocaleString()}`];
                                    if (state.compare && c.baseline !== null) {
                                        const sign = c.delta >= 0 ? '+' : '';
                                        lines.push(`Prior period: ${c.baseline.toLocaleString()} (${sign}${c.delta.toLocaleString()})`);
                                    }
                                    if (c.anomaly) lines.push('Anomaly: > 2σ above baseline');
                                    return lines;
                                },
                            },
                        },
                    },
                    scales: {
                        x: {
                            type: 'category',
                            position: 'bottom',
                            labels: weekLabels,
                            offset: true,
                            ticks: { display: false },
                            grid: { display: false },
                            border: { display: false },
                        },
                        y: {
                            type: 'category',
                            labels: DOW_LABELS,
                            offset: true,
                            ticks: { autoSkip: false },
                            grid: { display: false },
                            border: { display: false },
                        },
                    },
                },
            });
        }

        function renderHourDow(data) {
            const cellMap = new Map(); // key = `${dow}_${hour}`
            data.buckets.forEach(b => cellMap.set(`${b.dow}_${b.hour}`, b.count));

            let baselineMap = null;
            if (state.compare && data.compare_buckets) {
                baselineMap = new Map();
                data.compare_buckets.forEach(b => baselineMap.set(`${b.dow}_${b.hour}`, b.count));
            }

            const anomalySet = new Set((data.anomalies || []).map(a => `${a.dow}_${a.hour}`));

            const cells = [];
            for (let dow = 1; dow <= 7; dow++) {
                for (let hour = 0; hour < 24; hour++) {
                    const key = `${dow}_${hour}`;
                    const count = cellMap.get(key) || 0;
                    const baseline = baselineMap ? (baselineMap.get(key) || 0) : null;
                    cells.push({
                        x: HOUR_LABELS[hour],          // category label
                        y: DOW_LABELS[dow - 1],        // category label
                        v: count,
                        baseline: baseline,
                        delta: baseline !== null ? count - baseline : null,
                        anomaly: anomalySet.has(key),
                        dow: dow,
                        hour: hour,
                    });
                }
            }

            const maxCount = Math.max(...cells.map(c => c.v), 1);
            const maxAbsDelta = baselineMap ?
                Math.max(...cells.map(c => Math.abs(c.delta || 0)), 1) : 1;

            const CELL_GAP = 3;

            state.chart = new Chart(canvas, {
                type: 'matrix',
                data: {
                    datasets: [{
                        label: 'Activity',
                        data: cells,
                        backgroundColor: ctx => {
                            const c = ctx.raw;
                            if (state.compare && c.delta !== null) return colorDiverging(c.delta, maxAbsDelta);
                            return colorAbsolute(c.v / maxCount);
                        },
                        borderColor: ctx => ctx.raw.anomaly ? 'rgb(239, 68, 68)' : 'rgba(255,255,255,0.04)',
                        borderWidth: ctx => ctx.raw.anomaly ? 2 : 1,
                        width: ({ chart }) => {
                            const w = chart.chartArea && chart.chartArea.width;
                            return w ? Math.max(w / 24 - CELL_GAP, 2) : 0;
                        },
                        height: ({ chart }) => {
                            const h = chart.chartArea && chart.chartArea.height;
                            return h ? Math.max(h / 7 - CELL_GAP, 2) : 0;
                        },
                    }],
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: { display: false },
                        tooltip: {
                            callbacks: {
                                title: items => `${DOW_LABELS[items[0].raw.dow - 1]} ${HOUR_LABELS[items[0].raw.hour]}:00 ${state.tz}`,
                                label: ctx => {
                                    const c = ctx.raw;
                                    const lines = [`${metricNoun(state.metric).replace(/^./, s => s.toUpperCase())}: ${c.v.toLocaleString()}`];
                                    if (state.compare && c.baseline !== null) {
                                        const sign = c.delta >= 0 ? '+' : '';
                                        lines.push(`Prior period: ${c.baseline.toLocaleString()} (${sign}${c.delta.toLocaleString()})`);
                                    }
                                    if (c.anomaly) lines.push('Anomaly vs other ' + HOUR_LABELS[c.hour] + ':00 cells');
                                    return lines;
                                },
                            },
                        },
                    },
                    scales: {
                        x: {
                            type: 'category',
                            position: 'bottom',
                            labels: HOUR_LABELS,
                            offset: true,
                            ticks: { autoSkip: false, maxRotation: 0, callback: (v, i) => i % 3 === 0 ? HOUR_LABELS[i] : '' },
                            grid: { display: false },
                            border: { display: false },
                            title: { display: true, text: `Hour of day (${state.tz})` },
                        },
                        y: {
                            type: 'category',
                            labels: DOW_LABELS,
                            offset: true,
                            ticks: { autoSkip: false },
                            grid: { display: false },
                            border: { display: false },
                        },
                    },
                },
            });
        }

        function updateSummary(data) {
            const s = data.summary || {};
            const parts = [];
            parts.push(`${(s.total || 0).toLocaleString()} ${metricNoun(state.metric)}`);
            if (s.active_buckets !== undefined) parts.push(`${s.active_buckets} active days`);
            const peak = findPeak(data);
            if (peak) parts.push(`peak ${peak} ${data.tz}`);
            if (data.anomalies && data.anomalies.length) parts.push(`${data.anomalies.length} anomaly bucket${data.anomalies.length === 1 ? '' : 's'}`);
            summaryEl.textContent = parts.join(' · ');
        }

        function findPeak(data) {
            if (!data.buckets || !data.buckets.length) return null;
            if (data.layout === 'hourdow') {
                let best = data.buckets[0];
                for (const b of data.buckets) if (b.count > best.count) best = b;
                return `${DOW_LABELS[best.dow - 1]} ${HOUR_LABELS[best.hour]}:00`;
            }
            let best = data.buckets[0];
            for (const b of data.buckets) if (b.count > best.count) best = b;
            return best.date;
        }

        // Initial load
        refresh();
        return cardEl;
    };
})();
