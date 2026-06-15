import { ScrollArea } from '@/components/ui/scroll-area';

interface PhaseOverviewProps {
  details: any;
  loading: boolean;
  error: string | null;
}

export default function PhaseOverview({ details, loading, error }: PhaseOverviewProps) {
  if (loading) {
    return (
      <div className="flex items-center justify-center py-8">
        <div className="animate-spin rounded-full h-6 w-6 border-b-2 border-blue-600" />
        <span className="ml-2 text-sm text-gray-500">Loading phase details...</span>
      </div>
    );
  }

  if (error) {
    return (
      <div className="bg-red-50 border border-red-200 rounded-lg p-4 text-center">
        <div className="text-red-600 text-sm font-medium mb-2">Failed to load phase details</div>
        <div className="text-red-500 text-xs">{error}</div>
      </div>
    );
  }

  if (!details) {
    return (
      <div className="text-center py-8 text-gray-500 text-sm">
        No details available.
      </div>
    );
  }

  return (
    <ScrollArea className="max-h-[400px]">
      <div className="space-y-5 pr-4">
        {/* Description */}
        <div>
          <h4 className="font-semibold text-sm text-gray-700 mb-1">Description</h4>
          <p className="text-sm text-gray-600 leading-relaxed whitespace-pre-wrap">
            {details.description}
          </p>
        </div>

        {/* Done Definitions */}
        {details.done_definitions?.length > 0 && (
          <div>
            <h4 className="font-semibold text-sm text-gray-700 mb-1">Completion Criteria</h4>
            <ul className="space-y-1">
              {details.done_definitions.map((def: string, index: number) => (
                <li key={index} className="flex items-start gap-2 text-sm">
                  <span className="text-green-500 mt-1 text-xs">✓</span>
                  <span className="text-gray-600">{def}</span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {/* Additional Notes */}
        {details.additional_notes && (
          <div>
            <h4 className="font-semibold text-sm text-gray-700 mb-1">Additional Notes</h4>
            <p className="text-sm text-gray-600 leading-relaxed bg-blue-50 p-3 rounded-md whitespace-pre-wrap">
              {details.additional_notes}
            </p>
          </div>
        )}

        {/* Expected Outputs */}
        {details.outputs && (
          <div>
            <h4 className="font-semibold text-sm text-gray-700 mb-1">Expected Outputs</h4>
            <p className="text-sm text-gray-600 leading-relaxed bg-gray-50 p-3 rounded-md whitespace-pre-wrap">
              {details.outputs}
            </p>
          </div>
        )}

        {/* Next Steps */}
        {details.next_steps && (
          <div>
            <h4 className="font-semibold text-sm text-gray-700 mb-1">Next Steps</h4>
            <p className="text-sm text-gray-600 leading-relaxed bg-purple-50 p-3 rounded-md whitespace-pre-wrap">
              {details.next_steps}
            </p>
          </div>
        )}
      </div>
    </ScrollArea>
  );
}
