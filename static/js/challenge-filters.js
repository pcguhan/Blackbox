document.addEventListener('DOMContentLoaded', () => {
    const search = document.getElementById('challengeSearch');
    const category = document.getElementById('categoryFilter');
    const unsolved = document.getElementById('unsolvedFilter');
    const results = document.getElementById('challengeResults');
    const noMatches = document.getElementById('noChallengeMatches');
    const noChallenges = document.getElementById('noChallengesAvailable');
    const sections = document.querySelectorAll('.category-section');
    const entries = Array.from(document.querySelectorAll('.challenge-entry'), element => ({
        element,
        act: element.closest('.act-content'),
        category: element.dataset.category,
        solved: element.dataset.solved === 'true',
        title: element.dataset.searchTitle.toLowerCase(),
        description: element.dataset.searchDescription.toLowerCase(),
    }));

    [...new Set(entries.map(entry => entry.category))].sort().forEach(value => {
        category.add(new Option(value, value));
    });

    function updateResults() {
        const query = search.value.trim().toLowerCase();
        let visible = 0;
        let total = 0;
        entries.forEach(({ element, act, category: entryCategory, solved, title, description }) => {
            const matches = (!category.value || entryCategory === category.value)
                && (!unsolved.checked || !solved)
                && (title.includes(query) || description.includes(query));
            element.hidden = !matches;
            if (!act || act.classList.contains('active')) {
                total++;
                if (matches) visible++;
            }
        });
        sections.forEach(section => {
            section.hidden = !section.querySelector('.challenge-entry:not([hidden])');
        });
        results.textContent = `${visible} of ${total} challenges`;
        noMatches.hidden = visible > 0 || total === 0;
        noChallenges.hidden = total > 0 || Boolean(document.querySelector('.act-content.active .locked-act-message'));
    }

    search.addEventListener('input', updateResults);
    category.addEventListener('change', updateResults);
    unsolved.addEventListener('change', updateResults);
    document.getElementById('clearFilters').addEventListener('click', () => {
        search.value = '';
        category.value = '';
        unsolved.checked = false;
        updateResults();
        search.focus();
    });
    window.addEventListener('act-changed', updateResults);
    updateResults();
});
