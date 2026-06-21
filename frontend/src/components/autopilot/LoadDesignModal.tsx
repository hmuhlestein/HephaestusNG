import React, { useState, useRef, useEffect } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { motion, AnimatePresence } from 'framer-motion';
import { X, Upload, FileText, Check } from 'lucide-react';
import { apiService } from '@/services/api';
import { Button } from '@/components/ui/button';
import toast from 'react-hot-toast';

interface LoadDesignModalProps {
  open: boolean;
  projectId: string | null;
  onClose: () => void;
}

interface LoadedFile {
  name: string;
  content: string;
  size: number;
}

const LoadDesignModal: React.FC<LoadDesignModalProps> = ({ open, projectId, onClose }) => {
  const queryClient = useQueryClient();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [loadedFiles, setLoadedFiles] = useState<LoadedFile[]>([]);
  const [isDragOver, setIsDragOver] = useState(false);

  useEffect(() => {
    if (!open) {
      setLoadedFiles([]);
      setIsDragOver(false);
    }
  }, [open]);

  const addMutation = useMutation({
    mutationFn: async (files: LoadedFile[]) => {
      if (!projectId) throw new Error('No project selected');
      
      const results = [];
      for (const file of files) {
        const ext = file.name.endsWith('.txt') ? '.txt' : '.md';
        const name = file.name.replace(/\.[^/.]+$/, '').replace(/[_-]/g, ' ');
        const result = await apiService.addAutopilotProjectDesign(projectId, name, file.content, ext);
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
    setLoadedFiles(prev => prev.filter((_, i) => i !== index));
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
                <div className="p-2 bg-violet-100 rounded-lg">
                  <Upload className="w-5 h-5 text-violet-600" />
                </div>
                <div>
                  <h2 className="text-lg font-bold text-gray-800">Load Design Files</h2>
                  <p className="text-xs text-gray-500">Select or drag & drop design documents from anywhere</p>
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
                  Supports .md and .txt files
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
                  className="bg-violet-600 hover:bg-violet-700 text-white"
                  disabled={addMutation.isPending || loadedFiles.length === 0}
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
