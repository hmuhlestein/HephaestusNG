import React, { useState, useRef, useEffect } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { FolderOpen, ChevronDown, Plus, Check, X, Star } from 'lucide-react';
import { useProject } from '@/context/ProjectContext';
import toast from 'react-hot-toast';

interface SidebarProjectSelectorProps {
  collapsed: boolean;
}

const SidebarProjectSelector: React.FC<SidebarProjectSelectorProps> = ({ collapsed }) => {
  const { projects, selectedProject, activateProject, deactivateProject, createProject } = useProject();
  const [open, setOpen] = useState(false);
  const [showAdd, setShowAdd] = useState(false);
  const [newName, setNewName] = useState('');
  const [newPath, setNewPath] = useState('');
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, []);

  if (collapsed) {
    return (
      <div className="px-4 py-3 flex justify-center" title={selectedProject?.name || 'No project'}>
        <FolderOpen className="w-5 h-5 text-violet-500" />
      </div>
    );
  }

  return (
    <div className="relative px-4 py-2" ref={ref}>
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center gap-2 px-3 py-2 bg-gray-50 border border-gray-200 rounded-lg text-sm hover:border-violet-300 hover:bg-violet-50 transition-all"
      >
        <FolderOpen className="w-4 h-4 text-violet-500 flex-shrink-0" />
        <span className="flex-1 text-left truncate font-medium text-gray-700">
          {selectedProject?.name || 'No project'}
        </span>
        <ChevronDown className={`w-3.5 h-3.5 text-gray-400 transition-transform flex-shrink-0 ${open ? 'rotate-180' : ''}`} />
      </button>

      <AnimatePresence>
        {open && (
          <motion.div
            initial={{ opacity: 0, y: -4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -4 }}
            className="absolute z-50 mt-1 left-4 right-4 bg-white border border-gray-200 rounded-xl shadow-lg overflow-hidden"
          >
            <div className="max-h-56 overflow-y-auto">
              {(!projects || projects.length === 0) && (
                <div className="px-4 py-6 text-center text-sm text-gray-400">
                  No projects yet
                </div>
              )}
              {(projects || []).map((proj) => (
                <div
                  key={proj.id}
                  className={`flex items-center gap-3 px-3 py-2.5 cursor-pointer transition-colors ${
                    proj.id === selectedProject?.id ? 'bg-violet-50' : 'hover:bg-gray-50'
                  }`}
                  onClick={() => { activateProject(proj.id); setOpen(false); }}
                  title={proj.is_active ? 'Active — click to view' : 'Click to activate and view'}
                >
                  <span
                    className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${proj.is_active ? 'bg-green-500' : 'bg-gray-300'}`}
                    title={proj.is_active ? 'Active' : 'Inactive'}
                  />
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-1.5">
                      <span className="text-sm font-medium text-gray-800 truncate">{proj.name}</span>
                      {proj.is_default && <Star className="w-3 h-3 text-amber-400 fill-amber-400 flex-shrink-0" />}
                    </div>
                    <span className="text-xs text-gray-400 truncate block">{proj.base_dir}</span>
                  </div>
                  {proj.id === selectedProject?.id && <Check className="w-4 h-4 text-violet-600 flex-shrink-0" />}
                  {proj.is_active && proj.id !== selectedProject?.id && (
                    <button
                      onClick={(e) => { e.stopPropagation(); deactivateProject(proj.id); }}
                      className="p-1 rounded hover:bg-gray-200 text-gray-400 hover:text-gray-600 flex-shrink-0"
                      title="Deactivate"
                    >
                      <X className="w-3.5 h-3.5" />
                    </button>
                  )}
                </div>
              ))}
            </div>

            <div className="border-t px-2 py-1.5">
              <button
                onClick={() => { setShowAdd(true); setOpen(false); }}
                className="w-full flex items-center justify-center gap-1.5 px-3 py-2 text-xs font-medium text-violet-600 hover:bg-violet-50 rounded-lg transition-colors"
              >
                <Plus className="w-3.5 h-3.5" /> Add Project
              </button>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Add Project Dialog */}
      <AnimatePresence>
        {showAdd && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/50 backdrop-blur-sm"
            onClick={(e) => { if (e.target === e.currentTarget) setShowAdd(false); }}
          >
            <motion.div
              initial={{ scale: 0.95, opacity: 0 }}
              animate={{ scale: 1, opacity: 1 }}
              exit={{ scale: 0.95, opacity: 0 }}
              className="bg-white rounded-2xl shadow-2xl w-full max-w-md overflow-hidden"
            >
              <div className="px-6 py-4 border-b flex items-center justify-between">
                <div className="flex items-center gap-3">
                  <div className="p-2 bg-violet-100 rounded-lg">
                    <FolderOpen className="w-5 h-5 text-violet-600" />
                  </div>
                  <div>
                    <h2 className="text-lg font-bold text-gray-800">Add Project</h2>
                    <p className="text-xs text-gray-500">Point to a git repository</p>
                  </div>
                </div>
                <button onClick={() => setShowAdd(false)} className="p-2 rounded-lg hover:bg-gray-100 text-gray-500">
                  <X className="w-4 h-4" />
                </button>
              </div>
              <form
                onSubmit={async (e) => {
                  e.preventDefault();
                  if (!newName.trim() || !newPath.trim()) {
                    toast.error('Name and path are required');
                    return;
                  }
                  try {
                    await createProject(newName, newPath);
                    setShowAdd(false);
                    setNewName('');
                    setNewPath('');
                    toast.success(`Project "${newName}" created`);
                  } catch (err: any) {
                    toast.error(err?.response?.data?.detail || 'Failed to create project');
                  }
                }}
                className="p-6 space-y-4"
              >
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">Project Name</label>
                  <input
                    type="text"
                    value={newName}
                    onChange={(e) => setNewName(e.target.value)}
                    placeholder="e.g. My App"
                    className="w-full px-4 py-2.5 border border-gray-200 rounded-xl text-sm focus:outline-none focus:ring-2 focus:ring-violet-500"
                    autoFocus
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">Repository Path</label>
                  <input
                    type="text"
                    value={newPath}
                    onChange={(e) => setNewPath(e.target.value)}
                    placeholder="/path/to/your/project"
                    className="w-full px-4 py-2.5 border border-gray-200 rounded-xl text-sm font-mono focus:outline-none focus:ring-2 focus:ring-violet-500"
                    autoFocus
                  />
                  <p className="text-xs text-gray-400 mt-1">
                    Must be a git repository. Paste the full path (e.g. /Users/you/code/myproject)
                  </p>
                </div>
                <div className="flex justify-end gap-3 pt-2">
                  <button type="button" onClick={() => setShowAdd(false)} className="px-4 py-2 text-sm border border-gray-200 rounded-xl hover:bg-gray-50">
                    Cancel
                  </button>
                  <button
                    type="submit"
                    disabled={!newName.trim() || !newPath.trim()}
                    className="px-4 py-2 text-sm bg-violet-600 text-white rounded-xl hover:bg-violet-700 disabled:opacity-50"
                  >
                    Create Project
                  </button>
                </div>
              </form>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
};

export default SidebarProjectSelector;
