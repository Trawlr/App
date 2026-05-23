(function () {
    'use strict';

    const KIND_LABELS = {
        text: 'Text',
        media: 'Media',
        entity: 'URL/Entity',
        crossposter: 'Crossposter',
    };

    const STATE = {
        apiUrl: null,
        flagUrl: null,
        csrfToken: null,
        tab: 'clusters',
        // Last response (used when posting flags so we can decorate inline without re-fetching)
        lastRows: [],
    };

    window.initCIB = function ({ apiUrl, flagUrl, csrfToken }) {
        STATE.apiUrl = apiUrl;
        STATE.flagUrl = flagUrl;
        STATE.csrfToken = csrfToken;

        // Wire control inputs
        const inputs = ['cib-days', 'cib-min-channels', 'cib-window', 'cib-kind', 'cib-active-only'];
        inputs.forEach(id => {
            const el = document.getElementById(id);
            if (!el) return;
            const evt = (el.tagName === 'INPUT' && el.type === 'checkbox') ? 'change'
                       : (el.tagName === 'INPUT' ? 'change' : 'change');
            el.addEventListener(evt, refresh);
        });
        document.getElementById('cib-refresh').addEventListener('click', refresh);

        // Tab switching
        document.querySelectorAll('.cib-tabs [data-tab]').forEach(el => {
            el.addEventListener('click', e => {
                e.preventDefault();
                document.querySelectorAll('.cib-tabs .nav-link').forEach(n => n.classList.remove('active'));
                el.classList.add('active');
                STATE.tab = el.dataset.tab;
                // Hide kind selector for crossposters tab (queries text+media internally)
                document.getElementById('cib-kind').disabled = (STATE.tab === 'crossposters');
                refresh();
            });
        });

        refresh();
    };

    function buildQuery() {
        const params = new URLSearchParams({
            tab: STATE.tab,
            kind: document.getElementById('cib-kind').value,
            days: document.getElementById('cib-days').value,
            min_channels: document.getElementById('cib-min-channels').value,
            window_seconds: document.getElementById('cib-window').value,
            active_only: document.getElementById('cib-active-only').checked ? '1' : '0',
        });
        return params.toString();
    }

    async function refresh() {
        showLoading(true);
        try {
            const resp = await fetch(`${STATE.apiUrl}?${buildQuery()}`, {
                headers: { 'X-Requested-With': 'XMLHttpRequest' },
            });
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const data = await resp.json();
            STATE.lastRows = data.rows || [];
            render(data);
        } catch (err) {
            console.error('CIB fetch failed:', err);
            if (window.showToast) window.showToast('Failed to load CIB data', 'error');
        } finally {
            showLoading(false);
        }
    }

    function showLoading(on) {
        document.getElementById('cib-loading').classList.toggle('d-none', !on);
        if (on) {
            document.getElementById('cib-empty').classList.add('d-none');
            document.getElementById('cib-results').innerHTML = '';
            document.getElementById('cib-summary').textContent = '';
        }
    }

    function render(data) {
        const summaryEl = document.getElementById('cib-summary');
        const emptyEl = document.getElementById('cib-empty');
        const resultsEl = document.getElementById('cib-results');

        if (!data.rows || data.rows.length === 0) {
            emptyEl.classList.remove('d-none');
            resultsEl.innerHTML = '';
            summaryEl.textContent = '';
            return;
        }
        emptyEl.classList.add('d-none');

        const truncated = data.summary && data.summary.truncated;
        summaryEl.textContent =
            `${data.rows.length} ${truncated ? 'clusters (capped)' : 'clusters'} · ` +
            `${data.params.days}d window · min ${data.params.min_channels} channels · ` +
            `≤ ${data.params.window_seconds}s span`;

        if (data.tab === 'chains') {
            resultsEl.innerHTML = renderChains(data.rows);
        } else if (data.tab === 'crossposters') {
            resultsEl.innerHTML = renderCrossposters(data.rows);
        } else {
            resultsEl.innerHTML = renderClusters(data.rows);
        }
        wireRowExpanders();
        wireFlagButtons();
    }

    // ------------------------------------------------------------------
    // Tab 1: Burst clusters
    // ------------------------------------------------------------------
    function renderClusters(rows) {
        const head = `
            <table class="table table-sm table-hover mb-0">
            <thead>
                <tr>
                    <th>Kind</th><th>Channels</th><th>Msgs</th><th>Span</th>
                    <th>First seen</th><th>Sample</th><th>Flag</th>
                </tr>
            </thead><tbody>`;
        const body = rows.map((r, i) => {
            const kindLabel = KIND_LABELS[r.kind] || r.kind;
            const sample = sampleText(r);
            const flagBadge = renderFlagBadge(r.flag);
            return `
                <tr class="cib-row" data-row-idx="${i}">
                    <td>${escapeHtml(kindLabel)}</td>
                    <td><strong>${r.n_channels}</strong></td>
                    <td>${r.n_messages}</td>
                    <td>${formatSpan(r.span_seconds)}</td>
                    <td>${formatDate(r.first_seen)}</td>
                    <td class="cib-sample">${escapeHtml(sample)}</td>
                    <td>${flagBadge}${renderFlagButtons(r)}</td>
                </tr>
                <tr class="cib-detail">
                    <td colspan="7">
                        <div class="cib-fingerprint mb-2">fingerprint: ${escapeHtml(r.fingerprint || '')}</div>
                        ${renderChannelTable(r.channels)}
                    </td>
                </tr>`;
        }).join('');
        return head + body + `</tbody></table>`;
    }

    function renderChannelTable(channels) {
        if (!channels || channels.length === 0) return '<em class="text-muted">no detail</em>';
        const rows = channels.map(c => `
            <tr class="cib-message-row">
                <td>${escapeHtml(c.title || '')}</td>
                <td>+${formatSpan(c.delta_seconds)}</td>
                <td>${formatDate(c.first_at)}</td>
                <td>${c.n_messages_in_cluster}× post${c.n_messages_in_cluster === 1 ? '' : 's'}</td>
                <td>
                    <a href="/source/${c.id}/posts/v3/?goto_msg=${c.first_msg_id}"
                       class="btn btn-sm btn-outline-secondary py-0">
                        <i class="bi bi-box-arrow-up-right"></i> open msg ${c.first_msg_id}
                    </a>
                </td>
            </tr>`).join('');
        return `
            <table class="table table-sm mb-0">
                <thead><tr>
                    <th>Channel</th><th>Δ from originator</th><th>First post</th><th>Posts</th><th></th>
                </tr></thead>
                <tbody>${rows}</tbody>
            </table>`;
    }

    // ------------------------------------------------------------------
    // Tab 2: Amplifier chains (Phase B)
    // ------------------------------------------------------------------
    function renderChains(rows) {
        return rows.map((r, i) => `
            <div class="card border-0 cib-card mb-2">
                <div class="card-body p-3">
                    <div class="d-flex justify-content-between align-items-start mb-1">
                        <div>
                            <strong>${KIND_LABELS[r.kind] || r.kind}</strong> ·
                            <span class="text-muted small">${r.n_channels} channels · span ${formatSpan(r.span_seconds)} · first ${formatDate(r.first_seen)}</span>
                        </div>
                        <div>${renderFlagBadge(r.flag)}${renderFlagButtons(r)}</div>
                    </div>
                    <div class="cib-sample text-muted small mb-1">${escapeHtml(sampleText(r))}</div>
                    ${renderChainSwimlane(r)}
                </div>
            </div>`).join('');
    }

    function renderChainSwimlane(cluster) {
        const channels = cluster.channels || [];
        if (channels.length === 0) return '';
        const totalSpan = Math.max(cluster.span_seconds, 1);
        const pins = channels.map((c, i) => {
            const pct = Math.min(100, (c.delta_seconds / totalSpan) * 100);
            const pinClass = i === 0 ? 'cib-chain-pin originator' : 'cib-chain-pin';
            return `
                <div class="${pinClass}" style="left: ${pct}%;"
                     title="${escapeHtml(c.title)} +${formatSpan(c.delta_seconds)}"></div>
                <div class="cib-chain-label" style="left: ${pct}%;">
                    <a href="/source/${c.id}/posts/v3/?goto_msg=${c.first_msg_id}">${escapeHtml(c.title)}</a>
                    <small>+${formatSpan(c.delta_seconds)}</small>
                </div>`;
        }).join('');
        return `<div class="cib-chain"><div class="cib-chain-track">${pins}</div></div>`;
    }

    // ------------------------------------------------------------------
    // Tab 3: Speed crossposters
    // ------------------------------------------------------------------
    function renderCrossposters(rows) {
        const head = `
            <table class="table table-sm table-hover mb-0">
            <thead>
                <tr>
                    <th>User</th><th>Content kind</th><th>Channels</th><th>Posts</th>
                    <th>Span</th><th>Sample</th><th>Flag</th>
                </tr>
            </thead><tbody>`;
        const body = rows.map((r, i) => {
            const sender = r.sender || {};
            const userLabel = sender.username
                ? `@${escapeHtml(sender.username)}`
                : (sender.name || `id ${sender.id}`);
            return `
                <tr class="cib-row" data-row-idx="${i}">
                    <td><strong>${escapeHtml(userLabel)}</strong></td>
                    <td>${KIND_LABELS[r.content_kind] || r.content_kind}</td>
                    <td>${r.n_channels}</td>
                    <td>${r.n_messages}</td>
                    <td>${formatSpan(r.span_seconds)}</td>
                    <td class="cib-sample">${escapeHtml(sampleText(r))}</td>
                    <td>${renderFlagBadge(r.flag)}${renderFlagButtons(r)}</td>
                </tr>
                <tr class="cib-detail">
                    <td colspan="7">${renderChannelTable(r.channels)}</td>
                </tr>`;
        }).join('');
        return head + body + `</tbody></table>`;
    }

    // ------------------------------------------------------------------
    // Flag buttons / badges (Phase D)
    // ------------------------------------------------------------------
    function renderFlagBadge(flag) {
        if (!flag) return '';
        const cls = flag.status === 'confirmed' ? 'cib-badge-confirmed' : 'cib-badge-dismissed';
        const label = flag.status === 'confirmed' ? 'Confirmed CIB' : 'False positive';
        const tip = flag.note ? ` title="${escapeHtml(flag.note)}"` : '';
        return `<span class="badge ${cls} me-1"${tip}>${label}</span>`;
    }

    function renderFlagButtons(row) {
        const fk = row.kind === 'crossposter' ? 'crossposter' : (row.kind || 'text');
        const fp = row.fingerprint || '';
        return `
            <button class="btn btn-sm btn-link p-0 text-success cib-flag-btn"
                    data-flag-kind="${fk}" data-flag-fingerprint="${escapeHtml(fp)}" data-flag-status="confirmed"
                    title="Mark as confirmed CIB">
                <i class="bi bi-flag-fill"></i>
            </button>
            <button class="btn btn-sm btn-link p-0 text-secondary cib-flag-btn"
                    data-flag-kind="${fk}" data-flag-fingerprint="${escapeHtml(fp)}" data-flag-status="dismissed"
                    title="Dismiss as false positive">
                <i class="bi bi-x-circle"></i>
            </button>`;
    }

    function wireFlagButtons() {
        document.querySelectorAll('.cib-flag-btn').forEach(btn => {
            btn.addEventListener('click', async (e) => {
                e.stopPropagation();
                const kind = btn.dataset.flagKind;
                const fingerprint = btn.dataset.flagFingerprint;
                const status = btn.dataset.flagStatus;
                const note = window.prompt(`Note for this ${status} flag (optional):`, '') || '';
                try {
                    const resp = await fetch(STATE.flagUrl, {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                            'X-CSRFToken': STATE.csrfToken,
                        },
                        body: JSON.stringify({ kind, fingerprint, status, note }),
                    });
                    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                    if (window.showToast) window.showToast('Flag saved', 'success');
                    refresh();
                } catch (err) {
                    console.error('flag save failed:', err);
                    if (window.showToast) window.showToast('Failed to save flag', 'error');
                }
            });
        });
    }

    function wireRowExpanders() {
        document.querySelectorAll('.cib-row').forEach(row => {
            row.addEventListener('click', () => row.classList.toggle('expanded'));
        });
    }

    // ------------------------------------------------------------------
    // Helpers
    // ------------------------------------------------------------------
    function sampleText(r) {
        const s = r.sample || {};
        if (s.text) return s.text;
        if (s.entity_text) return `${s.entity_type || 'entity'}: ${s.entity_text}`;
        if (s.file_unique_id) return `${s.media_type || 'file'} ${s.file_unique_id}`;
        return '';
    }

    function formatSpan(sec) {
        if (sec == null) return '';
        if (sec < 60) return `${sec}s`;
        if (sec < 3600) return `${Math.floor(sec / 60)}m ${sec % 60}s`;
        const h = Math.floor(sec / 3600);
        const m = Math.floor((sec % 3600) / 60);
        return `${h}h ${m}m`;
    }

    function formatDate(iso) {
        if (!iso) return '';
        const d = new Date(iso);
        return d.toLocaleString();
    }

    function escapeHtml(s) {
        return String(s ?? '').replace(/[&<>"']/g, c => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
        }[c]));
    }
})();
