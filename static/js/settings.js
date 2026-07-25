/* The settings screen: edit the server list, its order, and the cache TTL.
 *
 * List order is the failover priority, so reordering is a first-class action
 * rather than a nicety. The whole list is saved in one PUT. */

(function () {
    'use strict';

    const list = document.getElementById('server-list');
    const banner = document.getElementById('banner');
    const ttlInput = document.getElementById('ttl');

    // The working copy. Edits stay local until Save, so a half-typed address is
    // never used for failover.
    let servers = [];
    let verdicts = {};

    function setBanner(kind, message, detail) {
        if (!message) { banner.hidden = true; return; }
        banner.className = 'banner ' + kind;
        banner.textContent = message;
        if (detail) {
            const span = document.createElement('span');
            span.className = 'detail';
            span.textContent = detail;
            banner.appendChild(span);
        }
        banner.hidden = false;
    }

    function iconButton(label, path, onClick, disabled) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'icon-btn';
        button.setAttribute('aria-label', label);
        button.title = label;
        button.disabled = Boolean(disabled);
        button.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
                           'aria-hidden="true">' + path + '</svg>';
        button.addEventListener('click', onClick);
        return button;
    }

    const ARROW_UP = '<path d="M12 19V6M6 12l6-6 6 6" stroke-linecap="round" stroke-linejoin="round"/>';
    const ARROW_DOWN = '<path d="M12 5v13M18 12l-6 6-6-6" stroke-linecap="round" stroke-linejoin="round"/>';
    const BIN = '<path d="M4 7h16M9 7V5h6v2M6 7l1 13h10l1-13M10 11v6M14 11v6" ' +
                'stroke-linecap="round" stroke-linejoin="round"/>';

    function move(index, delta) {
        const target = index + delta;
        if (target < 0 || target >= servers.length) { return; }
        const moved = servers.splice(index, 1)[0];
        servers.splice(target, 0, moved);
        render();
    }

    function remove(index) {
        servers.splice(index, 1);
        render();
    }

    function verdictFor(server) {
        // Match on url, because a freshly added server has no id on the server
        // side yet and its url is what was actually probed.
        return verdicts[server.url] || null;
    }

    function renderCard(server, index) {
        const card = document.createElement('div');
        card.className = 'server-card';

        const row = document.createElement('div');
        row.className = 'row';

        const order = document.createElement('span');
        order.className = 'order';
        order.textContent = (index + 1) + '.';
        row.appendChild(order);

        const name = document.createElement('input');
        name.type = 'text';
        name.className = 'field name';
        name.placeholder = 'Name (optional)';
        name.value = server.name || '';
        name.setAttribute('aria-label', 'Server name');
        name.addEventListener('input', function () { server.name = name.value; });
        row.appendChild(name);

        row.appendChild(iconButton('Move up', ARROW_UP, function () { move(index, -1); }, index === 0));
        row.appendChild(iconButton('Move down', ARROW_DOWN, function () { move(index, 1); },
                                   index === servers.length - 1));
        row.appendChild(iconButton('Remove', BIN, function () { remove(index); }));
        card.appendChild(row);

        const url = document.createElement('input');
        url.type = 'url';
        url.className = 'field';
        url.placeholder = '192.168.1.10 or http://host:9149';
        url.value = server.url || '';
        url.autocapitalize = 'none';
        url.spellcheck = false;
        url.setAttribute('inputmode', 'url');
        url.setAttribute('aria-label', 'Server address');
        url.addEventListener('input', function () { server.url = url.value; });
        card.appendChild(url);

        const verdict = verdictFor(server);
        if (verdict) {
            card.dataset.verdict = verdict.ready ? 'ready' : (verdict.status === 'loading' ? 'loading' : 'down');
            const line = document.createElement('div');
            line.className = 'verdict';
            line.innerHTML = '<span class="dot"></span>';

            const text = document.createElement('span');
            if (verdict.ready) {
                text.textContent = verdict.backend
                    ? verdict.backend + ' · v' + (verdict.version || '?')
                    : 'ready';
            } else {
                text.textContent = verdict.error || 'unavailable';
            }
            line.appendChild(text);

            if (verdict.active) {
                const badge = document.createElement('span');
                badge.className = 'badge';
                badge.textContent = 'in use';
                line.appendChild(badge);
            }
            card.appendChild(line);
        }

        return card;
    }

    function render() {
        list.innerHTML = '';
        if (!servers.length) {
            const empty = document.createElement('div');
            empty.className = 'empty';
            empty.textContent = 'No servers yet. Add the address of a machine running PyWhispr.';
            list.appendChild(empty);
            return;
        }
        servers.forEach(function (server, index) { list.appendChild(renderCard(server, index)); });
    }

    async function request(url, options) {
        const response = await fetch(url, options);
        if (response.status === 401) {
            window.location = '/login?next=/settings';
            throw new Error('not authenticated');
        }
        let data = {};
        try { data = await response.json(); } catch (e) { /* handled by caller */ }
        if (!response.ok) {
            throw new Error((data.error && data.error.message) || ('HTTP ' + response.status));
        }
        return data;
    }

    async function load() {
        try {
            const data = await request('/api/servers');
            servers = data.servers.map(function (s) { return {id: s.id, name: s.name, url: s.url}; });
            ttlInput.value = data.cache_ttl_seconds;
            render();
        } catch (err) {
            setBanner('error', 'Could not load settings.', String(err.message || err));
        }
    }

    async function save() {
        const button = document.getElementById('save');
        button.disabled = true;
        try {
            const data = await request('/api/servers', {
                method: 'PUT',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    servers: servers.filter(function (s) { return (s.url || '').trim(); }),
                    cache_ttl_seconds: parseInt(ttlInput.value, 10),
                }),
            });
            // Re-read from the response so the user sees the normalised
            // addresses (a bare hostname gains a scheme and port).
            servers = data.servers.map(function (s) { return {id: s.id, name: s.name, url: s.url}; });
            ttlInput.value = data.cache_ttl_seconds;
            verdicts = {};
            render();
            setBanner('ok', 'Saved.');
            test();
        } catch (err) {
            setBanner('error', 'Could not save.', String(err.message || err));
        } finally {
            button.disabled = false;
        }
    }

    async function test() {
        const button = document.getElementById('test');
        button.disabled = true;
        try {
            const data = await request('/api/servers/status');
            verdicts = {};
            data.servers.forEach(function (v) { verdicts[v.url] = v; });
            render();
            const ready = data.servers.filter(function (v) { return v.ready; }).length;
            if (!data.servers.length) {
                setBanner('warn', 'No servers configured yet.');
            } else if (ready) {
                setBanner('ok', ready + ' of ' + data.servers.length + ' server(s) ready.');
            } else {
                setBanner('error', 'No server is ready.', 'See each server below for why.');
            }
        } catch (err) {
            setBanner('error', 'Could not test servers.', String(err.message || err));
        } finally {
            button.disabled = false;
        }
    }

    document.getElementById('add').addEventListener('click', function () {
        servers.push({name: '', url: ''});
        render();
        // Focus the address of the row just added, so typing can start at once.
        const fields = list.querySelectorAll('input[type="url"]');
        if (fields.length) { fields[fields.length - 1].focus(); }
    });
    document.getElementById('save').addEventListener('click', save);
    document.getElementById('test').addEventListener('click', test);

    load().then(function () {
        if (servers.length) { test(); }
    });
})();
