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
  Plus, Trash2, Eye, FileText, Clock, GripVertical, Search, ListOrdered
} from 'lucide-react';
import { apiService } from '@/services/api';
import { Button } from '@/components/ui/button';
import { formatDistanceToNow } from 'date-fns';
import toast from 'react-hot-toast';

interface DesignQueuePanelProps {
  onAddDesign: () => void;
}

const DesignQueuePanel: React.FC<DesignQueuePanelProps> = ({ onAddDesign }) => {
  const queryClient = useQueryClient();
  const [previewFile, setPreviewFile] = useState<string | null>(null);
  const [previewContent, setPreviewContent] = useState<string>('');
  const [search, setSearch] = useState('');
  const [localOrder, setLocalOrder] = useState<any[] | null>(null);

  const { data: queue, isLoading } = useQuery({
    queryKey: ['autopilot-queue'],
    queryFn: () => apiService.getAutopilotQueue(),
    refetchInterval: 30000,
  });

  const items = localOrder ?? queue ?? [];

  useEffect(() => {
    if (queue) setLocalOrder(queue);
  }, [queue]);

  const removeMutation = useMutation({
    mutationFn: (filename: string) => apiService.removeFromAutopilotQueue(filename),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['autopilot-queue'] });
      toast.success('Design removed from queue');
    },
    onError: (error: any) => {
      toast.error(error?.response?.data?.detail || 'Failed to remove design');
    },
  });

  const reorderMutation = useMutation({
    mutationFn: (filenames: string[]) => apiService.reorderAutopilotQueue(filenames),
    onError: () => {
      queryClient.invalidateQueries({ queryKey: ['autopilot-queue'] });
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
      const oldIndex = prev.findIndex((i) => i.filename === active.id);
      const newIndex = prev.findIndex((i) => i.filename === over.id);
      const reordered = arrayMove(prev, oldIndex, newIndex);
      reorderMutation.mutate(reordered.map((i) => i.filename));
      return reordered;
    });
  };

  const handlePreview = async (filename: string) => {
    try {
      const result = await apiService.getAutopilotQueueContent(filename);
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
        <Button onClick={onAddDesign} className="bg-violet-600 hover:bg-violet-700 text-white">
          <Plus className="w-4 h-4 mr-1" />
          Add Design
        </Button>
      </div>

      {isLoading ? (
        <div className="flex items-center justify-center h-64">
          <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-violet-500" />
        </div>
      ) : filteredQueue.length > 0 ? (
        <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={handleDragEnd}>
          <SortableContext items={filteredQueue.map((i: any) => i.filename)} strategy={verticalListSortingStrategy}>
            <div className="space-y-2">
              {filteredQueue.map((item: any, index: number) => (
                <SortableDesignItem
                  key={item.filename}
                  item={item}
                  index={index}
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
  onPreview: (filename: string) => void;
  onRemove: (filename: string) => void;
}

const SortableDesignItem: React.FC<SortableDesignItemProps> = ({ item, index, onPreview, onRemove }) => {
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id: item.filename });

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
        className={`bg-white rounded-xl border shadow-sm hover:shadow-md transition-shadow ${
          isDragging ? 'shadow-lg border-violet-300 ring-2 ring-violet-200' : 'border-gray-100'
        }`}
      >
        <div className="flex items-center gap-4 px-5 py-4">
          {/* Drag handle */}
          <button
            {...listeners}
            className="flex flex-col items-center gap-1 text-gray-300 hover:text-gray-500 cursor-grab active:cursor-grabbing touch-none"
          >
            <GripVertical className="w-5 h-5" />
            <span className="text-xs font-mono text-gray-400">#{index + 1}</span>
          </button>

          <div className="p-2.5 bg-violet-50 rounded-lg">
            <FileText className="w-5 h-5 text-violet-600" />
          </div>

          <div className="flex-1 min-w-0">
            <h4 className="text-sm font-semibold text-gray-800 truncate">{item.name}</h4>
            <div className="flex items-center gap-3 mt-1">
              <span className="text-xs text-gray-500 font-mono">{item.filename}</span>
              <span className="text-xs text-gray-400">{formatBytes(item.size_bytes)}</span>
              <span className="text-xs text-gray-400 flex items-center gap-1">
                <Clock className="w-3 h-3" />
                {formatDistanceToNow(new Date(item.modified), { addSuffix: true })}
              </span>
            </div>
          </div>

          <div className="flex items-center gap-2">
            <button
              onClick={() => onPreview(item.filename)}
              className="p-2 rounded-lg hover:bg-gray-100 transition-colors text-gray-500 hover:text-gray-700"
              title="Preview"
            >
              <Eye className="w-4 h-4" />
            </button>
            <button
              onClick={() => onRemove(item.filename)}
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
