import React, { useState, useRef, useEffect } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { motion, AnimatePresence } from 'framer-motion';
import { X, Upload, FileText, Check, FolderOpen, Folder, ArrowLeft, Sparkles, Bug, FolderInput } from 'lucide-react';
import { apiService } from '@/services/api';
import { Button } from '@/components/ui/button';
import SpecKitFeaturePicker from './SpecKitFeaturePicker';
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
    title: 'Design Spec',
    subtitle: 'Select or drag & drop design documents describing what to build.',
    accent: 'blue',
    defaultFolder: 'docs/spec',
  },
  bugfix: {
    icon: Bug,
    title: 'Bug Spec',
    subtitle: "Select or drag & drop bug reports describing what's broken.",
    accent: 'amber',
    defaultFolder: 'docs/bugfix',
  },
} as const;

const ACCENT_CLASSES = {
  violet: {
    iconBg: 'bg-violet-100 dark:bg-violet-800/50', iconText: 'text-violet-600 dark:text-violet-400',
    ring: 'focus:ring-violet-500', button: 'bg-violet-600 hover:bg-violet-700 text-white',
  },
  blue: {
    iconBg: 'bg-blue-100 dark:bg-blue-800/50', iconText: 'text-blue-600 dark:text-blue-400',
    ring: 'focus:ring-blue-500', button: 'bg-blue-600 hover:bg-blue-700 text-white',
  },
  amber: {
    iconBg: 'bg-amber-100 dark:bg-amber-800/50', iconText: 'text-amber-600 dark:text-amber-400',
    ring: 'focus:ring-amber-500', button: 'bg-amber-600 hover:bg-amber-700 text-white',
  },
} as const;

