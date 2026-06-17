import React, { useState, useEffect } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { motion, AnimatePresence } from 'framer-motion';
import {
  DndContext,
  closestCenter,
  KeyboardSensor,
  PointerSensor,
  useSensor,
  useSensors,
  DragEndEvent,
} from '@dnd-kit/core';
import {
  arrayMove,
  SortableContext,
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy,
} from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import {
  Plus, Trash2, FileText, Clock, GripVertical, Search, ListOrdered, RefreshCw
} from 'lucide-react';
import { apiService } from '@/services/api';
import { Button } from '@/components/ui/button';
import { formatDistanceToNow } from 'date-fns';
import toast from 'react-hot-toast';

interface DesignQueuePanelProps {
  projectId: string | null;
  onAddDesign: () => void;
  currentDesign?: string | null;
}

const DesignQueuePanel: React.FC<DesignQueuePanelProps> = ({ projectId, onAddDesign, currentDesign }) => {
  const queryClient = useQueryClient();
  const [previewFile, setPreviewFile] = useState<string | null>(null);
  const [previewContent, setPreviewContent] = useState<string>('');
  const [search, setSearch] = useState('');
  const [localOrder, setLocalOrder] = useState<any[] | null>(null);

  const { data: designs, isLoading } = useQuery({
    queryKey: ['autopilot-project-designs', projectId],
    queryFn: () => projectId ? apiService.getAutopilotProjectDesigns(projectId) : Promise.resolve([]),
    enabled: !!projectId,
    refetchInterval: 30000,
  });

  const items = localOrder ?? designs ?? [];

  useEffect(() => {
    if (designs) setLocalOrder(designs);
  }, [designs]);

  useEffect(() => {
    setLocalOrder(null);
  }, [projectId]);

  const reloadMutation = useMutation({
    mutationFn: () => {
      if (!projectId) throw new Error('No project selected');
      return apiService.reloadAutopilotProjectDesigns(projectId);
    },
    onSuccess: (data) => {
      setLocalOrder(data);
      queryClient.setQueryData(['autopilot-project-designs', projectId], data);
      toast.success('Designs reloaded from disk');
    },
    onError: () => toast.error('Failed to reload designs'),
  });

  const removeMutation = useMutation({
    mutationFn: (filename: string) => {
      if (!projectId) throw new Error('No project selected');
      return apiService.removeAutopilotProjectDesign(projectId, filename);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['autopilot-project-designs', projectId] });
      queryClient.invalidateQueries({ queryKey: ['autopilot-projects'] });
      toast.success('Design removed');
    },
    onError: (error: any) => {
      toast.error(error?.response?.data?.detail || 'Failed to remove design');
    },
  });

  const reorderMutation = useMutation({
    mutationFn: (designIds: string[]) => {
      if (!projectId) throw new Error('No project selected');
      return apiService.reorderAutopilotProjectDesigns(projectId, designIds);
    },
    onError: () => {
      queryClient.invalidateQueries({ queryKey: ['autopilot-project-designs', projectId] });
      toast.error('Failed to save order');
    },
  });

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );

  const handleDragEnd = (event: DragEndEvent) => {
    const { active, over } = event;
    if (!over || active.id === over.id) return;

    setLocalOrder((prev) => {
      if (!prev) return prev;
      const oldIndex = prev.findIndex((i) => i.id === active.id);
      const newIndex = prev.findIndex((i) => i.id === over.id);
      const reordered = arrayMove(prev, oldIndex, newIndex);
      reorderMutation.mutate(reordered.map((i) => i.id));
      return reordered;
    });
  };

  const handlePreview = async (filename: string) => {
    if (!projectId) return;
    try {
      const result = await apiService.getAutopilotProjectDesignContent(projectId, filename);
      setPreviewFile(filename);
      setPreviewContent(result.content);
    } catch (e) {
      toast.error('Failed to load preview');
    }
  };

  const filteredQueue = items.filter((item: any) =>
    !search || item.name.toLowerCase().includes(search.toLowerCase()) ||
    item.filename.toLowerCase().includes(search.toLowerCase())
  );

  if (!projectId) {
    return (
      <div className="bg-gray-50 border-2 border-dashed border-gray-200 rounded-2xl p-12 text-center">
        <ListOrdered className="w-12 h-12 text-gray-300 mx-auto mb-4" />
        <h3 className="text-lg font-semibold text-gray-600 mb-2">No project selected</h3>
        <p className="text-sm text-gray-400">Select or create a project to view its design queue</p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <div className="relative flex-1">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400" />
          <input
            type="text"
            placeholder="Search designs..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="w-full pl-10 pr-4 py-2.5 border border-gray-200 rounded-xl text-sm focus:outline-none focus:ring-2 focus:ring-violet-500 focus:border-transparent bg-white"
          />
        </div>
        <Button 
          onClick={() => reloadMutation.mutate()}
          disabled={reloadMutation.isPending}
          variant="outline"
          className="text-gray-600"
        >
          <RefreshCw className={`w-4 h-4 mr-1 ${reloadMutation.isPending ? 'animate-spin' : ''}`} />
          Reload
        </Button>
        <Button onClick={onAddDesign} className="bg-violet-600 hover:bg-violet-700 text-white">
          <Plus className="w-4 h-4 mr-1" />
          Add Design
        </Button>
      </div>
      <p className="text-xs text-gray-400">
        Sorted by filename by default. Drag to reorder manually.
      </p>

      {isLoading ? (
        <div className="flex items-center justify-center h-64">
          <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-violet-500" />
        </div>
      ) : filteredQueue.length > 0 ? (
        <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={handleDragEnd}>
          <SortableContext
            items={filteredQueue.map((i: any) => i.id)}
            strategy={verticalListSortingStrategy}
            disabled={!!search}
          >
            <div className="space-y-2">
              {filteredQueue.map((item: any, index: number) => (
                <SortableDesignItem
                  key={item.id}
                  item={item}
                  index={index}
                  isActive={item.name === currentDesign}
                  onPreview={handlePreview}
                  onRemove={(filename) => {
                    if (confirm(`Remove "${item.name}" from queue?`)) {
                      removeMutation.mutate(filename);
                    }
                  }}
                />
              ))}
            </div>
          </SortableContext>
        </DndContext>
      ) : (
        <div className="bg-gray-50 border-2 border-dashed border-gray-200 rounded-2xl p-12 text-center">
          <ListOrdered className="w-12 h-12 text-gray-300 mx-auto mb-4" />
          <h3 className="text-lg font-semibold text-gray-600 mb-2">Queue is empty</h3>
          <p className="text-sm text-gray-400 mb-4">
            Drop design documents into the queue to start processing
          </p>
          <Button onClick={onAddDesign} variant="outline" className="text-violet-600 border-violet-200 hover:bg-violet-50">
            <Plus className="w-4 h-4 mr-1" />
            Add Design
          </Button>
        </div>
      )}

      <AnimatePresence>
        {previewFile && (
          <motion.div
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 20 }}
            className="bg-white rounded-xl border shadow-lg"
          >
            <div className="px-5 py-3 border-b flex items-center justify-between">
              <div className="flex items-center gap-2">
                <FileText className="w-4 h-4 text-violet-600" />
                <span className="text-sm font-medium text-gray-700">{previewFile}</span>
              </div>
              <button
                onClick={() => { setPreviewFile(null); setPreviewContent(''); }}
                className="text-gray-400 hover:text-gray-600 text-sm"
              >
                Close
              </button>
            </div>
            <div className="p-5 max-h-96 overflow-y-auto">
              <pre className="text-sm text-gray-700 whitespace-pre-wrap font-mono leading-relaxed">
                {previewContent}
              </pre>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
};

// ── Sortable Item ───────────────────────────────────────────────

interface SortableDesignItemProps {
  item: any;
  index: number;
  isActive?: boolean;
  onPreview: (filename: string) => void;
  onRemove: (filename: string) => void;
}

const SortableDesignItem: React.FC<SortableDesignItemProps> = ({ item, index, isActive, onPreview, onRemove }) => {
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id: item.id });

  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
    zIndex: isDragging ? 50 : undefined,
    opacity: isDragging ? 0.8 : 1,
  };

  return (
    <div ref={setNodeRef} style={style} {...attributes}>
      <motion.div
        initial={{ opacity: 0, x: -20 }}
        animate={{ opacity: 1, x: 0 }}
        transition={{ delay: index * 0.03 }}
        onClick={() => onPreview(item.filename)}
        className={`rounded-xl border shadow-sm transition-all cursor-pointer ${
          isDragging ? 'shadow-lg border-violet-300 ring-2 ring-violet-200' :
          isActive ? 'bg-gradient-to-r from-violet-50 to-purple-50 border-violet-300 shadow-md ring-1 ring-violet-200' :
          'bg-white border-gray-100 hover:shadow-md'
        }`}
      >
        <div className="flex items-center gap-4 px-5 py-4">
          {/* Drag handle */}
          <button
            {...listeners}
            className="flex flex-col items-center gap-1 text-gray-300 hover:text-gray-500 cursor-grab active:cursor-grabbing touch-none"
          >
            <GripVertical className="w-5 h-5" />
            <span className="text-xs font-mono text-gray-400">#{item.ordinal ?? index + 1}</span>
          </button>

          <div 
            className={`p-2.5 rounded-lg cursor-pointer ${isActive ? 'bg-violet-200' : 'bg-violet-50'}`}
            onClick={() => onPreview(item.filename)}
          >
            <FileText className={`w-5 h-5 ${isActive ? 'text-violet-700' : 'text-violet-600'}`} />
          </div>

          <div 
            className="flex-1 min-w-0 cursor-pointer"
            onClick={() => onPreview(item.filename)}
          >
            <h4 className="text-sm font-semibold text-gray-800 truncate">{item.name}</h4>
            <div className="flex items-center gap-3 mt-1">
              <span className="text-xs text-gray-500 font-mono">{item.filename}</span>
              <span className="text-xs text-gray-400">{formatBytes(item.size_bytes)}</span>
              {item.modified_at && (
                <span className="text-xs text-gray-400 flex items-center gap-1">
                  <Clock className="w-3 h-3" />
                  {formatDistanceToNow(new Date(item.modified_at), { addSuffix: true })}
                </span>
              )}
            </div>
          </div>

          <div className="flex items-center gap-2">
            {isActive && (
              <span className="px-2 py-1 text-xs font-semibold bg-violet-600 text-white rounded-full animate-pulse">
                Active
              </span>
            )}
            <button
              onClick={(e) => { e.stopPropagation(); onRemove(item.filename); }}
              className="p-2 rounded-lg hover:bg-red-50 transition-colors text-gray-400 hover:text-red-600"
              title="Remove"
            >
              <Trash2 className="w-4 h-4" />
            </button>
          </div>
        </div>
      </motion.div>
    </div>
  );
};

const formatBytes = (bytes: number): string => {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
};

export default DesignQueuePanel;
