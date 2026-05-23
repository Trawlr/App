(function () {
    'use strict';

    const STATE = {
        userPk: null,
        urls: null,
    };
    let copostNetwork = null;

    window.initUserNetwork = function (cfg) {
        STATE.userPk = cfg.userPk;
        STATE.urls = cfg.urls;

        document.getElementById('un-days').addEventListener('change', refreshAll);
        document.getElementById('un-copost-window').addEventListener('change', refreshCopost);
        const minEl = document.getElementById('un-copost-min');
        let minDebounce = null;
        minEl.addEventListener('input', () => {
            clearTimeout(minDebounce);
            minDebounce = setTimeout(refreshCopost, 350);
        });

        refreshAll();
    };

    function currentDays() {
        return document.getElementById('un-days').value;
    }

    function refreshAll() {
        refreshLanes();
        refreshFlow();
        refreshCopost();
    }

    // ------------------------------------------------------------------
    // Panel 1: Swim lanes
    // ------------------------------------------------------------------
    async function refreshLanes() {
        const container = document.getElementById('un-lanes-container');
        container.innerHTML = loadingHtml();
        try {
            const r = await fetch(`${STATE.urls.lanes}?days=${currentDays()}`);
            if (!r.ok) throw new Error(`HTTP ${r.status}`);
            const data = await r.json();
            renderLanes(data, container);
        } catch (err) {
            console.error('lanes fetch failed', err);
            container.innerHTML = `<div class="un-empty">Failed to load lanes.</div>`;
        }
    }

    function renderLanes(data, container) {
        if (!data.groups || data.groups.length === 0) {
            container.innerHTML = `<div class="un-empty">
                No posts from this user in the selected window.
            </div>`;
            return;
        }
        const startMs = new Date(data.start).getTime();
        const endMs = new Date(data.end).getTime();
        const span = Math.max(1, endMs - startMs);

        const rows = data.groups.map(g => {
            const pins = (g.pins || []).map(p => {
                const t = new Date(p.ts).getTime();
                const pct = Math.max(0, Math.min(100, ((t - startMs) / span) * 100));
                const url = STATE.urls.sourcePosts
                    .replace('__SOURCE_PK__', g.id)
                    .replace('__MSG_ID__', p.message_id);
                return `<a class="un-lane-pin kind-${p.kind}"
                           style="left: ${pct.toFixed(3)}%;"
                           href="${url}"
                           title="${escapeHtml(p.kind)} · ${formatTs(p.ts)}"></a>`;
            }).join('');
            const groupHref = STATE.urls.sourcePosts
                .replace('__SOURCE_PK__', g.id)
                .split('?')[0];
            return `
                <div class="un-lane-row">
                    <div class="un-lane-label">
                        <a href="${groupHref}" title="${escapeHtml(g.title)}">${escapeHtml(g.title)}</a>
                    </div>
                    <div class="un-lane-count">${g.count}</div>
                    <div class="un-lane-track">${pins}</div>
                </div>`;
        }).join('');

        const ticks = buildTimeTicks(startMs, endMs).map(t =>
            `<span>${escapeHtml(t)}</span>`).join('');

        container.innerHTML = rows + `
            <div class="un-lane-axis">
                <div class="un-lane-axis-ticks">${ticks}</div>
            </div>`;
    }

    function buildTimeTicks(startMs, endMs) {
        const span = endMs - startMs;
        const days = span / 86400000;
        const ticks = [];
        const n = 5;
        for (let i = 0; i <= n; i++) {
            const t = new Date(startMs + (span * i) / n);
            const mm = pad(t.getMonth() + 1);
            const dd = pad(t.getDate());
            ticks.push(days <= 7
                ? `${mm}/${dd} ${pad(t.getHours())}:${pad(t.getMinutes())}`
                : `${t.getFullYear()}-${mm}-${dd}`);
        }
        return ticks;
    }

    // ------------------------------------------------------------------
    // Panel 2: Sankey
    // ------------------------------------------------------------------
    async function refreshFlow() {
        const container = document.getElementById('un-flow-container');
        const summary = document.getElementById('un-flow-summary');
        container.innerHTML = loadingHtml();
        summary.textContent = '';
        try {
            const r = await fetch(`${STATE.urls.flow}?days=${currentDays()}`);
            if (!r.ok) throw new Error(`HTTP ${r.status}`);
            const data = await r.json();
            renderFlow(data, container, summary);
        } catch (err) {
            console.error('flow fetch failed', err);
            container.innerHTML = `<div class="un-empty">Failed to load flow.</div>`;
        }
    }

    function renderFlow(data, container, summary) {
        if (!data.user_total) {
            container.innerHTML = `<div class="un-empty">No posts from this user in the selected window.</div>`;
            return;
        }
        summary.textContent =
            `${data.user_total} posts · ` +
            `${data.totals.inbound_forwards} forwarded in · ` +
            `${data.totals.outbound_forwards} re-broadcast out`;

        // Build node + link arrays for d3-sankey.
        // Nodes: [inbound...] + [centre user] + [outbound...]
        // Links: each inbound -> centre, centre -> each outbound.
        const nodes = [];
        const links = [];
        const centreIdx = data.inbound.length;

        data.inbound.forEach(b => {
            nodes.push({ name: b.label, side: 'in', meta: b });
        });
        nodes.push({ name: 'this user', side: 'centre', meta: { count: data.user_total } });
        data.outbound.forEach(b => {
            nodes.push({ name: b.label, side: 'out', meta: b });
        });

        data.inbound.forEach((b, i) => {
            links.push({ source: i, target: centreIdx, value: Math.max(1, b.count) });
        });
        data.outbound.forEach((b, i) => {
            links.push({ source: centreIdx, target: centreIdx + 1 + i, value: Math.max(1, b.count) });
        });

        if (links.length === 0) {
            container.innerHTML = `<div class="un-empty">
                No forwards in or out of this user. Their posts are entirely original
                content with no observed re-broadcast.
            </div>`;
            return;
        }

        container.innerHTML = `<svg id="un-flow-svg"></svg>`;
        const svg = d3.select('#un-flow-svg');
        const width = container.clientWidth;
        const height = 360;
        svg.attr('viewBox', `0 0 ${width} ${height}`);

        const sankey = d3.sankey()
            .nodeWidth(14)
            .nodePadding(10)
            .extent([[120, 12], [width - 120, height - 12]]);

        const graph = sankey({
            nodes: nodes.map(d => ({ ...d })),
            links: links.map(d => ({ ...d })),
        });

        const colorBySide = side => side === 'in' ? '#06b6d4'
                                : side === 'out' ? '#fbbf24'
                                : '#10b981';

        // Links
        svg.append('g')
            .selectAll('path')
            .data(graph.links)
            .join('path')
            .attr('d', d3.sankeyLinkHorizontal())
            .attr('class', 'un-flow-link')
            .attr('stroke', d => colorBySide(d.source.side))
            .attr('stroke-width', d => Math.max(1, d.width))
            .append('title')
            .text(d => `${d.source.name} → ${d.target.name}\n${d.value} msgs`);

        // Nodes
        const nodeG = svg.append('g')
            .selectAll('g')
            .data(graph.nodes)
            .join('g')
            .attr('class', 'un-flow-node');

        nodeG.append('rect')
            .attr('x', d => d.x0)
            .attr('y', d => d.y0)
            .attr('height', d => Math.max(1, d.y1 - d.y0))
            .attr('width', d => d.x1 - d.x0)
            .attr('fill', d => colorBySide(d.side))
            .append('title')
            .text(d => `${d.name}\n${d.value || (d.meta && d.meta.count) || ''} msgs`);

        nodeG.append('text')
            .attr('x', d => d.side === 'in' ? d.x0 - 6 : d.x1 + 6)
            .attr('y', d => (d.y0 + d.y1) / 2)
            .attr('dy', '0.35em')
            .attr('text-anchor', d => d.side === 'in' ? 'end' : 'start')
            .text(d => truncate(d.name, 28));
    }

    // ------------------------------------------------------------------
    // Panel 3: Co-poster vis.js
    // ------------------------------------------------------------------
    async function refreshCopost() {
        const container = document.getElementById('un-copost-container');
        const days = currentDays();
        const window_seconds = document.getElementById('un-copost-window').value;
        const min_overlap = document.getElementById('un-copost-min').value || 3;
        container.innerHTML = loadingHtml();
        try {
            const r = await fetch(
                `${STATE.urls.copost}?days=${days}&window_seconds=${window_seconds}&min_overlap=${min_overlap}`
            );
            if (!r.ok) throw new Error(`HTTP ${r.status}`);
            const data = await r.json();
            renderCopost(data, container);
        } catch (err) {
            console.error('copost fetch failed', err);
            container.innerHTML = `<div class="un-empty">Failed to load co-posters.</div>`;
        }
    }

    function renderCopost(data, container) {
        if (copostNetwork) {
            copostNetwork.destroy();
            copostNetwork = null;
        }
        if (!data.nodes || data.nodes.length <= 1) {
            container.innerHTML = `<div class="un-empty">
                No other accounts posted matching content within ±${data.window_seconds}s
                (min ${data.min_overlap} overlaps).
            </div>`;
            return;
        }
        container.innerHTML = `<div id="un-copost-graph"></div>`;
        const el = document.getElementById('un-copost-graph');

        const visNodes = data.nodes.map(n => ({
            id: n.id,
            label: n.label,
            value: n.size,
            shape: 'dot',
            color: n.group === 'focus'
                ? { background: '#fbbf24', border: '#fbbf24' }
                : { background: '#f97316', border: '#f97316' },
            font: { color: '#f1f5f9', size: 12, face: 'Inter, sans-serif' },
            title: buildCopostTooltip(n),
            data: n,
        }));
        const visEdges = data.edges.map((e, i) => ({
            id: i, from: e.from, to: e.to, value: e.value, title: e.title,
            color: { color: '#374151', highlight: '#fbbf24' },
        }));

        copostNetwork = new vis.Network(
            el,
            { nodes: new vis.DataSet(visNodes), edges: new vis.DataSet(visEdges) },
            {
                nodes: { borderWidth: 2 },
                edges: { smooth: { type: 'continuous', roundness: 0.4 }, scaling: { min: 1, max: 8 } },
                physics: {
                    barnesHut: {
                        gravitationalConstant: -2400, centralGravity: 0.3,
                        springLength: 110, springConstant: 0.05, damping: 0.4,
                    },
                    stabilization: { iterations: 120, fit: true },
                },
                interaction: { hover: true, tooltipDelay: 150 },
            },
        );

        copostNetwork.on('click', evt => {
            if (!evt.nodes || evt.nodes.length === 0) return;
            const id = evt.nodes[0];
            const node = visNodes.find(n => n.id === id);
            if (!node || !node.data || !node.data.user_pk) return;
            if (node.data.user_pk === STATE.userPk) return; // self
            window.location.href = STATE.urls.userDetail.replace('__USER_PK__', node.data.user_pk);
        });
    }

    function buildCopostTooltip(n) {
        const parts = [`<strong>${escapeHtml(n.label)}</strong>`];
        if (n.message_count) parts.push(`Co-posts: ${n.message_count}`);
        if (n.channel_count) parts.push(`Across ${n.channel_count} channels`);
        if (n.user_pk) parts.push(`<em>click to open</em>`);
        return parts.join('<br>');
    }

    // ------------------------------------------------------------------
    // Helpers
    // ------------------------------------------------------------------
    function loadingHtml() {
        return `<div class="un-loading text-muted">
            <div class="spinner-border spinner-border-sm me-2"></div>Loading…
        </div>`;
    }
    function formatTs(iso) {
        try { return new Date(iso).toLocaleString(); } catch (_) { return iso; }
    }
    function pad(n) { return String(n).padStart(2, '0'); }
    function truncate(s, n) {
        s = String(s ?? '');
        return s.length <= n ? s : s.slice(0, n - 1) + '…';
    }
    function escapeHtml(s) {
        return String(s ?? '').replace(/[&<>"']/g, c => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
        }[c]));
    }
})();
