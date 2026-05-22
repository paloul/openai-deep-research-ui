import { html } from '../htm.js';
import { useState, useEffect, useRef } from 'preact/hooks';
import {
    useStore, setState,
    startStream, stopStream, newSession, setRunMode, discoverModels,
    getReasoningEfforts, setSelectedModel, setSelectedReasoningEffort,
    addAttachments, removeAttachment,
} from '../state.js';
import { StatusBar } from './StatusBar.js';

const MODE_LABELS = {
    'background': 'BG',
    'auto-kill': 'Auto-kill',
    'live': 'Live',
};

const MODE_OPTIONS = [
    { value: 'background', label: 'Background (persistent)', desc: 'Survives browser close' },
    { value: 'auto-kill', label: 'Background (auto-kill)', desc: 'Dies when browser closes' },
    { value: 'live', label: 'Live (leave = stop)', desc: 'Leaving cancels the response' },
];

function formatFileSize(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function RunSplitButton({ runMode, disabled }) {
    const [open, setOpen] = useState(false);
    const ref = useRef(null);

    // Close dropdown on outside click
    useEffect(() => {
        if (!open) return;
        function onClick(e) {
            if (ref.current && !ref.current.contains(e.target)) setOpen(false);
        }
        document.addEventListener('mousedown', onClick);
        return () => document.removeEventListener('mousedown', onClick);
    }, [open]);

    function selectMode(mode) {
        setRunMode(mode);
        setOpen(false);
    }

    return html`
        <div class="split-btn" ref=${ref}>
            <button
                type="submit"
                class="btn btn-submit split-btn-main"
                disabled=${disabled}
            >
                Run <span class="split-btn-mode">${MODE_LABELS[runMode]}</span> <kbd>Ctrl+Enter</kbd>
            </button>
            <button
                type="button"
                class="btn btn-submit split-btn-chevron"
                onClick=${(e) => { e.preventDefault(); setOpen(!open); }}
                aria-label="Select run mode"
            >\u25BE</button>
            ${open && html`
                <div class="split-btn-menu">
                    ${MODE_OPTIONS.map(opt => html`
                        <button
                            type="button"
                            class="split-btn-option ${opt.value === runMode ? 'split-btn-option-active' : ''}"
                            onClick=${() => selectMode(opt.value)}
                            key=${opt.value}
                        >
                            <span class="split-btn-option-label">${opt.label}</span>
                            <span class="split-btn-option-desc">${opt.desc}</span>
                        </button>
                    `)}
                </div>
            `}
        </div>
    `;
}

export function InputPanel() {
    const store = useStore();
    const reasoningEfforts = getReasoningEfforts(store.selectedModel);
    const fileInputRef = useRef(null);

    function onSubmit(e) {
        e.preventDefault();
        startStream();
    }

    // isRunning only locks UI in live mode. In background/auto-kill, user can always start new.
    const uiLocked = store.isRunning && store.runMode === 'live';
    const attachmentsLocked = !!store.sessionId || store.viewingLiveSession || store.viewingHistory;

    return html`
        <div class="panel input-panel">
            <h2>Input</h2>
            ${store.viewingHistory && html`
                <div class="history-badge">
                    Viewing saved session
                    <button class="btn btn-ghost btn-sm" onClick=${newSession}>New Session</button>
                </div>
            `}
            <form onSubmit=${onSubmit}>
                <div class="form-group">
                    <div class="model-select-label-row">
                        <label for="modelSelect">Model</label>
                        <button
                            type="button"
                            class="btn btn-ghost btn-sm model-discover-btn"
                            onClick=${discoverModels}
                            disabled=${store.discoveringModels}
                            title="Query provider APIs to list available models"
                        >
                            ${store.discoveringModels ? 'Refreshing...' : 'Refresh Models'}
                        </button>
                    </div>
                    <select
                        id="modelSelect"
                        value=${store.selectedModel}
                        onChange=${(e) => setSelectedModel(e.target.value)}
                    >
                        ${store.discoveredModels.length > 0 && html`
                            <optgroup label="Discovered">
                                ${store.discoveredModels.map(m => html`
                                    <option value=${m.id}>${m.id}</option>
                                `)}
                            </optgroup>
                        `}
                        ${store.models.length > 0 && html`
                            <optgroup label="Configured">
                                ${store.models.map(m => html`
                                    <option value=${m.id} title=${m.description || ''}>${m.name}</option>
                                `)}
                            </optgroup>
                        `}
                    </select>
                    ${store.discoverErrors.length > 0 && html`
                        <p class="model-discover-errors">
                            ${store.discoverErrors.map(e => html`<span>${e}</span>`)}
                        </p>
                    `}
                </div>

                <div class="form-group">
                    <label for="reasoningEffortSelect">Reasoning Effort</label>
                    <select
                        id="reasoningEffortSelect"
                        value=${store.selectedReasoningEffort}
                        onChange=${(e) => setSelectedReasoningEffort(e.target.value)}
                    >
                        ${reasoningEfforts.map(effort => html`
                            <option value=${effort} key=${effort}>${effort[0].toUpperCase()}${effort.slice(1)}</option>
                        `)}
                    </select>
                </div>

                <div class="form-group">
                    <label for="question">Question</label>
                    <textarea
                        id="question"
                        placeholder="Enter your research question..."
                        value=${store.question}
                        onInput=${(e) => setState({ question: e.target.value })}
                        required
                    />
                </div>

                <div class="form-group attachments-group">
                    <label for="attachments">Attachments</label>
                    <div class="attachment-dropzone">
                        <input
                            ref=${fileInputRef}
                            id="attachments"
                            class="attachment-input"
                            type="file"
                            multiple
                            accept=".txt,.md,.csv,.json,.pdf,.doc,.docx,text/plain,text/markdown,text/csv,application/json,application/pdf,application/msword,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                            onChange=${(e) => {
                                addAttachments(e.target.files);
                                e.target.value = '';
                            }}
                        />
                        <button
                            type="button"
                            class="btn btn-ghost attachment-add-btn"
                            onClick=${() => fileInputRef.current?.click()}
                            disabled=${uiLocked}
                        >
                            Add files
                        </button>
                        <span class="attachment-hint">Text, PDF, .doc, .docx · max 5 files · 25 MB each</span>
                    </div>
                    ${store.attachmentError && html`
                        <div class="attachment-error">${store.attachmentError}</div>
                    `}
                    ${store.attachments.length > 0 && html`
                        <div class="attachment-list">
                            ${store.attachments.map((file, index) => html`
                                <div class="attachment-item" key=${`${file.name}-${file.size}-${file.lastModified}`}>
                                    <div class="attachment-meta">
                                        <span class="attachment-name">${file.name}</span>
                                        <span class="attachment-size">${formatFileSize(file.size)}</span>
                                    </div>
                                    <button
                                        type="button"
                                        class="attachment-remove"
                                        disabled=${attachmentsLocked}
                                        onClick=${() => removeAttachment(index)}
                                        aria-label=${`Remove ${file.name}`}
                                    >\u2715</button>
                                </div>
                            `)}
                        </div>
                    `}
                </div>

                <div class="button-group">
                    <${RunSplitButton} runMode=${store.runMode} disabled=${uiLocked} />
                    ${(store.isRunning || store.viewingLiveSession) && html`
                        <button
                            type="button"
                            class="btn btn-stop"
                            onClick=${stopStream}
                        >
                            Stop <kbd>Esc</kbd>
                        </button>
                    `}
                </div>
            </form>
            <${StatusBar} />
        </div>
    `;
}
