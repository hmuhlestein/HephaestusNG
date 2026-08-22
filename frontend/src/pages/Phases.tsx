import { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Layers, FileCode, ChevronRight, ChevronDown, Edit2 } from 'lucide-react';
import { cn } from '@/lib/utils';
import { useWorkflow } from '@/context/WorkflowContext';
import { apiService } from '@/services/api';
import { MarkdownRenderer } from '@/utils/markdown';
import PhaseDetailPanel from '@/components/workflow/PhaseDetailPanel';

export default function Phases() {
  const { definitions } = useWorkflow();
  const [searchParams] = useSearchParams();
  const [selectedDefinitionId, setSelectedDefinitionId] = useState<string | null>(
    searchParams.get('definition')
  );
  const [expandedPhaseIndex, setExpandedPhaseIndex] = useState<number | null>(null);

  // Deep link from elsewhere (e.g. a workflow card's "N phases" link) --
  // definitions load asynchronously via context, so the query param alone
  // isn't enough at mount if it arrives before `definitions` is populated.
  useEffect(() => {
    const def = searchParams.get('definition');
    if (def) setSelectedDefinitionId(def);
  }, [searchParams]);

  // Fetch phases for selected definition
  const { data: phasesRaw, isLoading: phasesLoading } = useQuery({
    queryKey: ['definition-phases', selectedDefinitionId],
    queryFn: () => apiService.getWorkflowDefinitionPhases(selectedDefinitionId!),
    enabled: !!selectedDefinitionId,
  });
  const phases = Array.isArray(phasesRaw) ? phasesRaw : [];

  const selectedDefinition = definitions.find((d) => d.id === selectedDefinitionId);

  const handleDefinitionClick = (defId: string) => {
    setSelectedDefinitionId(defId === selectedDefinitionId ? null : defId);
    setExpandedPhaseIndex(null);
  };

  const handlePhaseClick = (index: number) => {
    setExpandedPhaseIndex(index === expandedPhaseIndex ? null : index);
  };

  return (
    <div className="container mx-auto p-6 space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-3xl font-bold text-gray-800 dark:text-gray-100 flex items-center gap-2">
          <Layers className="h-8 w-8" />
          Phases
        </h1>
        <p className="text-muted-foreground mt-1">Edit workflow phase configurations</p>
      </div>

      {/* Workflow Definition Cards */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <FileCode className="h-5 w-5 text-purple-600 dark:text-purple-400" />
            Workflow Definitions
          </CardTitle>
          <CardDescription>Select a workflow to view and edit its phases</CardDescription>
        </CardHeader>
        <CardContent>
          {definitions.length === 0 ? (
            <p className="text-sm text-muted-foreground">No workflow definitions loaded</p>
          ) : (
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
              {definitions.map((def) => (
                <div
                  key={def.id}
                  onClick={() => handleDefinitionClick(def.id)}
                  className={cn(
                    "border rounded-lg p-4 transition-all cursor-pointer hover:shadow-md",
                    selectedDefinitionId === def.id
                      ? "bg-purple-100 dark:bg-purple-900/30 border-purple-400 dark:border-purple-600 ring-2 ring-purple-200 dark:ring-purple-800"
                      : "bg-purple-50 dark:bg-purple-900/20 border-purple-200 dark:border-purple-800 hover:bg-purple-100 dark:hover:bg-purple-900/30"
                  )}
                >
                  <div className="font-medium text-gray-800 dark:text-gray-200">{def.name}</div>
                  <div className="text-sm text-gray-600 dark:text-gray-400 line-clamp-2 mt-1">{def.description}</div>
                  <div className="text-xs text-purple-600 dark:text-purple-400 mt-2 flex items-center gap-1">
                    {def.phases_count} phases
                    <ChevronRight className={cn("w-3 h-3 transition-transform", selectedDefinitionId === def.id && "rotate-90")} />
                  </div>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      {/* Selected Definition - Phase List */}
      {selectedDefinitionId && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Layers className="h-5 w-5 text-blue-600 dark:text-blue-400" />
              {selectedDefinition?.name} — Phases
            </CardTitle>
            <CardDescription>
              {phases?.length || 0} phases defined. Click a phase to view and edit its configuration.
            </CardDescription>
          </CardHeader>
          <CardContent>
            {phasesLoading ? (
              <div className="flex items-center justify-center py-8">
                <div className="animate-spin rounded-full h-6 w-6 border-b-2 border-blue-600" />
                <span className="ml-2 text-sm text-muted-foreground">Loading phases...</span>
              </div>
            ) : !phases || phases.length === 0 ? (
              <div className="text-center py-8 text-muted-foreground">
                No phases found for this workflow definition.
              </div>
            ) : (
              <div className="space-y-3">
                {phases.map((phase: any, index: number) => (
                  <div key={phase.id || index}>
                    {/* Phase card header */}
                    <div
                      onClick={() => handlePhaseClick(index)}
                      className={cn(
                        "border rounded-lg p-4 cursor-pointer transition-all",
                        expandedPhaseIndex === index
                          ? "border-blue-300 dark:border-blue-700 bg-blue-50 dark:bg-blue-900/20"
                          : "border-gray-200 dark:border-gray-700 hover:border-gray-300 dark:hover:border-gray-600 bg-white dark:bg-gray-800 hover:bg-gray-50 dark:hover:bg-gray-700"
                      )}
                    >
                      <div className="flex items-center gap-3">
                        {expandedPhaseIndex === index ? (
                          <ChevronDown className="w-5 h-5 text-gray-500 dark:text-gray-400 flex-shrink-0" />
                        ) : (
                          <ChevronRight className="w-5 h-5 text-gray-500 dark:text-gray-400 flex-shrink-0" />
                        )}
                        <div className="flex-1">
                          <div className="flex items-center gap-2">
                            <span className="bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-400 text-xs font-medium px-2 py-0.5 rounded">
                              Phase {index + 1}
                            </span>
                            <span className="font-medium text-gray-800 dark:text-gray-200">{phase.name}</span>
                          </div>
                          {phase.description && (
                            <p className="text-sm text-gray-500 dark:text-gray-400 mt-1 line-clamp-1">
                              {phase.description}
                            </p>
                          )}
                          <div className="flex items-center gap-4 mt-2 text-xs text-gray-400 dark:text-gray-500">
                            {phase.done_definitions && (
                              <span>{phase.done_definitions.length} completion criteria</span>
                            )}
                            {phase.cli_tool && (
                              <span>Tool: {phase.cli_tool}</span>
                            )}
                            {phase.cli_model && (
                              <span>Model: {phase.cli_model}</span>
                            )}
                          </div>
                        </div>
                        <Edit2 className="w-4 h-4 text-gray-400 dark:text-gray-500" />
                      </div>
                    </div>

                    {/* Expanded phase detail panel - only for UUID phase IDs (from DB) */}
                    {expandedPhaseIndex === index && phase.id && typeof phase.id === 'string' && phase.id.includes('-') && (
                      <div className="ml-8 mt-2 mb-4">
                        <PhaseDetailPanel phaseId={phase.id} />
                      </div>
                    )}

                    {/* Expanded phase definition (numeric ID from definition JSON) */}
                    {expandedPhaseIndex === index && (!phase.id || typeof phase.id === 'number' || (typeof phase.id === 'string' && !phase.id.includes('-'))) && (
                      <div className="ml-8 mt-2 mb-4 bg-gray-50 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg p-4">
                        <div className="space-y-4">
                          {/* Description */}
                          <div>
                            <h4 className="text-xs font-semibold text-gray-500 dark:text-gray-400 mb-1">Description</h4>
                            <MarkdownRenderer content={phase.description || ''} className="text-sm prose prose-sm dark:prose-invert prose-violet max-w-none dark:text-gray-300" />
                          </div>

                          {/* Done Definitions */}
                          {phase.done_definitions?.length > 0 && (
                            <div>
                              <h4 className="text-xs font-semibold text-gray-500 dark:text-gray-400 mb-1">Completion Criteria</h4>
                              <ul className="space-y-1">
                                {phase.done_definitions.map((def: string, i: number) => (
                                  <li key={i} className="flex items-start gap-2 text-sm">
                                    <span className="text-green-500 mt-0.5">✓</span>
                                    <span className="text-gray-700 dark:text-gray-300">{def}</span>
                                  </li>
                                ))}
                              </ul>
                            </div>
                          )}

                          {/* Additional Notes */}
                          {phase.additional_notes && (
                            <div>
                              <h4 className="text-xs font-semibold text-gray-500 dark:text-gray-400 mb-1">Additional Notes</h4>
                              <div className="text-sm text-gray-600 dark:text-gray-300 bg-blue-50 dark:bg-blue-900/20 p-2 rounded prose prose-sm prose-violet max-w-none">
                                <MarkdownRenderer content={phase.additional_notes} />
                              </div>
                            </div>
                          )}

                          {/* Config */}
                          <div className="flex gap-4 text-xs text-gray-500 dark:text-gray-400">
                            {phase.cli_tool && (
                              <span>Tool: <code className="bg-gray-100 dark:bg-gray-700 px-1 rounded">{phase.cli_tool}</code></span>
                            )}
                            {phase.cli_model && (
                              <span>Model: <code className="bg-gray-100 dark:bg-gray-700 px-1 rounded">{phase.cli_model}</code></span>
                            )}
                            {phase.working_directory && (
                              <span>Dir: <code className="bg-gray-100 dark:bg-gray-700 px-1 rounded">{phase.working_directory}</code></span>
                            )}
                          </div>
                        </div>
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  );
}
