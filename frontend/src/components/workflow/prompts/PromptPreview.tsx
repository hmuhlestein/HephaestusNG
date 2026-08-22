import React from 'react';
import { Badge } from '@/components/ui/badge';
import { Eye, AlertTriangle, Check } from 'lucide-react';
import type { PhasePromptPreview } from '@/types';

interface PromptPreviewProps {
  preview: PhasePromptPreview | null | undefined;
  loading: boolean;
}

export default function PromptPreview({ preview, loading }: PromptPreviewProps) {
  if (loading || !preview) {
    return (
      <div className="flex items-center justify-center py-8">
        <div className="animate-spin rounded-full h-6 w-6 border-b-2 border-blue-600" />
        <span className="ml-2 text-sm text-gray-500 dark:text-gray-400">Generating preview...</span>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {/* Warnings */}
      {preview.warnings.length > 0 && (
        <div className="bg-yellow-50 dark:bg-yellow-900/20 border border-yellow-200 dark:border-yellow-800 rounded-lg p-2">
          {preview.warnings.map((w, i) => (
            <div key={i} className="flex items-start gap-1.5 text-xs text-yellow-700 dark:text-yellow-300">
              <AlertTriangle className="w-3 h-3 mt-0.5 flex-shrink-0" />
              {w}
            </div>
          ))}
        </div>
      )}

      {/* Variables used */}
      {preview.variables_used.length > 0 && (
        <div className="flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
          <Check className="w-3 h-3 text-green-500" />
          Variables: {preview.variables_used.map(v => (
            <Badge key={v} variant="outline" className="text-[10px]">{`{${v}}`}</Badge>
          ))}
        </div>
      )}

      {/* System Prompt */}
      <div>
        <h4 className="text-xs font-semibold text-gray-500 dark:text-gray-400 mb-1 flex items-center gap-1">
          <Eye className="w-3 h-3" />
          System Prompt
        </h4>
        <div className="bg-gray-900 text-green-400 rounded-lg p-3 font-mono text-xs leading-relaxed max-h-[200px] overflow-y-auto whitespace-pre-wrap">
          {highlightVariables(preview.system_prompt)}
        </div>
      </div>

      {/* User Prompt */}
      <div>
        <h4 className="text-xs font-semibold text-gray-500 dark:text-gray-400 mb-1 flex items-center gap-1">
          <Eye className="w-3 h-3" />
          User Prompt Template
        </h4>
        <div className="bg-gray-50 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 text-gray-800 dark:text-gray-200 rounded-lg p-3 font-mono text-xs leading-relaxed max-h-[300px] overflow-y-auto whitespace-pre-wrap">
          {highlightVariables(preview.user_prompt)}
        </div>
      </div>
    </div>
  );
}

/** Highlight {var_name} tokens in text */
function highlightVariables(text: string): React.ReactNode[] {
  const parts = text.split(/(\{\w+\})/g);
  return parts.map((part, i) => {
    if (/^\{\w+\}$/.test(part)) {
      return (
        <span key={i} className="bg-yellow-200 text-yellow-900 px-0.5 rounded font-bold">
          {part}
        </span>
      );
    }
    return <span key={i}>{part}</span>;
  });
}
