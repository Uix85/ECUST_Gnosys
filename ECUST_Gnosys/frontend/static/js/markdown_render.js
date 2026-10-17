/* One renderer for streaming, history, notes, and previews. Dependencies are pinned locally. */
(function () {
    'use strict';
    const md = window.markdownit({
        html: false, linkify: true, breaks: false,
        highlight(code, language) {
            if (language && window.hljs.getLanguage(language)) {
                return window.hljs.highlight(code, {language, ignoreIllegals: true}).value;
            }
            return '';
        }
    });
    md.use(window.texmath, {engine: window.katex, delimiters: ['dollars', 'brackets'],
        katexOptions: {throwOnError: false, strict: 'ignore', trust: false, maxExpand: 1000}});
    window.renderMarkdown = function (text) {
        const html = DOMPurify.sanitize(md.render(String(text == null ? '' : text)), {
            USE_PROFILES: {html: true, mathMl: true}, ADD_ATTR: ['target'],
            FORBID_TAGS: ['style', 'script', 'iframe', 'object', 'embed', 'form'],
            FORBID_ATTR: ['srcdoc']
        });
        const box = document.createElement('div');
        box.innerHTML = html;
        box.querySelectorAll('a').forEach(a => {
            const href = a.getAttribute('href') || '';
            if (/^(https?:|mailto:)/i.test(href)) {
                a.target = '_blank'; a.rel = 'noopener noreferrer';
            } else if (href && !href.startsWith('#') && !href.startsWith('/')) {
                a.removeAttribute('href');
            }
        });
        box.querySelectorAll('li').forEach(li => {
            const node = li.firstChild;
            if (node && node.nodeType === Node.TEXT_NODE && /^\[[ xX]\] /.test(node.textContent)) {
                const checked = /^\[[xX]\]/.test(node.textContent);
                node.textContent = node.textContent.slice(4);
                const checkbox = document.createElement('input');
                checkbox.type = 'checkbox'; checkbox.disabled = true;
                if (checked) checkbox.setAttribute('checked', '');
                checkbox.setAttribute('aria-label', checked ? '已完成' : '未完成');
                li.prepend(checkbox);
            }
        });
        return box.innerHTML;
    };
})();
