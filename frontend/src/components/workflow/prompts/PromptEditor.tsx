import PromptFieldList from './PromptFieldList';

interface PromptEditorProps {
  prompt: {
    description: string;
    done_definitions: string[];
    additional_notes: string | null;
    outputs: string | null;
    next_steps: string | null;
  } | null;
  onChange: (prompt: any) => void;
  disabled?: boolean;
}

export default function PromptEditor({ prompt, onChange, disabled }: PromptEditorProps) {
  if (!prompt) {
    return (
      <div className="text-center py-8 text-gray-500 text-sm">
        No prompt data available.
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {/* Description */}
      <div>
        <label className="text-xs font-semibold text-gray-500 mb-1 block">
          Description (Phase System Prompt Root)
        </label>
        <textarea
          value={prompt.description}
          onChange={(e) => onChange({ ...prompt, description: e.target.value })}
          disabled={disabled}
          rows={4}
          className="w-full text-sm border border-gray-200 rounded-md px-3 py-2 font-mono resize-y focus:outline-none focus:ring-2 focus:ring-blue-200 disabled:opacity-50"
        />
      </div>

      {/* Done Definitions */}
      <div>
        <label className="text-xs font-semibold text-gray-500 mb-1 block">
          Completion Criteria
        </label>
        <PromptFieldList
          items={prompt.done_definitions}
          onChange={(items) => onChange({ ...prompt, done_definitions: items })}
          disabled={disabled}
        />
      </div>

      {/* Additional Notes */}
      <div>
        <label className="text-xs font-semibold text-gray-500 mb-1 block">
          Additional Notes
        </label>
        <textarea
          value={prompt.additional_notes || ''}
          onChange={(e) => onChange({ ...prompt, additional_notes: e.target.value || null })}
          disabled={disabled}
          rows={3}
          className="w-full text-sm border border-gray-200 rounded-md px-3 py-2 resize-y focus:outline-none focus:ring-2 focus:ring-blue-200 disabled:opacity-50"
        />
      </div>

      {/* Expected Outputs */}
      <div>
        <label className="text-xs font-semibold text-gray-500 mb-1 block">
          Expected Outputs
        </label>
        <textarea
          value={prompt.outputs || ''}
          onChange={(e) => onChange({ ...prompt, outputs: e.target.value || null })}
          disabled={disabled}
          rows={2}
          className="w-full text-sm border border-gray-200 rounded-md px-3 py-2 resize-y focus:outline-none focus:ring-2 focus:ring-blue-200 disabled:opacity-50"
        />
      </div>

      {/* Next Steps */}
      <div>
        <label className="text-xs font-semibold text-gray-500 mb-1 block">
          Next Steps
        </label>
        <textarea
          value={prompt.next_steps || ''}
          onChange={(e) => onChange({ ...prompt, next_steps: e.target.value || null })}
          disabled={disabled}
          rows={2}
          className="w-full text-sm border border-gray-200 rounded-md px-3 py-2 resize-y focus:outline-none focus:ring-2 focus:ring-blue-200 disabled:opacity-50"
        />
      </div>
    </div>
  );
}
