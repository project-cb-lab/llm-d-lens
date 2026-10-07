// Minimal, dependency-free Markdown -> safe HTML renderer.
//
// Scope is intentionally small: this only needs to render HuggingFace model
// README files inside the model source picker (see HuggingFaceModelPickerModal.jsx).
// It is NOT a full CommonMark implementation. All literal text is HTML-escaped
// before any markdown syntax is turned into tags, so the only HTML ever
// produced is the small set of tags this renderer emits itself -- untrusted
// README content can never inject arbitrary HTML/script.
//
// Supported: headings, bold/italic, inline code, fenced code blocks, links,
// images (rendered as a small label, README images are frequently relative
// paths that don't resolve outside the repo anyway), blockquotes, horizontal
// rules, unordered/ordered lists, and paragraphs.

function escapeHtml(text) {
    return text
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

// Applies inline-level formatting (code spans, bold, italic, links) to a
// single already-escaped line/segment.
function renderInline(escaped) {
    let text = escaped;
    // Inline code first so its contents are never re-processed by the rules below.
    text = text.replace(/`([^`]+)`/g, '<code>$1</code>');
    text = text.replace(/!\[([^\]]*)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g, '[image: $1]');
    text = text.replace(
        /\[([^\]]+)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g,
        (match, label, href) => {
            const safeHref = /^(https?:)?\/\//.test(href) ? href : '#';
            return `<a href="${safeHref}" target="_blank" rel="noreferrer noopener">${label}</a>`;
        }
    );
    text = text.replace(/\*\*([^*]+)\*\*|__([^_]+)__/g, (match, a, b) => `<strong>${a || b}</strong>`);
    text = text.replace(/\*([^*]+)\*|_([^_]+)_/g, (match, a, b) => `<em>${a || b}</em>`);
    return text;
}

export function renderMarkdown(source) {
    const lines = String(source || '').replace(/\r\n/g, '\n').split('\n');
    const html = [];
    let paragraph = [];
    let listType = null; // 'ul' | 'ol' | null
    let inCodeBlock = false;
    let codeLines = [];

    const flushParagraph = () => {
        if (!paragraph.length) return;
        html.push(`<p>${paragraph.join(' ')}</p>`);
        paragraph = [];
    };
    const flushList = () => {
        if (!listType) return;
        html.push(`</${listType}>`);
        listType = null;
    };

    for (const rawLine of lines) {
        if (inCodeBlock) {
            if (/^```/.test(rawLine.trim())) {
                html.push(`<pre><code>${codeLines.join('\n')}</code></pre>`);
                codeLines = [];
                inCodeBlock = false;
            } else {
                codeLines.push(escapeHtml(rawLine));
            }
            continue;
        }

        const line = rawLine.trimEnd();
        const trimmed = line.trim();

        if (/^```/.test(trimmed)) {
            flushParagraph();
            flushList();
            inCodeBlock = true;
            continue;
        }
        if (!trimmed) {
            flushParagraph();
            flushList();
            continue;
        }
        if (/^(-\s*){3,}$|^(\*\s*){3,}$|^(_\s*){3,}$/.test(trimmed)) {
            flushParagraph();
            flushList();
            html.push('<hr />');
            continue;
        }

        const headingMatch = /^(#{1,6})\s+(.*)$/.exec(trimmed);
        if (headingMatch) {
            flushParagraph();
            flushList();
            const level = headingMatch[1].length;
            html.push(`<h${level}>${renderInline(escapeHtml(headingMatch[2]))}</h${level}>`);
            continue;
        }

        const quoteMatch = /^>\s?(.*)$/.exec(trimmed);
        if (quoteMatch) {
            flushParagraph();
            flushList();
            html.push(`<blockquote>${renderInline(escapeHtml(quoteMatch[1]))}</blockquote>`);
            continue;
        }

        const unorderedMatch = /^[-*+]\s+(.*)$/.exec(trimmed);
        const orderedMatch = /^\d+\.\s+(.*)$/.exec(trimmed);
        if (unorderedMatch || orderedMatch) {
            flushParagraph();
            const nextType = unorderedMatch ? 'ul' : 'ol';
            if (listType && listType !== nextType) flushList();
            if (!listType) {
                listType = nextType;
                html.push(`<${listType}>`);
            }
            const itemText = unorderedMatch ? unorderedMatch[1] : orderedMatch[1];
            html.push(`<li>${renderInline(escapeHtml(itemText))}</li>`);
            continue;
        }

        flushList();
        paragraph.push(renderInline(escapeHtml(trimmed)));
    }

    if (inCodeBlock) html.push(`<pre><code>${codeLines.join('\n')}</code></pre>`);
    flushParagraph();
    flushList();

    return html.join('\n');
}

export default renderMarkdown;
