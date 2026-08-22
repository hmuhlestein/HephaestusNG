import React, { useState, useRef, useEffect } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { motion, AnimatePresence } from 'framer-motion';
import { X, Upload, FileText, Check, FolderOpen, Folder, ArrowLeft, Sparkles, Bug, FolderInput } from 'lucide-react';
import { apiService } from '@/services/api';
import { Button } from '@/components/ui/button';
import toast from 'react-hot-toast';

interface LoadDesignModalProps {
  open: boolean;
  projectId: string | null;
  // Fixes the workflow type instead of letting the backend auto-detect it,
  // and switches on the destination-folder picker below (spec-driven
  // development happens outside this UI -- these two flows exist to pull
  // an already-written design/bug doc in and route it to the right
  // pipeline, not to type one from scratch). Omitted for the plain "Load
  // Design" entry point, which keeps today's auto-detect + queue/docs
  // behavior completely unchanged.
  workflowType?: 'feature' | 'bugfix';
  onClose: () => void;
}

interface LoadedFile {
  name: string;
  content: string;
  size: number;
  remotePath?: string;
}

interface RemoteEntry {
  name: string;
  path: string;
  type: 'dir' | 'file';
}

const TYPE_COPY = {
  feature: {
    icon: Sparkles,
    title: 'New Feature',
    subtitle: 'Select or drag & drop design documents describing what to build.',
    accent: 'blue',
    defaultFolder: 'docs/design',
  },
  bugfix: {
    icon: Bug,
    title: 'Report Bug',
    subtitle: "Select or drag & drop bug reports describing what's broken.",
    accent: 'amber',
    defaultFolder: 'docs/bugfix',
  },
} as const;

const ACCENT_CLASSES = {
  violet: {
    iconBg: 'bg-violet-100', iconText: 'text-violet-600',
    ring: 'focus:ring-violet-500', button: 'bg-violet-600 hover:bg-violet-700 text-white',
  },
  blue: {
    iconBg: 'bg-blue-100', iconText: 'text-blue-600',
    ring: 'focus:ring-blue-500', button: 'bg-blue-600 hover:bg-blue-700 text-white',
  },
  amber: {
    iconBg: 'bg-amber-100', iconText: 'text-amber-600',
    ring: 'focus:ring-amber-500', button: 'bg-amber-600 hover:bg-amber-700 text-white',
  },
} as const;

