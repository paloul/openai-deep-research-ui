import { html } from '../htm.js';
import { useStore } from '../state.js';
import { renderMarkdown, copyToClipboard, downloadTextFile } from '../utils.js';

function filenameBase(question) {
    const slug = (question || 'deep-research-report')
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, '-')
        .replace(/^-+|-+$/g, '')
        .slice(0, 60) || 'deep-research-report';
    const date = new Date().toISOString().slice(0, 10);
    return `${slug}-${date}`;
}

export function AnswerBox() {
    const answer = useStore(s => s.finalAnswer);
    const question = useStore(s => s.question);
    if (!answer) return null;

    function onCopy(e) {
        copyToClipboard(answer, e.currentTarget);
    }

    function onDownloadMarkdown() {
        downloadTextFile(`${filenameBase(question)}.md`, answer, 'text/markdown');
    }

    function onDownloadText() {
        downloadTextFile(`${filenameBase(question)}.txt`, answer, 'text/plain');
    }

    return html`
        <div class="answer-box">
            <div class="answer-box-header">
                <span class="answer-box-label">Final Answer</span>
                <div class="answer-box-actions">
                    <button class="btn-copy" onClick=${onDownloadMarkdown}>Download .md</button>
                    <button class="btn-copy" onClick=${onDownloadText}>Download .txt</button>
                    <button class="btn-copy" onClick=${onCopy}>Copy</button>
                </div>
            </div>
            <div
                class="answer-box-content markdown-body"
                dangerouslySetInnerHTML=${{ __html: renderMarkdown(answer) }}
            />
        </div>
    `;
}
