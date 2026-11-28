document.addEventListener('DOMContentLoaded', () => {
    const badge = document.getElementById('notifBadge');
    const list = document.getElementById('notifList');
    const toggle = document.getElementById('notificationsDropdown');
    const status = document.getElementById('notificationStatus');
    if (!badge || !list || !toggle) return;
    const deletedWhileLoading = new Set();
    let loading = true;
    const updateBadge = () => {
        const unread = list.querySelectorAll('[data-read="0"]').length;
        badge.textContent = unread;
        badge.hidden = unread === 0;
        if (status) status.textContent = unread ? `${unread} unread` : 'All caught up';
        const count = document.getElementById('announcementCount');
        if (count) count.textContent = unread ? `(${unread} unread)` : '';
        const latest = document.getElementById('latestAnnouncement');
        if (latest) latest.textContent = list.querySelector('strong')?.textContent || 'No announcements yet';
        toggle.setAttribute('aria-label', `Notifications, ${unread} unread`);
    };
    const placeholder = message => {
        if (list.querySelector('[data-notif-id]')) return;
        list.replaceChildren();
        const item = document.createElement('div');
        item.className = 'list-group-item text-muted'; item.textContent = message;
        list.append(item);
    };
    function addItem(data, live = false) {
        if (Array.from(list.children).some(item => item.dataset.notifId === String(data.id))) return;
        if (!list.querySelector('[data-notif-id]')) list.replaceChildren();
        const item = document.createElement('div');
        item.className = 'list-group-item';
        item.dataset.notifId = data.id;
        item.dataset.read = data.read ? '1' : '0';
        const header = document.createElement('div'); header.className = 'notification-title';
        const title = document.createElement('strong'); title.textContent = data.title || 'Notification';
        const time = document.createElement('small'); time.className = 'text-muted';
        time.textContent = data.created_at ? new Date(data.created_at).toLocaleString() : 'Just now';
        const body = document.createElement('p'); body.className = 'mb-0'; body.textContent = data.body || '';
        header.append(title, time); item.append(header, body);
        if (live) list.prepend(item);
        else list.append(item);
    }
    async function markRead() {
        for (const item of list.querySelectorAll('[data-read="0"]')) {
            if (item.dataset.pending) continue;
            item.dataset.pending = '1';
            try {
                const response = await fetchWithCSRF(`/api/notifications/${encodeURIComponent(item.dataset.notifId)}/read`, { method: 'POST' });
                const data = await response.json();
                if (response.ok && data.success && item.isConnected) {
                    item.dataset.read = '1';
                    updateBadge();
                }
            } catch (_) { /* Retry on next open. */ }
            delete item.dataset.pending;
        }
    }
    function showToast(data) {
        const item = document.createElement('div'); item.className = 'toast'; item.setAttribute('role', 'status');
        const body = document.createElement('div'); body.className = 'toast-body';
        const title = document.createElement('strong'); title.textContent = data.title || 'Notification';
        const text = document.createElement('p'); text.className = 'mb-0 mt-2'; text.textContent = data.body || '';
        const close = document.createElement('button'); close.type = 'button'; close.className = 'btn-close ms-auto'; close.setAttribute('aria-label', 'Dismiss notification'); close.dataset.bsDismiss = 'toast';
        body.append(title, text, close); item.append(body); document.getElementById('toastContainer').append(item);
        if (window.bootstrap) new bootstrap.Toast(item, { delay: 10000 }).show();
        item.addEventListener('hidden.bs.toast', () => item.remove());
    }
    placeholder('Loading notifications…');
    fetchWithCSRF('/api/notifications').then(response => {
        if (!response.ok) throw new Error('Notifications unavailable');
        return response.json();
    }).then(data => {
        if (!data.success || !Array.isArray(data.notifications)) throw new Error('Invalid notifications');
        for (const notification of data.notifications) {
            if (!deletedWhileLoading.has(String(notification.id))) addItem(notification);
        }
        placeholder('No notifications yet.'); updateBadge();
        if (toggle.getAttribute('aria-expanded') === 'true') markRead();
    }).catch(() => placeholder('Notifications could not be loaded. Refresh to retry.')).finally(() => {
        loading = false;
        deletedWhileLoading.clear();
    });
    toggle.addEventListener('shown.bs.dropdown', markRead);
    // REST notifications remain available when the live service is disconnected.
    if (typeof io !== 'function') return;
    const socket = window.liveSocket = io('/live');
    socket.on('notification', data => {
        const exists = Array.from(list.children).some(item => item.dataset.notifId === String(data.id));
        if (exists) return;
        addItem(data, true); updateBadge(); showToast(data);
        if (data.play_sound) document.getElementById('notificationSound')?.play().catch(() => {});
        if (toggle.getAttribute('aria-expanded') === 'true') markRead();
    });
    socket.on('notification_deleted', data => {
        if (loading) deletedWhileLoading.add(String(data.id));
        const item = Array.from(list.children).find(item => item.dataset.notifId === String(data.id));
        if (!item) return;
        item.remove(); updateBadge(); placeholder('No notifications yet.');
    });
});
