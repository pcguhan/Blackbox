(() => {
    const dialog = document.getElementById('versions-dialog');
    const opener = document.querySelector('[data-versions-open]');
    if (!dialog || !opener) return;

    const body = dialog.querySelector('[data-versions-body]');
    const copyButton = dialog.querySelector('[data-versions-copy]');
    const status = dialog.querySelector('[data-versions-status]');
    const sections = [
        ['runtime', 'Runtime'],
        ['host', 'Host and Docker'],
        ['services', 'Service containers'],
        ['challenge_images', 'Challenge images'],
        ['browser_libraries', 'Browser libraries'],
        ['python_packages', 'Python packages'],
    ];
    let plainText = '';
    let activeRequest;

    function element(tag, value) {
        const node = document.createElement(tag);
        if (value !== undefined) node.textContent = value;
        return node;
    }

    function render(data) {
        const lines = [];
        body.replaceChildren();
        for (const [key, title] of sections) {
            const items = data[key] || [];
            if (!items.length) continue;
            const section = element('section');
            const heading = element('h3', title);
            heading.className = 'h5';
            const table = element('table');
            table.className = 'table table-sm';
            const tbody = element('tbody');
            lines.push(title);
            for (const item of items) {
                const versions = Object.entries(item.versions || {})
                    .map(([name, version]) => `${name} ${version}`).join(', ');
                const details = [item.source, versions, item.created].filter(Boolean).join(' · ');
                const row = element('tr');
                const name = element('th', item.name);
                name.scope = 'row';
                row.append(name, element('td', item.image || item.version), element('td', details));
                tbody.append(row);
                lines.push([item.name, item.image || item.version, details].filter(Boolean).join('\t'));
            }
            lines.push('');
            table.append(tbody);
            section.append(heading, table);
            body.append(section);
        }
        plainText = lines.join('\n');
        copyButton.disabled = !plainText;
    }

    opener.addEventListener('click', async () => {
        dialog.showModal();
        body.textContent = 'Loading versions…';
        copyButton.disabled = true;
        status.textContent = '';
        const controller = new AbortController();
        activeRequest = controller;
        try {
            const response = await fetch(opener.dataset.url, {
                headers: { Accept: 'application/json' },
                signal: controller.signal,
            });
            if (!response.ok) throw new Error(`Request failed (${response.status})`);
            const data = await response.json();
            if (!controller.signal.aborted) render(data);
        } catch (error) {
            if (!controller.signal.aborted) body.textContent = `Could not load versions. ${error.message}`;
        }
    });
    dialog.addEventListener('close', () => activeRequest?.abort());
    copyButton.addEventListener('click', async () => {
        try {
            await navigator.clipboard.writeText(plainText);
            status.textContent = 'Copied.';
        } catch {
            status.textContent = 'Could not copy versions.';
        }
    });
})();