const LoadDesignModal: React.FC<LoadDesignModalProps> = ({ open, projectId, workflowType, onClose }) => {
  const queryClient = useQueryClient();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [loadedFiles, setLoadedFiles] = useState<LoadedFile[]>([]);
  const [isDragOver, setIsDragOver] = useState(false);
  const [remoteOpen, setRemoteOpen] = useState(false);
  const [remotePath, setRemotePath] = useState('');
  const [remoteParent, setRemoteParent] = useState<string | null>(null);
  const [remoteEntries, setRemoteEntries] = useState<RemoteEntry[]>([]);
  const [remoteLoading, setRemoteLoading] = useState(false);
  const [remoteError, setRemoteError] = useState<string | null>(null);
  const [addingRemotePath, setAddingRemotePath] = useState<string | null>(null);
  const [addedRemotePaths, setAddedRemotePaths] = useState<Set<string>>(new Set());
  const [browsingForFolder, setBrowsingForFolder] = useState(false);
  const [destinationFolder, setDestinationFolder] = useState('');
  const remoteRequestId = useRef(0);

  const copy = workflowType ? TYPE_COPY[workflowType] : null;
  const accent = ACCENT_CLASSES[copy?.accent ?? 'violet'];
  const Icon = copy?.icon ?? Upload;

  useEffect(() => {
    if (!open) {
      setLoadedFiles([]);
      setIsDragOver(false);
      setRemoteOpen(false);
      setRemotePath('');
      setRemoteEntries([]);
      setRemoteError(null);
      setAddedRemotePaths(new Set());
      setBrowsingForFolder(false);
    } else {
      setDestinationFolder(copy?.defaultFolder ?? '');
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, workflowType]);

  const loadRemoteDir = async (path: string) => {
    if (!projectId) return;
    const requestId = ++remoteRequestId.current;
    setRemoteLoading(true);
    setRemoteError(null);
    try {
      const result = await apiService.browseAutopilotProjectFiles(projectId, path);
      if (requestId !== remoteRequestId.current) return; // superseded by a newer navigation
      setRemotePath(result.path);
      setRemoteParent(result.parent);
      setRemoteEntries(result.entries);
    } catch (error: any) {
      if (requestId !== remoteRequestId.current) return;
      setRemoteError(error?.response?.data?.detail || 'Failed to load directory');
    } finally {
      if (requestId === remoteRequestId.current) {
        setRemoteLoading(false);
      }
    }
  };

  const openRemoteBrowser = () => {
    setBrowsingForFolder(false);
    setRemoteOpen(true);
    loadRemoteDir('');
  };

  const openFolderBrowser = () => {
    setBrowsingForFolder(true);
    setRemoteOpen(true);
    loadRemoteDir(destinationFolder);
  };

  const handleSelectRemoteFile = async (entry: RemoteEntry) => {
    if (!projectId || addedRemotePaths.has(entry.path)) return;
    setAddingRemotePath(entry.path);
    try {
      const file = await apiService.getAutopilotProjectFileContent(projectId, entry.path);
      setLoadedFiles(prev => [...prev, { name: file.name, content: file.content, size: file.size_bytes, remotePath: entry.path }]);
      setAddedRemotePaths(prev => new Set(prev).add(entry.path));
    } catch (error: any) {
      toast.error(error?.response?.data?.detail || 'Failed to load file');
    } finally {
      setAddingRemotePath(null);
    }
  };

  const useCurrentFolderAsDestination = () => {
    setDestinationFolder(remotePath);
    setRemoteOpen(false);
    setBrowsingForFolder(false);
  };

  const addMutation = useMutation({
    mutationFn: async (files: LoadedFile[]) => {
      if (!projectId) throw new Error('No project selected');

      const results = [];
      for (const file of files) {
        const ext = file.name.endsWith('.txt') ? '.txt' : '.md';
        const name = file.name.replace(/\.[^/.]+$/, '').replace(/[_-]/g, ' ');
        // New Feature/Report Bug: every file (local or remote-picked) goes
        // to the one destination folder the user chose for this flow.
        // Plain "Load Design" (workflowType unset): unchanged -- files
        // picked from the user's own machine are new content being
        // introduced into the project (persisted as real, git-tracked
        // files in docs/), while files picked via "Load from Remote"
        // already live somewhere in the project and keep going to the
        // existing .hephaestus/designs/ staging dir.
        const destination = workflowType
          ? destinationFolder
          : (file.remotePath ? 'queue' : 'docs');
        const result = await apiService.addAutopilotProjectDesign(projectId, name, file.content, ext, destination, workflowType ?? null);
        results.push(result);
      }
      return results;
    },
    onSuccess: (results) => {
      queryClient.invalidateQueries({ queryKey: ['autopilot-project-designs', projectId] });
      queryClient.invalidateQueries({ queryKey: ['projects'] });
      queryClient.invalidateQueries({ queryKey: ['autopilot-status', projectId] });
      toast.success(`${results.length} design(s) added to queue`);
      onClose();
    },
    onError: (error: any) => {
      toast.error(error?.response?.data?.detail || 'Failed to add designs');
    },
  });

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (!files) return;
    
    Array.from(files).forEach(file => {
      const reader = new FileReader();
      reader.onload = (event) => {
        const content = event.target?.result as string;
        setLoadedFiles(prev => [...prev, {
          name: file.name,
          content,
          size: file.size
        }]);
      };
      reader.readAsText(file);
    });
    
    // Reset input
    if (fileInputRef.current) {
      fileInputRef.current.value = '';
    }
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setIsDragOver(false);
    
    const files = e.dataTransfer.files;
    Array.from(files).forEach(file => {
      if (file.type === 'text/plain' || file.name.endsWith('.md') || file.name.endsWith('.txt')) {
        const reader = new FileReader();
        reader.onload = (event) => {
          const content = event.target?.result as string;
          setLoadedFiles(prev => [...prev, {
            name: file.name,
            content,
            size: file.size
          }]);
        };
        reader.readAsText(file);
      }
    });
  };

  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    setIsDragOver(true);
  };

  const handleDragLeave = () => {
    setIsDragOver(false);
  };

  const removeFile = (index: number) => {
    const removed = loadedFiles[index];
    setLoadedFiles(prev => prev.filter((_, i) => i !== index));
    if (removed?.remotePath) {
      setAddedRemotePaths(prev => {
        const next = new Set(prev);
        next.delete(removed.remotePath!);
        return next;
      });
    }
  };

  const formatBytes = (bytes: number): string => {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  };

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/50 backdrop-blur-sm"
          onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
        >
          <motion.div
            initial={{ scale: 0.95, opacity: 0 }}
            animate={{ scale: 1, opacity: 1 }}
            exit={{ scale: 0.95, opacity: 0 }}
            className="bg-white rounded-2xl shadow-2xl w-full max-w-2xl overflow-hidden"
          >
            {/* Header */}
            <div className="px-6 py-4 border-b flex items-center justify-between">
              <div className="flex items-center gap-3">
                <div className={`p-2 rounded-lg ${accent.iconBg}`}>
                  <Icon className={`w-5 h-5 ${accent.iconText}`} />
                </div>
                <div>
                  <h2 className="text-lg font-bold text-gray-800">{copy?.title ?? 'Load Design Files'}</h2>
                  <p className="text-xs text-gray-500">{copy?.subtitle ?? 'Select or drag & drop design documents from anywhere'}</p>
                </div>
              </div>
              <button
                onClick={onClose}
                className="p-2 rounded-lg hover:bg-gray-100 text-gray-500"
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            {/* Content */}
            <div className="p-6 space-y-4">
              {/* Destination Folder (New Feature / Report Bug flows only) */}
              {workflowType && (
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">Destination Folder</label>
                  <div className="flex items-center gap-2">
                    <input
                      type="text"
                      value={destinationFolder}
                      onChange={(e) => setDestinationFolder(e.target.value)}
                      placeholder={copy?.defaultFolder}
                      className={`flex-1 px-4 py-2 border border-gray-200 rounded-xl text-sm font-mono focus:outline-none focus:ring-2 ${accent.ring}`}
                    />
                    <button
                      type="button"
                      onClick={openFolderBrowser}
                      className="flex items-center gap-1.5 px-3 py-2 text-xs font-medium text-gray-600 border border-gray-200 rounded-xl hover:bg-gray-50 flex-shrink-0"
                    >
                      <FolderInput className="w-3.5 h-3.5" />
                      Browse…
                    </button>
                  </div>
                  <p className="text-xs text-gray-400 mt-1">
                    Every file below is stored here, whether uploaded from your machine or picked from the project.
                  </p>
                </div>
              )}

              {/* Drop Zone */}
              <div
                onDrop={handleDrop}
                onDragOver={handleDragOver}
                onDragLeave={handleDragLeave}
                onClick={() => fileInputRef.current?.click()}
                className={`border-2 border-dashed rounded-xl p-8 text-center cursor-pointer transition-all ${
                  isDragOver
                    ? 'border-violet-500 bg-violet-50'
                    : 'border-gray-200 hover:border-violet-300 hover:bg-gray-50'
                }`}
              >
                <FileText className={`w-12 h-12 mx-auto mb-3 ${isDragOver ? 'text-violet-500' : 'text-gray-300'}`} />
                <p className="text-sm font-medium text-gray-600 mb-1">
                  {isDragOver ? 'Drop files here' : 'Click to select files or drag & drop'}
                </p>
                <p className="text-xs text-gray-400">
                  Supports .md and .txt files &middot; stored in{' '}
                  <code className="font-mono">{workflowType ? destinationFolder || copy?.defaultFolder : 'docs/'}</code>
                </p>
              </div>

              <input
                ref={fileInputRef}
                type="file"
                multiple
                accept=".md,.txt,text/plain,text/markdown"
                onChange={handleFileSelect}
                className="hidden"
              />

              <div className="flex justify-start">
                <button
                  type="button"
                  onClick={openRemoteBrowser}
                  className="flex items-center gap-1.5 text-xs font-medium text-violet-600 hover:text-violet-700"
                >
                  <FolderOpen className="w-3.5 h-3.5" />
                  Load from Remote
                </button>
              </div>

              {/* Remote File Browser */}
              {remoteOpen && (
                <div className="border border-gray-200 rounded-xl overflow-hidden">
                  <div className="flex items-center justify-between px-3 py-2 border-b bg-gray-50">
                    <div className="flex items-center gap-2 min-w-0">
                      <button
                        type="button"
                        onClick={() => remoteParent !== null && loadRemoteDir(remoteParent)}
                        disabled={remoteParent === null || remoteLoading}
                        className="p-1 rounded hover:bg-gray-200 text-gray-500 disabled:opacity-30 disabled:cursor-not-allowed"
                      >
                        <ArrowLeft className="w-3.5 h-3.5" />
                      </button>
                      <span className="text-xs text-gray-500 truncate">/{remotePath}</span>
                    </div>
                    {browsingForFolder ? (
                      <Button
                        type="button"
                        onClick={useCurrentFolderAsDestination}
                        className={`h-7 px-2.5 text-xs ${accent.button}`}
                      >
                        Use This Folder
                      </Button>
                    ) : (
                      <button
                        type="button"
                        onClick={() => setRemoteOpen(false)}
                        className="text-xs font-medium text-gray-500 hover:text-gray-700"
                      >
                        Done
                      </button>
                    )}
                  </div>
                  <div className="max-h-64 overflow-y-auto divide-y divide-gray-100">
                    {remoteLoading ? (
                      <div className="p-4 text-center text-xs text-gray-400">Loading…</div>
                    ) : remoteError ? (
                      <div className="p-4 text-center text-xs text-red-500">{remoteError}</div>
                    ) : remoteEntries.length === 0 ? (
                      <div className="p-4 text-center text-xs text-gray-400">
                        {browsingForFolder ? 'No subfolders here' : 'No folders or .md/.txt files here'}
                      </div>
                    ) : (
                      remoteEntries
                        .filter((entry) => !browsingForFolder || entry.type === 'dir')
                        .map((entry) => {
                        const isAdded = entry.type === 'file' && addedRemotePaths.has(entry.path);
                        return (
                          <button
                            key={entry.path}
                            type="button"
                            onClick={() => (entry.type === 'dir' ? loadRemoteDir(entry.path) : handleSelectRemoteFile(entry))}
                            disabled={addingRemotePath === entry.path || isAdded}
                            className="w-full flex items-center gap-2 px-3 py-2 text-left hover:bg-gray-50 disabled:opacity-50"
                          >
                            {entry.type === 'dir' ? (
                              <Folder className="w-4 h-4 text-violet-400 flex-shrink-0" />
                            ) : (
                              <FileText className="w-4 h-4 text-gray-400 flex-shrink-0" />
                            )}
                            <span className="text-sm text-gray-700 truncate flex-1">{entry.name}</span>
                            {addingRemotePath === entry.path ? (
                              <div className="animate-spin rounded-full h-3 w-3 border-b-2 border-violet-500 flex-shrink-0" />
                            ) : isAdded ? (
                              <Check className="w-3.5 h-3.5 text-green-500 flex-shrink-0" />
                            ) : null}
                          </button>
                        );
                      })
                    )}
                  </div>
                </div>
              )}

              {/* Loaded Files List */}
              {loadedFiles.length > 0 && (
                <div className="space-y-2">
                  <p className="text-sm font-medium text-gray-700">
                    {loadedFiles.length} file(s) selected
                  </p>
                  <div className="max-h-64 overflow-y-auto space-y-2">
                    {loadedFiles.map((file, index) => (
                      <div
                        key={index}
                        className="flex items-center gap-3 p-3 bg-gray-50 rounded-lg"
                      >
                        <FileText className="w-4 h-4 text-violet-500 flex-shrink-0" />
                        <div className="flex-1 min-w-0">
                          <p className="text-sm font-medium text-gray-700 truncate">{file.name}</p>
                          <p className="text-xs text-gray-400">{formatBytes(file.size)}</p>
                        </div>
                        <button
                          onClick={() => removeFile(index)}
                          className="p-1 rounded hover:bg-gray-200 text-gray-400 hover:text-red-500"
                        >
                          <X className="w-3 h-3" />
                        </button>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* Actions */}
              <div className="flex items-center justify-end gap-3 pt-2">
                <Button type="button" variant="outline" onClick={onClose}>
                  Cancel
                </Button>
                <Button
                  onClick={() => addMutation.mutate(loadedFiles)}
                  className={accent.button}
                  disabled={addMutation.isPending || loadedFiles.length === 0 || (!!workflowType && !destinationFolder.trim())}
                >
                  {addMutation.isPending ? (
                    <div className="animate-spin rounded-full h-4 w-4 border-b-2 border-white mr-2" />
                  ) : (
                    <Check className="w-4 h-4 mr-1" />
                  )}
                  Add {loadedFiles.length > 0 ? `${loadedFiles.length} ` : ''}to Queue
                </Button>
              </div>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
};

export default LoadDesignModal;
