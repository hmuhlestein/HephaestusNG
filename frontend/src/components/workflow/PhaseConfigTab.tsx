import { Badge } from '@/components/ui/badge';
import { Settings, Terminal, Cpu, FolderOpen } from 'lucide-react';

interface PhaseConfigTabProps {
  details: any;
  loading: boolean;
}

export default function PhaseConfigTab({ details, loading }: PhaseConfigTabProps) {
  if (loading) {
    return (
      <div className="flex items-center justify-center py-8">
        <div className="animate-spin rounded-full h-6 w-6 border-b-2 border-blue-600" />
      </div>
    );
  }

  if (!details) {
    return (
      <div className="text-center py-8 text-gray-500 dark:text-gray-400 text-sm">No configuration available.</div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-4">
        <div className="bg-gray-50 dark:bg-gray-800 rounded-lg p-3">
          <div className="flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400 mb-1">
            <Terminal className="w-3 h-3" />
            CLI Tool
          </div>
          <div className="text-sm font-medium text-gray-800 dark:text-gray-200">
            {details.cli_tool || <span className="text-gray-400 dark:text-gray-500 italic">default</span>}
          </div>
        </div>
        <div className="bg-gray-50 dark:bg-gray-800 rounded-lg p-3">
          <div className="flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400 mb-1">
            <Cpu className="w-3 h-3" />
            CLI Model
          </div>
          <div className="text-sm font-medium text-gray-800 dark:text-gray-200">
            {details.cli_model || <span className="text-gray-400 dark:text-gray-500 italic">default</span>}
          </div>
        </div>
      </div>
      {details.working_directory && (
        <div className="bg-gray-50 dark:bg-gray-800 rounded-lg p-3">
          <div className="flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400 mb-1">
            <FolderOpen className="w-3 h-3" />
            Working Directory
          </div>
          <div className="text-sm font-mono text-gray-800 dark:text-gray-200 break-all">
            {details.working_directory}
          </div>
        </div>
      )}
      {details.glm_api_token_env && (
        <div className="bg-gray-50 dark:bg-gray-800 rounded-lg p-3">
          <div className="flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400 mb-1">
            <Settings className="w-3 h-3" />
            GLM Token Env
          </div>
          <div className="text-sm font-mono text-gray-800 dark:text-gray-200">
            <Badge variant="outline">{details.glm_api_token_env}</Badge>
          </div>
        </div>
      )}
    </div>
  );
}
