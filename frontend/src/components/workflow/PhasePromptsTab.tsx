import { useState, useEffect, useRef } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Button } from '@/components/ui/button';
import { apiService } from '@/services/api';
import PromptEditor from './prompts/PromptEditor';
import PromptPreview from './prompts/PromptPreview';
import PromptVersionHistory from './prompts/PromptVersionHistory';
import { Save, Eye, History, AlertTriangle } from 'lucide-react';
import type { PhasePromptVersion } from '@/types';

interface PhasePromptsTabProps {
  phaseId: string;
  details: any;
  activeAgents: any[];
}

export default function PhasePromptsTab({
  phaseId,
  details,
  activeAgents,
}: PhasePromptsTabProps) {
  const queryClient = useQueryClient();
  const [activeSubTab, setActiveSubTab] = useState<'edit' | 'preview'>('edit');
  const [draftPrompt, setDraftPrompt] = useState<{
    description: string;
    done_definitions: string[];
    additional_notes: string | null;
    outputs: string | null;
    next_steps: string | null;
  } | null>(null);
  const [changeSummary, setChangeSummary] = useState('');


  const hasActiveAgents = activeAgents.length > 0;

  // Initialise draft from details (only on first load or explicit reset)
  const lastInitRef = useRef<string | null>(null);
  useEffect(() => {
    if (details) {
      const detailsKey = JSON.stringify(details);
      if (!draftPrompt || lastInitRef.current !== detailsKey) {
        lastInitRef.current = detailsKey;
        setDraftPrompt({
          description: details.description || '',
          done_definitions: details.done_definitions || [],
          additional_notes: details.additional_notes ?? null,
          outputs: details.outputs ?? null,
          next_steps: details.next_steps ?? null,
        });
      }
    }
  }, [details]);

  // Fetch versions
  const { data: versionsData } = useQuery({
    queryKey: ['phase-prompt-versions', phaseId],
    queryFn: () => apiService.getPhasePromptVersions(phaseId),
  });

  // Fetch preview with draft data (debounced by React Query staleTime)
  const { data: previewData } = useQuery({
    queryKey: ['phase-prompt-preview', phaseId, draftPrompt],
    queryFn: () => apiService.getPhasePromptPreviewDraft(phaseId, draftPrompt!),
    enabled: !!draftPrompt && activeSubTab === 'preview',
    staleTime: 500,
  });

  // Save mutation
  const saveMutation = useMutation({
    mutationFn: (publish: boolean) =>
      apiService.createPhasePromptVersion(phaseId, {
        description: draftPrompt?.description || '',
        done_definitions: draftPrompt?.done_definitions || [],
        additional_notes: draftPrompt?.additional_notes ?? null,
        outputs: draftPrompt?.outputs ?? null,
        next_steps: draftPrompt?.next_steps ?? null,
        change_summary: changeSummary,
        publish,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['phase-prompt-versions', phaseId] });
      queryClient.invalidateQueries({ queryKey: ['phase-details', phaseId] });
      setChangeSummary('');
    },
  });

  const handleSaveDraft = () => saveMutation.mutate(false);
  const handlePublish = () => saveMutation.mutate(true);

  const handleDiscard = () => {
    if (details) {
      setDraftPrompt({
        description: details.description || '',
        done_definitions: details.done_definitions || [],
        additional_notes: details.additional_notes ?? null,
        outputs: details.outputs ?? null,
        next_steps: details.next_steps ?? null,
      });
      setChangeSummary('');
    }
  };

  const versions: PhasePromptVersion[] = versionsData?.versions || [];
  const isDirty = draftPrompt && details && (
    draftPrompt.description !== (details.description || '') ||
    JSON.stringify(draftPrompt.done_definitions) !== JSON.stringify(details.done_definitions || []) ||
    draftPrompt.additional_notes !== details.additional_notes ||
    draftPrompt.outputs !== details.outputs ||
    draftPrompt.next_steps !== details.next_steps
  );

  return (
    <div className="space-y-4">
      {/* Active agent warning */}
      {hasActiveAgents && (
        <div className="bg-yellow-50 dark:bg-yellow-900/20 border border-yellow-200 dark:border-yellow-800 rounded-lg p-3 flex items-start gap-2">
          <AlertTriangle className="w-4 h-4 text-yellow-600 dark:text-yellow-400 mt-0.5 flex-shrink-0" />
          <div className="text-sm text-yellow-700 dark:text-yellow-300">
            <strong>{activeAgents.length} active agent(s)</strong> will NOT receive these changes.
            Your edits apply to queued and future tasks only.
          </div>
        </div>
      )}

      {/* Sub-tabs */}
      <div className="flex items-center gap-2">
        <Button
          variant={activeSubTab === 'edit' ? 'default' : 'outline'}
          size="sm"
          onClick={() => setActiveSubTab('edit')}
        >
          <Save className="w-3 h-3 mr-1" />
          Edit Fields
        </Button>
        <Button
          variant={activeSubTab === 'preview' ? 'default' : 'outline'}
          size="sm"
          onClick={() => setActiveSubTab('preview')}
        >
          <Eye className="w-3 h-3 mr-1" />
          Prompt Preview
        </Button>
      </div>

      {/* Content */}
      {activeSubTab === 'edit' ? (
        <PromptEditor
          prompt={draftPrompt}
          onChange={setDraftPrompt}
          disabled={false}
        />
      ) : (
        <PromptPreview
          preview={previewData}
          loading={!previewData}
        />
      )}

      {/* Change summary */}
      {isDirty && (
        <div>
          <label className="text-xs text-gray-500 dark:text-gray-400 mb-1 block">Change summary (optional):</label>
          <input
            type="text"
            value={changeSummary}
            onChange={(e) => setChangeSummary(e.target.value)}
            placeholder="Describe what changed..."
            className="w-full text-sm border border-gray-200 dark:border-gray-600 rounded-md px-3 py-1.5 bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100"
          />
        </div>
      )}

      {/* Actions */}
      <div className="flex items-center gap-2 pt-2 border-t border-gray-200 dark:border-gray-700">
        <Button variant="outline" size="sm" onClick={handleDiscard} disabled={!isDirty}>
          Discard
        </Button>
        <Button
          variant="outline"
          size="sm"
          onClick={handleSaveDraft}
          disabled={!isDirty || saveMutation.isPending}
        >
          Save as Draft
        </Button>
        <Button
          size="sm"
          onClick={handlePublish}
          disabled={!isDirty || saveMutation.isPending}
        >
          Save & Publish
        </Button>
        {saveMutation.isError && (
          <span className="text-xs text-red-500 dark:text-red-400 ml-2">Save failed</span>
        )}
      </div>

      {/* Version history */}
      {versions.length > 0 && (
        <div className="pt-2 border-t border-gray-200 dark:border-gray-700">
          <h4 className="text-xs font-semibold text-gray-500 dark:text-gray-400 mb-2 flex items-center gap-1">
            <History className="w-3 h-3" />
            Version History
          </h4>
          <PromptVersionHistory
            phaseId={phaseId}
            versions={versions}
          />
        </div>
      )}
    </div>
  );
}
