import { html } from '../htm.js';
import { formatElapsedTime, renderMarkdown } from '../utils.js';
import { Collapsible } from './Collapsible.js';
import { ToolCall } from './ToolCall.js';
import { MetricsBar } from './PlanStep.js';

export function StepContainer({ node }) {
    const data = node.data;

    return html`
        <div class="step-container" data-step=${data.step_number}>
            <div class="step-number ${!data.duration ? 'active' : ''}">
                ${data.step_number}
            </div>
            ${data.duration != null && html`
                <div class="step-elapsed">${formatElapsedTime(Math.round(data.duration))}</div>
            `}
            <div class="step-header">
                <span>Step ${data.step_number}</span>
            </div>
            ${(data.duration != null || data.token_usage) && html`
                <${MetricsBar} data=${data} />
            `}
            <div class="step-children">
                <!-- LLM reasoning -->
                ${data.model_output && html`
                    <div class="model-output" dangerouslySetInnerHTML=${{ __html: renderMarkdown(data.model_output) }} />
                `}

                ${renderToolCalls(data)}

                <!-- Error -->
                ${data.error && html`
                    <div class="event-error">${data.error}</div>
                `}
            </div>
        </div>
    `;
}

function renderToolCalls(data) {
    const toolCalls = data.tool_calls || [];

    if (toolCalls.length === 1) {
        return html`
            <${ToolCall}
                toolName=${toolCalls[0].name}
                args=${toolCalls[0].arguments}
                result=${data.observations}
            />
        `;
    }

    return html`
        ${toolCalls.map((tc, i) => html`
            <${ToolCall}
                toolName=${tc.name}
                args=${tc.arguments}
                key=${i}
            />
        `)}
        ${data.observations && html`
            <${Collapsible}
                title="Results"
                content=${data.observations}
                type="observation"
                isMarkdown=${true}
            />
        `}
    `;
}
