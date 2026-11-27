document.addEventListener('DOMContentLoaded', () => {
    const table = document.getElementById('scoreboardTable');
    const body = document.getElementById('scoreboardBody');
    const refresh = document.getElementById('refreshScoreboard');
    const view = document.getElementById('standingsView');
    const status = document.getElementById('scoreboardStatus');
    const statusText = document.getElementById('scoreboardStatusText');
    const updated = document.getElementById('scoreboardUpdated');
    const freezeNotice = document.getElementById('freezeNotice');
    const standing = document.getElementById('yourStanding');
    const isAdmin = table.dataset.isAdmin === 'true';
    const openRosters = new Set();
    let socket, currentRequest, frozenAt, rendered = false;

    function setStatus(state, message) {
        status.dataset.state = state;
        statusText.textContent = message;
    }

    function showEmpty(message) {
        body.replaceChildren();
        const cell = body.insertRow().insertCell();
        cell.colSpan = 5;
        cell.className = 'text-center py-5 text-muted';
        cell.textContent = message;
    }

    function renderRoster(cell, entry) {
        const details = document.createElement('details');
        details.className = 'team-roster';
        details.open = openRosters.has(entry.id);

        const summary = document.createElement('summary');
        summary.id = `team-members-${entry.id}`;
        summary.textContent = `Members (${entry.members.length})`;
        details.append(summary);

        const list = document.createElement('ul');
        for (const member of entry.members) {
            const item = document.createElement('li');
            item.textContent = member.name;
            if (member.roll_number) {
                const roll = document.createElement('span');
                roll.className = 'member-roll';
                roll.textContent = member.roll_number;
                item.append(roll);
            }
            list.append(item);
        }
        details.append(list);
        details.addEventListener('toggle', () => {
            if (!details.isConnected) return;
            if (details.open) openRosters.add(entry.id);
            else openRosters.delete(entry.id);
        });
        cell.append(details);
    }

    function render(data) {
        if (!Array.isArray(data.rows)) throw new Error('Invalid leaderboard response');
        rendered = true;
        frozenAt = data.frozen_at;
        freezeNotice.hidden = !frozenAt;
        if (frozenAt) {
            const time = new Date(frozenAt).toLocaleString();
            freezeNotice.textContent = isAdmin
                ? `Public standings frozen at ${time}. Your admin view is live.`
                : `Standings frozen at ${time}.`;
        }
        document.getElementById('leaderboardTitle').textContent = data.teams_enabled
            ? 'Team leaderboard' : 'Individual leaderboard';
        document.getElementById('entrantHeading').textContent = data.teams_enabled ? 'Team' : 'Participant';

        standing.hidden = !data.me;
        if (data.me) {
            document.getElementById('yourEntrant').textContent = data.me.name;
            document.getElementById('yourRank').textContent = `#${data.me.rank}`;
            document.getElementById('yourScore').textContent = data.me.score;
            document.getElementById('yourSolves').textContent = data.me.solves;
            document.getElementById('yourFirstBloods').textContent = data.me.first_bloods ?? 0;
        }

        const activeRoster = document.activeElement?.closest('.team-roster')?.querySelector('summary').id;
        body.replaceChildren();
        if (!data.rows.length) {
            if (!data.visible) showEmpty('The leaderboard is currently hidden.');
            else if (!data.started && !isAdmin) showEmpty('The competition has not started.');
            else if (view.value === 'nearby') showEmpty('You have no ranked entry in this competition mode.');
            else showEmpty('No ranked entries yet.');
        }

        for (const entry of data.rows) {
            const row = body.insertRow();
            if (entry.id === data.me?.id) row.className = 'own-standing';
            const rank = row.insertCell();
            rank.className = 'rank-cell';
            rank.textContent = `#${entry.rank}`;

            const name = row.insertCell();
            const label = document.createElement('div');
            label.className = 'team-name';
            label.textContent = entry.name;
            name.append(label);
            if (entry.affiliation) {
                const affiliation = document.createElement('div');
                affiliation.className = 'team-affiliation';
                affiliation.textContent = entry.affiliation;
                name.append(affiliation);
            }
            if (entry.members?.length) renderRoster(name, entry);

            const score = row.insertCell();
            score.className = `score-cell${entry.score < 0 ? ' text-danger' : ''}`;
            score.textContent = entry.score;
            const solves = row.insertCell();
            solves.className = 'solves-cell';
            solves.textContent = entry.solves;
            const firstBloods = row.insertCell();
            firstBloods.className = 'first-bloods-cell';
            firstBloods.textContent = entry.first_bloods ?? 0;
        }
        if (activeRoster) document.getElementById(activeRoster)?.focus({preventScroll: true});
        updated.textContent = `Updated ${new Date().toLocaleTimeString()}`;
    }

    async function load() {
        currentRequest?.abort();
        const controller = new AbortController();
        currentRequest = controller;
        refresh.disabled = true;
        const timeout = setTimeout(() => controller.abort(), 10000);
        const url = new URL(table.dataset.viewUrl, window.location.origin);
        if (view.value === 'nearby') url.searchParams.set('nearby', '1');

        try {
            const response = await fetchWithCSRF(url, { signal: controller.signal, cache: 'no-store' });
            if (currentRequest !== controller) return;
            if (response.redirected) {
                window.location.assign(response.url);
                return;
            }
            if (!response.ok) throw new Error('Leaderboard unavailable');
            const data = await response.json();
            if (currentRequest !== controller) return;
            render(data);
            if (frozenAt && !isAdmin) setStatus('frozen', 'Public standings frozen');
            else if (socket?.connected) setStatus('live', 'Live updates connected');
            else setStatus('polling', 'Refreshing every 30 seconds');
        } catch (_) {
            if (currentRequest !== controller) return;
            setStatus('error', 'Update failed');
            updated.textContent = 'Update failed. Use Refresh to try again.';
            if (!rendered) showEmpty('Unable to load rankings.');
        } finally {
            clearTimeout(timeout);
            if (currentRequest === controller) refresh.disabled = false;
        }
    }

    refresh.addEventListener('click', load);
    view.addEventListener('change', load);
    load();

    const onConnect = () => {
        socket.emit('join_scoreboard');
        load();
    };
    const onDisconnect = () => {
        if (!frozenAt || isAdmin) setStatus('polling', 'Refreshing every 30 seconds');
    };
    function onVisibility(data) {
        if (!data.visible && !isAdmin) {
            body.replaceChildren();
            window.location.reload();
        } else load();
    }
    const refreshEvents = ['scoreboard_update', 'scoreboard_freeze'];
    const sharedSocket = window.liveSocket;
    if (typeof io === 'function') {
        socket = sharedSocket || io('/live', { transports: ['polling', 'websocket'] });
        socket.on('connect', onConnect);
        socket.on('disconnect', onDisconnect);
        socket.on('connect_error', onDisconnect);
        socket.on('scoreboard_visibility', onVisibility);
        for (const event of refreshEvents) socket.on(event, load);
        if (socket.connected) onConnect();
    }
    const interval = setInterval(() => { if (!document.hidden) load(); }, 30000);
    window.addEventListener('pagehide', () => {
        clearInterval(interval);
        currentRequest?.abort();
        if (!socket) return;
        if (sharedSocket) {
            for (const event of refreshEvents) socket.off(event, load);
            socket.off('connect', onConnect);
            socket.off('disconnect', onDisconnect);
            socket.off('connect_error', onDisconnect);
            socket.off('scoreboard_visibility', onVisibility);
        } else socket.disconnect();
    });
});
