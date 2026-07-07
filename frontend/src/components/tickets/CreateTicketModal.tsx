import React, { useState } from 'react';
import { useMutation, useQueryClient, useQuery } from '@tanstack/react-query';
import { X, Ticket, Loader2 } from 'lucide-react';
import { apiService } from '@/services/api';
import toast from 'react-hot-toast';

interface CreateTicketModalProps {
  isOpen: boolean;
  onClose: () => void;
  workflowId?: string | null;
  projectId?: string | null;
}

const TICKET_TYPES = [
  { value: 'feature', label: 'Feature', color: 'bg-blue-100 text-blue-700' },
  { value: 'bug', label: 'Bug', color: 'bg-red-100 text-red-700' },
  { value: 'improvement', label: 'Improvement', color: 'bg-green-100 text-green-700' },
  { value: 'task', label: 'Task', color: 'bg-gray-100 text-gray-700' },
  { value: 'infrastructure', label: 'Infrastructure', color: 'bg-orange-100 text-orange-700' },
  { value: 'security', label: 'Security', color: 'bg-purple-100 text-purple-700' },
  { value: 'documentation', label: 'Documentation', color: 'bg-cyan-100 text-cyan-700' },
  { value: 'spike', label: 'Spike', color: 'bg-yellow-100 text-yellow-700' },
];

const PRIORITIES = [
  { value: 'low', label: 'Low', color: 'bg-gray-100 text-gray-600' },
  { value: 'medium', label: 'Medium', color: 'bg-yellow-100 text-yellow-700' },
  { value: 'high', label: 'High', color: 'bg-red-100 text-red-700' },
];

const CreateTicketModal: React.FC<CreateTicketModalProps> = ({ isOpen, onClose, workflowId, projectId }) => {
  const queryClient = useQueryClient();
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [ticketType, setTicketType] = useState('feature');
  const [priority, setPriority] = useState('medium');
  const [tags, setTags] = useState('');
  const [selectedWorkflow, setSelectedWorkflow] = useState<string>('');

  // Fetch workflows for the dropdown
  const { data: executions } = useQuery({
    queryKey: ['workflow-executions', projectId],
    queryFn: () => apiService.listWorkflowExecutions('all'),
    enabled: isOpen && !!projectId,
  });

  // Get board config for available ticket types
  const { data: stats } = useQuery({
    queryKey: ['ticketStats', workflowId || projectId],
    queryFn: () => apiService.getTicketStats(workflowId || projectId || ''),
    enabled: isOpen && !!(workflowId || projectId),
  });

  const availableTypes = stats?.board_config?.ticket_types || TICKET_TYPES.map(t => t.value);
  const defaultType = stats?.board_config?.default_ticket_type || 'feature';

  const createMutation = useMutation({
    mutationFn: async () => {
      const targetWorkflowId = workflowId || selectedWorkflow || executions?.[0]?.id;
      if (!targetWorkflowId) throw new Error('No workflow selected — please select one');

      return apiService.createTicket({
        workflow_id: targetWorkflowId,
        title: title.trim(),
        description: description.trim(),
        ticket_type: ticketType,
        priority,
        tags: tags ? tags.split(',').map(t => t.trim()).filter(Boolean) : [],
      });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['tickets'] });
      queryClient.invalidateQueries({ queryKey: ['ticketStats'] });
      toast.success('Ticket created');
      handleClose();
    },
    onError: (error: any) => {
      toast.error(error?.response?.data?.detail || error?.message || 'Failed to create ticket');
    },
  });

  const handleClose = () => {
    setTitle('');
    setDescription('');
    setTicketType(defaultType);
    setPriority('medium');
    setTags('');
    onClose();
  };

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/50 backdrop-blur-sm" onClick={(e) => { if (e.target === e.currentTarget) handleClose(); }}>
      <div className="bg-white rounded-2xl shadow-2xl w-full max-w-lg overflow-hidden">
        {/* Header */}
        <div className="px-6 py-4 border-b flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="p-2 bg-blue-100 rounded-lg">
              <Ticket className="w-5 h-5 text-blue-600" />
            </div>
            <h2 className="text-lg font-bold text-gray-800">Create Ticket</h2>
          </div>
          <button onClick={handleClose} className="p-2 rounded-lg hover:bg-gray-100 text-gray-500">
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* Form */}
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (!title.trim()) {
              toast.error('Title is required');
              return;
            }
            createMutation.mutate();
          }}
          className="p-6 space-y-4"
        >
          {/* Title */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Title</label>
            <input
              type="text"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="Brief description of the ticket"
              className="w-full px-4 py-2.5 border border-gray-200 rounded-xl text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              autoFocus
            />
          </div>

          {/* Description */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Description</label>
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Detailed description, acceptance criteria, etc."
              rows={4}
              className="w-full px-4 py-2.5 border border-gray-200 rounded-xl text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 resize-none"
            />
          </div>

          {/* Workflow selector (only when no workflow is pre-selected) */}
          {!workflowId && executions && executions.length > 0 && (
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Workflow</label>
              <select
                value={selectedWorkflow}
                onChange={(e) => setSelectedWorkflow(e.target.value)}
                className="w-full px-4 py-2.5 border border-gray-200 rounded-xl text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 bg-white"
              >
                <option value="">Select workflow...</option>
                {executions.map((exec: any) => (
                  <option key={exec.id} value={exec.id}>
                    {exec.definition_name || exec.description?.split('\n')[0] || exec.id.slice(0, 12)}
                  </option>
                ))}
              </select>
            </div>
          )}

          {/* Type and Priority */}
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Type</label>
              <select
                value={ticketType}
                onChange={(e) => setTicketType(e.target.value)}
                className="w-full px-4 py-2.5 border border-gray-200 rounded-xl text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 bg-white"
              >
                {availableTypes.map((type) => (
                  <option key={type} value={type}>
                    {type.charAt(0).toUpperCase() + type.slice(1).replace('-', ' ')}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Priority</label>
              <select
                value={priority}
                onChange={(e) => setPriority(e.target.value)}
                className="w-full px-4 py-2.5 border border-gray-200 rounded-xl text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 bg-white"
              >
                {PRIORITIES.map((p) => (
                  <option key={p.value} value={p.value}>{p.label}</option>
                ))}
              </select>
            </div>
          </div>

          {/* Tags */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Tags <span className="text-gray-400 font-normal">(comma separated)</span></label>
            <input
              type="text"
              value={tags}
              onChange={(e) => setTags(e.target.value)}
              placeholder="e.g. frontend, api, urgent"
              className="w-full px-4 py-2.5 border border-gray-200 rounded-xl text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>

          {/* Actions */}
          <div className="flex justify-end gap-3 pt-2">
            <button
              type="button"
              onClick={handleClose}
              className="px-4 py-2 text-sm border border-gray-200 rounded-xl hover:bg-gray-50"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={createMutation.isPending || !title.trim()}
              className="px-4 py-2 text-sm bg-blue-600 text-white rounded-xl hover:bg-blue-700 disabled:opacity-50 flex items-center gap-2"
            >
              {createMutation.isPending && <Loader2 className="w-4 h-4 animate-spin" />}
              Create Ticket
            </button>
          </div>
        </form>
      </div>
    </div>
  );
};

export default CreateTicketModal;