// Spec Kit feature folders (top-level specs/001-my-feature) hold spec.md,
// plan.md, tasks.md, etc.
const SPEC_FOLDER_PATH_RE = /^specs\/\d+-/;

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
  // Bug Spec flow only: typed/pasted content saved directly to a file in
  // the destination folder, instead of the feature flow's drag & drop --
  // a bug report is usually written fresh on the spot, not dragged in
  // from an existing file. Local-file selection still exists for this
  // flow (see the "Select Local File" button in Actions below), it's
  // just no longer the primary composition surface.
  const [textContent, setTextContent] = useState('');
  const [textFilename, setTextFilename] = useState('bug-report.md');
  // Set only immediately after a remote-select fills the filename/textarea
  // pair, and cleared the instant either one is edited -- see the two
  // onChange handlers below. Lets addMutation tell the backend "this is
  // still exactly the file the user picked, unedited," so re-submitting
  // an existing design back to itself can be treated as a no-op instead
  // of a name collision -- without also swallowing a genuine collision
  // from a freshly typed/uploaded name.
  const [textSourceRemotePath, setTextSourceRemotePath] = useState<string | null>(null);
  // Design Spec flow only: read-only preview of the most recently clicked
  // spec folder's spec.md -- Design Spec keeps its multi-file card list
  // (unlike Bug Spec's single textarea), so this is purely a "see what
  // you're about to add" preview, not the thing actually submitted.
  const [specPreview, setSpecPreview] = useState<{ path: string; content: string } | null>(null);
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
      setTextContent('');
      setTextFilename('bug-report.md');
      setTextSourceRemotePath(null);
      setSpecPreview(null);
    } else {
      const defaultFolder = copy?.defaultFolder ?? '';
      setDestinationFolder(defaultFolder);
      if (defaultFolder && projectId) {
        apiService.ensureAutopilotProjectFolder(projectId, defaultFolder).catch(() => {});
      }
      // Expanded by default -- these flows exist specifically to pull in
      // an already-written doc, so the remote browser (not the local
      // drop zone) is the primary path, not a secondary one behind a
      // click. Kept expanded for Bug Spec too (its own list is shrunk
      // instead -- see the max-h-32 remote browser below -- rather than
      // hiding the option entirely).
      setBrowsingForFolder(false);
      setRemoteOpen(true);
      loadRemoteDir('');
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, workflowType]);

  // Close on Escape, same as the existing backdrop-click-to-close --
  // applies to both the Design Spec and Bug Spec flows (and the plain
  // "Load Design" entry point), since this one component renders all of
  // them via workflowType.
  useEffect(() => {
    if (!open) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [open, onClose]);

  // Destination Folder picker: keep the textbox above in sync with
  // whatever folder is currently being browsed, not just on "Create" --
  // otherwise navigating into (or back out of) a folder silently left the
  // textbox showing a stale path until the user explicitly confirmed.
  useEffect(() => {
    if (browsingForFolder) setDestinationFolder(remotePath);
  }, [remotePath, browsingForFolder]);

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

  // Clicking a spec folder itself pulls spec.md, the top-level document,
  // straight into the viewer instead of just navigating in. A folder mid-
  // workflow (e.g. speckit-plan already ran and spec.md was consumed/
  // replaced by plan.md, research.md, etc.) has no spec.md yet -- fall
  // back to just navigating in instead of erroring, so the user can still
  // reach whatever IS there.
  const isSpecFolder = (entry: RemoteEntry) =>
    entry.type === 'dir' && SPEC_FOLDER_PATH_RE.test(entry.path);

  const handleSelectSpecFolder = async (entry: RemoteEntry) => {
    if (!projectId) return;
    const specEntry: RemoteEntry = { name: 'spec.md', path: `${entry.path}/spec.md`, type: 'file' };
    setAddingRemotePath(specEntry.path);
    try {
      const file = await apiService.getAutopilotProjectFileContent(projectId, specEntry.path);
      _applyLoadedFile(specEntry.path, file, entry.name);
      if (workflowType === 'feature') setSpecPreview({ path: specEntry.path, content: file.content });
    } catch {
      loadRemoteDir(entry.path);
    } finally {
      setAddingRemotePath(null);
    }
  };

  // Applies a fetched remote file to the loader's state -- shared by the
  // plain file-click path and the spec-folder fallback above. displayName
  // overrides the fetched file's own name for display/labeling purposes
  // (e.g. a Spec Kit folder's name instead of "spec.md") -- path still
  // drives where it's tracked as loaded from.
  const _applyLoadedFile = (path: string, file: { name: string; content: string; size_bytes: number }, displayName?: string) => {
    const name = displayName ?? file.name;
    // Selecting a file implies where it already lives -- update the
    // Destination Folder textbox above to match, instead of leaving it
    // at whatever it was (typically the workflow's default), so the
    // file doesn't silently get re-saved somewhere else on submit.
    if (workflowType) {
      const lastSlash = path.lastIndexOf('/');
      setDestinationFolder(lastSlash === -1 ? '' : path.slice(0, lastSlash));
    }
    // Bug Spec: same single filename/textarea preview "Select Local
    // File" already fills (see handleFileSelect) -- replace its content
    // instead of appending to loadedFiles, which rendered as an inert
    // "1 file(s) selected" row with no visible content, inconsistent
    // with the local-file path.
    if (workflowType === 'bugfix') {
      setTextFilename(name);
      setTextContent(file.content);
      setTextSourceRemotePath(path);
    } else {
      setLoadedFiles(prev => [...prev, { name, content: file.content, size: file.size_bytes, remotePath: path }]);
      setAddedRemotePaths(prev => new Set(prev).add(path));
    }
  };

  const handleSelectRemoteFile = async (entry: RemoteEntry) => {
    if (!projectId || addedRemotePaths.has(entry.path)) return;
    setAddingRemotePath(entry.path);
    try {
      const file = await apiService.getAutopilotProjectFileContent(projectId, entry.path);
      _applyLoadedFile(entry.path, file);
    } catch (error: any) {
      toast.error(error?.response?.data?.detail || 'Failed to load file');
    } finally {
      setAddingRemotePath(null);
    }
  };

  const ensureFolder = (path: string) => {
    if (!projectId || !path.trim()) return;
    apiService.ensureAutopilotProjectFolder(projectId, path).catch(() => {});
  };

  const useCurrentFolderAsDestination = () => {
    setDestinationFolder(remotePath);
    ensureFolder(remotePath);
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
        // existing .hephaestus/specs/ staging dir.
        const destination = workflowType
          ? destinationFolder
          : (file.remotePath ? 'queue' : 'docs');
        const result = await apiService.addAutopilotProjectDesign(projectId, name, file.content, ext, destination, workflowType ?? null, file.remotePath ?? null);
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

    // Bug Spec: "Select Local File" feeds the same filename/textarea pair
    // as typing directly, so the picked file previews (and stays editable)
    // there instead of disappearing into a "1 file(s) selected" row with
    // no visible content -- only the first file applies, since there's
    // just one filename/textarea pair to preview it in.
    if (workflowType === 'bugfix') {
      const file = files[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = (event) => {
        setTextFilename(file.name);
        setTextContent((event.target?.result as string) ?? '');
        setTextSourceRemotePath(null);
      };
      reader.readAsText(file);
      if (fileInputRef.current) fileInputRef.current.value = '';
      return;
    }

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

  // Bug Spec's typed content is a file in its own right (see the textarea
  // above) -- combine it with anything picked via "Select Local File" /
  // "Load from Remote" at submit time rather than threading it through
  // loadedFiles on every keystroke.
  const typedFile: LoadedFile[] =
    workflowType === 'bugfix' && textContent.trim()
      ? [{
          name: textFilename.trim() || 'bug-report.md',
          content: textContent,
          size: new Blob([textContent]).size,
          remotePath: textSourceRemotePath ?? undefined,
        }]
      : [];
  const filesToSubmit = [...typedFile, ...loadedFiles];

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
            className="bg-white dark:bg-gray-800 rounded-2xl shadow-2xl w-full max-w-2xl overflow-hidden"
          >
            {/* Header */}
            <div className="px-6 py-4 border-b border-gray-200 dark:border-gray-700 flex items-center justify-between">
              <div className="flex items-center gap-3">
                <div className={`p-2 rounded-lg ${accent.iconBg}`}>
                  <Icon className={`w-5 h-5 ${accent.iconText}`} />
                </div>
                <div>
                  <h2 className="text-lg font-bold text-gray-800 dark:text-gray-100">{copy?.title ?? 'Load Design Files'}</h2>
                  <p className="text-xs text-gray-500 dark:text-gray-400">{copy?.subtitle ?? 'Select or drag & drop design documents from anywhere'}</p>
                </div>
              </div>
              <button
                onClick={onClose}
                className="p-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700 text-gray-500 dark:text-gray-400"
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            {/* Content */}
            <div className="p-6 space-y-4">
              {/* Destination Folder (New Feature / Report Bug flows only) */}
              {workflowType && (
                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Destination Folder</label>
                  <div className="flex items-center gap-2">
                    <input
                      type="text"
                      value={destinationFolder}
                      onChange={(e) => setDestinationFolder(e.target.value)}
                      onBlur={() => ensureFolder(destinationFolder)}
                      placeholder={copy?.defaultFolder}
                      className={`flex-1 px-4 py-2 border border-gray-200 dark:border-gray-600 rounded-xl text-sm font-mono bg-white dark:bg-gray-700 text-gray-800 dark:text-gray-200 placeholder-gray-400 dark:placeholder-gray-500 focus:outline-none focus:ring-2 ${accent.ring}`}
                    />
                    <button
                      type="button"
                      onClick={openFolderBrowser}
                      className="flex items-center gap-1.5 px-3 py-2 text-xs font-medium text-gray-600 dark:text-gray-300 border border-gray-200 dark:border-gray-600 rounded-xl hover:bg-gray-50 dark:hover:bg-gray-700 flex-shrink-0"
                    >
                      <FolderInput className="w-3.5 h-3.5" />
                      Create
                    </button>
                  </div>
                  <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">
                    Every file below is stored here, whether uploaded from your machine or picked from the project.
                  </p>
                </div>
              )}

              {/* Bug Spec: write the report directly, saved to a file in the
                  destination folder -- a bug report is usually composed on
                  the spot, not dragged in from an existing local file. */}
              {workflowType === 'bugfix' ? (
                <div>
                  <div className="flex items-center gap-2 mb-2">
                    <input
                      type="text"
                      value={textFilename}
                      onChange={(e) => { setTextFilename(e.target.value); setTextSourceRemotePath(null); }}
                      placeholder="bug-report.md"
                      className={`w-48 px-3 py-1.5 border border-gray-200 dark:border-gray-600 rounded-lg text-xs font-mono bg-white dark:bg-gray-700 text-gray-800 dark:text-gray-200 placeholder-gray-400 dark:placeholder-gray-500 focus:outline-none focus:ring-2 ${accent.ring}`}
                    />
                    <span className="text-xs text-gray-400 dark:text-gray-500">
                      saved to{' '}
                      <code className="font-mono">{destinationFolder || copy?.defaultFolder}</code>
                    </span>
                  </div>
                  <textarea
                    value={textContent}
                    onChange={(e) => { setTextContent(e.target.value); setTextSourceRemotePath(null); }}
                    placeholder="Describe what's broken: steps to reproduce, expected vs. actual behavior, error messages…"
                    rows={6}
                    className={`w-full px-4 py-3 border border-gray-200 dark:border-gray-600 rounded-xl text-sm bg-white dark:bg-gray-700 text-gray-800 dark:text-gray-200 placeholder-gray-400 dark:placeholder-gray-500 focus:outline-none focus:ring-2 ${accent.ring}`}
                  />
                </div>
              ) : (
                <div
                  onDrop={handleDrop}
                  onDragOver={handleDragOver}
                  onDragLeave={handleDragLeave}
                  onClick={() => fileInputRef.current?.click()}
                  className={`border-2 border-dashed rounded-xl p-8 text-center cursor-pointer transition-all ${
                    isDragOver
                      ? 'border-violet-500 bg-violet-50 dark:bg-violet-900/20'
                      : 'border-gray-200 dark:border-gray-600 hover:border-violet-300 dark:hover:border-violet-500 hover:bg-gray-50 dark:hover:bg-gray-700/50'
                  }`}
                >
                  <FileText className={`w-12 h-12 mx-auto mb-3 ${isDragOver ? 'text-violet-500 dark:text-violet-400' : 'text-gray-300 dark:text-gray-600'}`} />
                  <p className="text-sm font-medium text-gray-600 dark:text-gray-300 mb-1">
                    {isDragOver ? 'Drop files here' : 'Click to select files or drag & drop'}
                  </p>
                  <p className="text-xs text-gray-400 dark:text-gray-500">
                    Supports .md and .txt files &middot; stored in{' '}
                    <code className="font-mono">{workflowType ? destinationFolder || copy?.defaultFolder : 'docs/'}</code>
                  </p>
                </div>
              )}

              <input
                ref={fileInputRef}
                type="file"
                multiple={workflowType !== 'bugfix'}
                accept=".md,.txt,text/plain,text/markdown"
                onChange={handleFileSelect}
                className="hidden"
              />

              {/* Detected Spec Kit Features (REQ-10) -- lets a user jump
                  straight to a known specs/<NNN-name>/ feature without
                  navigating the remote browser folder-by-folder. Renders
                  nothing when the project has no Spec Kit features. */}
              {workflowType === 'feature' && projectId && (
                <div>
                  <label className="text-sm font-medium text-gray-700 dark:text-gray-300 mb-1.5 block">
                    Or pick a Spec Kit feature
                  </label>
                  <SpecKitFeaturePicker
                    projectId={projectId}
                    onSelect={(feature) =>
                      handleSelectSpecFolder({
                        name: `${feature.number}-${feature.slug}`,
                        path: `specs/${feature.number}-${feature.slug}`,
                        type: 'dir',
                      })
                    }
                  />
                </div>
              )}

              <div className="flex justify-start">
                <button
                  type="button"
                  onClick={openRemoteBrowser}
                  className="flex items-center gap-1.5 text-xs font-medium text-violet-600 dark:text-violet-400 hover:text-violet-700 dark:hover:text-violet-300"
                >
                  <FolderOpen className="w-3.5 h-3.5" />
                  Load from Remote
                </button>
              </div>

              {/* Remote File Browser */}
              {remoteOpen && (
                <div className="border border-gray-200 dark:border-gray-600 rounded-xl overflow-hidden">
                  <div className="flex items-center justify-between px-3 py-2 border-b border-gray-200 dark:border-gray-600 bg-gray-50 dark:bg-gray-700/50">
                    <div className="flex items-center gap-2 min-w-0">
                      <button
                        type="button"
                        onClick={() => remoteParent !== null && loadRemoteDir(remoteParent)}
                        disabled={remoteParent === null || remoteLoading}
                        className="p-1 rounded hover:bg-gray-200 dark:hover:bg-gray-600 text-gray-500 dark:text-gray-400 disabled:opacity-30 disabled:cursor-not-allowed"
                      >
                        <ArrowLeft className="w-3.5 h-3.5" />
                      </button>
                      <span className="text-xs text-gray-500 dark:text-gray-400 truncate">/{remotePath}</span>
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
                        className="text-xs font-medium text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200"
                      >
                        Done
                      </button>
                    )}
                  </div>
                  <div className={`${workflowType === 'bugfix' ? 'max-h-32' : 'max-h-64'} overflow-y-auto divide-y divide-gray-100 dark:divide-gray-700`}>
                    {remoteLoading ? (
                      <div className="p-4 text-center text-xs text-gray-400 dark:text-gray-500">Loading…</div>
                    ) : remoteError ? (
                      <div className="p-4 text-center text-xs text-red-500 dark:text-red-400">{remoteError}</div>
                    ) : remoteEntries.length === 0 ? (
                      <div className="p-4 text-center text-xs text-gray-400 dark:text-gray-500">
                        {browsingForFolder ? 'No subfolders here' : 'No folders or .md/.txt files here'}
                      </div>
                    ) : (
                      remoteEntries
                        .filter((entry) => !browsingForFolder || entry.type === 'dir')
                        .map((entry) => {
                        const specFolder = !browsingForFolder && isSpecFolder(entry);
                        const targetPath = specFolder ? `${entry.path}/spec.md` : entry.path;
                        const isAdded = (entry.type === 'file' || specFolder) && addedRemotePaths.has(targetPath);
                        return (
                          <button
                            key={entry.path}
                            type="button"
                            onClick={() => {
                              if (specFolder) return handleSelectSpecFolder(entry);
                              return entry.type === 'dir' ? loadRemoteDir(entry.path) : handleSelectRemoteFile(entry);
                            }}
                            disabled={addingRemotePath === targetPath || isAdded}
                            className="w-full flex items-center gap-2 px-3 py-2 text-left hover:bg-gray-50 dark:hover:bg-gray-700 disabled:opacity-50"
                          >
                            {entry.type === 'dir' ? (
                              <Folder className="w-4 h-4 text-violet-400 dark:text-violet-500 flex-shrink-0" />
                            ) : (
                              <FileText className="w-4 h-4 text-gray-400 dark:text-gray-500 flex-shrink-0" />
                            )}
                            <span className="text-sm text-gray-700 dark:text-gray-300 truncate flex-1">{entry.name}</span>
                            {addingRemotePath === targetPath ? (
                              <div className="animate-spin rounded-full h-3 w-3 border-b-2 border-violet-500 flex-shrink-0" />
                            ) : isAdded ? (
                              <Check className="w-3.5 h-3.5 text-green-500 dark:text-green-400 flex-shrink-0" />
                            ) : null}
                          </button>
                        );
                      })
                    )}
                  </div>
                </div>
              )}

              {/* Spec Folder Preview (Design Spec flow only) -- read-only,
                  just lets you see what a clicked spec folder's spec.md
                  actually says before adding it; the file itself is still
                  tracked via the Loaded Files list below. */}
              {workflowType === 'feature' && specPreview && (
                <div>
                  <div className="flex items-center justify-between mb-1">
                    <label className="text-sm font-medium text-gray-700 dark:text-gray-300">
                      Preview: <code className="font-mono text-xs">{specPreview.path}</code>
                    </label>
                    <button
                      type="button"
                      onClick={() => setSpecPreview(null)}
                      className="text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-300"
                    >
                      <X className="w-3.5 h-3.5" />
                    </button>
                  </div>
                  <textarea
                    readOnly
                    value={specPreview.content}
                    rows={10}
                    className="w-full px-4 py-3 border border-gray-200 dark:border-gray-600 rounded-xl text-xs font-mono bg-gray-50 dark:bg-gray-900 text-gray-700 dark:text-gray-300"
                  />
                </div>
              )}

              {/* Loaded Files List */}
              {loadedFiles.length > 0 && (
                <div className="space-y-2">
                  <p className="text-sm font-medium text-gray-700 dark:text-gray-300">
                    {loadedFiles.length} file(s) selected
                  </p>
                  <div className="max-h-64 overflow-y-auto space-y-2">
                    {loadedFiles.map((file, index) => (
                      <div
                        key={index}
                        className="flex items-center gap-3 p-3 bg-gray-50 dark:bg-gray-700/50 rounded-lg"
                      >
                        <FileText className="w-4 h-4 text-violet-500 dark:text-violet-400 flex-shrink-0" />
                        <div className="flex-1 min-w-0">
                          <p className="text-sm font-medium text-gray-700 dark:text-gray-300 truncate">{file.name}</p>
                          <p className="text-xs text-gray-400 dark:text-gray-500">{formatBytes(file.size)}</p>
                        </div>
                        <button
                          onClick={() => removeFile(index)}
                          className="p-1 rounded hover:bg-gray-200 dark:hover:bg-gray-600 text-gray-400 dark:text-gray-500 hover:text-red-500 dark:hover:text-red-400"
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
                <Button
                  type="button"
                  variant="outline"
                  onClick={onClose}
                  className="text-gray-700 dark:text-gray-300 dark:border-gray-600"
                >
                  Cancel
                </Button>
                {workflowType === 'bugfix' && (
                  <Button
                    type="button"
                    variant="outline"
                    onClick={() => fileInputRef.current?.click()}
                    className="text-gray-700 dark:text-gray-300 dark:border-gray-600"
                  >
                    <Upload className="w-4 h-4 mr-1" />
                    Select Local File
                  </Button>
                )}
                <Button
                  onClick={() => addMutation.mutate(filesToSubmit)}
                  className={accent.button}
                  disabled={addMutation.isPending || filesToSubmit.length === 0 || (!!workflowType && !destinationFolder.trim())}
                >
                  {addMutation.isPending ? (
                    <div className="animate-spin rounded-full h-4 w-4 border-b-2 border-white mr-2" />
                  ) : (
                    <Check className="w-4 h-4 mr-1" />
                  )}
                  Add {filesToSubmit.length > 0 ? `${filesToSubmit.length} ` : ''}to Queue
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
