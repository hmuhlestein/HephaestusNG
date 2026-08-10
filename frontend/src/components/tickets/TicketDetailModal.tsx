import React, { useState } from 'react';
import { useQuery, useQueryClient, useMutation } from '@tanstack/react-query';
import { motion, AnimatePresence } from 'framer-motion';
import {
  X,
  Edit2,
  User,
  Tag,
  MessageCircle,
  GitCommit,
  Lock,
  CheckCircle,
  AlertCircle,
  Send,
  ExternalLink,
  ChevronDown,
  ChevronUp,
} from 'lucide-react';
import { formatDistanceToNow, format } from 'date-fns';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
import { toast } from 'react-hot-toast';
import { apiService } from '@/services/api';
import { cn } from '@/lib/utils';
import GitDiffModal from './GitDiffModal';
import AgentDetailModal from '../AgentDetailModal';
import { PhaseBadge } from '../PhaseBadge';
import TaskDetailModal from '../TaskDetailModal';
import TicketApprovalUI from './TicketApprovalUI';

interface TicketDetailModalProps {
  ticketId: string;
  onClose: () => void;
  onNavigateToSearchTab?: (tag: string) => void;
}

// Helper component for clickable agent IDs
const ClickableAgentId: React.FC<{ agentId: string; onClick: (agentId: string) => void; className?: string }> = ({
  agentId,
  onClick,
  className = '',
}) => {
  return (
    <button
      onClick={() => onClick(agentId)}
      className={cn(
        'font-medium font-mono text-xs text-blue-600 dark:text-blue-400 hover:text-blue-800 dark:hover:text-blue-300 hover:underline cursor-pointer transition-colors',
        className
      )}
      title={`View agent details: ${agentId}`}
    >
      {agentId}
    </button>
  );
};

// Helper component for blocked ticket items that fetches ticket details
const BlockedByTicketItem: React.FC<{
  ticketId: string;
  onClick: (ticketId: string) => void;
}> = ({ ticketId, onClick }) => {
  const { data, isLoading } = useQuery({
    queryKey: ['ticket', ticketId],
    queryFn: () => apiService.getTicket(ticketId),
    staleTime: 30000, // Cache for 30 seconds
  });

  const ticket = data?.ticket;

  if (isLoading) {
    return (
      <div className="p-2 bg-white dark:bg-gray-800 rounded border border-red-200 dark:border-red-600 text-sm animate-pulse">
        <div className="h-4 bg-gray-200 dark:bg-gray-700 rounded w-3/4"></div>
      </div>
    );
  }

  if (!ticket) {
    return (
      <div className="p-2 bg-white dark:bg-gray-800 rounded border border-red-200 dark:border-red-600 text-sm font-mono text-gray-500 dark:text-gray-400">
        {ticketId}
      </div>
    );
  }

  const tooltipText = `${ticket.title}\n\n${ticket.description.substring(0, 150)}${
    ticket.description.length > 150 ? '...' : ''
  }`;

  return (
    <button
      onClick={() => onClick(ticketId)}
      className="w-full p-2 bg-white dark:bg-gray-800 rounded border border-red-200 dark:border-red-600 text-left hover:bg-red-50 dark:hover:bg-red-900/30 hover:border-red-300 dark:hover:border-red-500 transition-colors cursor-pointer group"
      title={tooltipText}
    >
      <div className="flex items-center justify-between mb-1">
        <span className="text-xs font-mono text-gray-500 dark:text-gray-400">{ticketId}</span>
        <div className="flex items-center space-x-1">
          {ticket.is_blocked && (
            <span className="px-1.5 py-0.5 bg-red-100 dark:bg-red-900/40 text-red-700 dark:text-red-300 text-xs rounded">
              <Lock className="w-3 h-3 inline" />
            </span>
          )}
          <span className="px-1.5 py-0.5 bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300 text-xs rounded capitalize">
            {ticket.ticket_type}
          </span>
        </div>
      </div>
      <div className="text-sm font-medium text-gray-900 dark:text-gray-100 line-clamp-2 group-hover:text-blue-600 dark:group-hover:text-blue-400 transition-colors">
        {ticket.title}
      </div>
    </button>
  );
};

// Helper component for related task items that fetches task details
const RelatedTaskItem: React.FC<{
  taskId: string;
  onClick: (taskId: string) => void;
}> = ({ taskId, onClick }) => {
  const { data: task, isLoading } = useQuery({
    queryKey: ['task', taskId],
    queryFn: () => apiService.getTaskById(taskId),
    staleTime: 30000, // Cache for 30 seconds
  });

  if (isLoading) {
    return (
      <div className="p-3 bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 animate-pulse">
        <div className="flex items-center space-x-2 mb-2">
          <div className="h-5 w-16 bg-gray-200 dark:bg-gray-700 rounded"></div>
          <div className="h-4 w-24 bg-gray-200 dark:bg-gray-700 rounded"></div>
        </div>
        <div className="h-4 bg-gray-200 dark:bg-gray-700 rounded w-3/4 mb-2"></div>
        <div className="h-3 bg-gray-200 dark:bg-gray-700 rounded w-1/2"></div>
      </div>
    );
  }

  if (!task) {
    return (
      <div className="p-3 bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 text-sm font-mono text-gray-500 dark:text-gray-400">
        {taskId}
      </div>
    );
  }

  // Status color mapping
  const getStatusColor = (status: string) => {
    switch (status) {
      case 'done':
        return 'bg-green-100 dark:bg-green-900/40 text-green-700 dark:text-green-300 border-green-200 dark:border-green-600';
      case 'in_progress':
        return 'bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-300 border-blue-200 dark:border-blue-600';
      case 'failed':
        return 'bg-red-100 dark:bg-red-900/40 text-red-700 dark:text-red-300 border-red-200 dark:border-red-600';
      case 'blocked':
        return 'bg-orange-100 dark:bg-orange-900/40 text-orange-700 dark:text-orange-300 border-orange-200 dark:border-orange-600';
      case 'pending':
      case 'queued':
        return 'bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300 border-gray-200 dark:border-gray-600';
      default:
        return 'bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300 border-gray-200 dark:border-gray-600';
    }
  };

  const statusColorClass = getStatusColor(task.status);

  return (
    <button
      onClick={() => onClick(taskId)}
      className="w-full p-3 bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 text-left hover:bg-blue-50 dark:hover:bg-blue-900/30 hover:border-blue-300 dark:hover:border-blue-500 hover:shadow-sm transition-all cursor-pointer group"
    >
      {/* Header: Phase Badge + Task ID */}
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center space-x-2">
          {task.phase_order && task.phase_name ? (
            <PhaseBadge
              phaseOrder={task.phase_order}
              phaseName={task.phase_name}
              className="flex-shrink-0"
            />
          ) : (
            <span className="px-2 py-0.5 bg-gray-200 dark:bg-gray-700 text-gray-600 dark:text-gray-400 text-xs rounded font-medium">
              No Phase
            </span>
          )}
          <span className="text-xs font-mono text-gray-500 dark:text-gray-400 group-hover:text-blue-600 dark:group-hover:text-blue-400 transition-colors">
            {taskId}
          </span>
        </div>
        <ExternalLink className="w-3 h-3 text-gray-400 opacity-0 group-hover:opacity-100 transition-opacity" />
      </div>

      {/* Description */}
      <div className="text-sm font-medium text-gray-900 dark:text-gray-100 line-clamp-2 mb-2 group-hover:text-blue-700 dark:group-hover:text-blue-400 transition-colors">
        {task.description}
      </div>

      {/* Footer: Status + Priority */}
      <div className="flex items-center space-x-2">
        <span className={cn(
          'px-2 py-0.5 text-xs rounded-full border capitalize font-medium',
          statusColorClass
        )}>
          {task.status.replace('_', ' ')}
        </span>
        {task.priority && (
          <span className={cn(
            'px-2 py-0.5 text-xs rounded capitalize',
            task.priority === 'high' && 'bg-red-50 dark:bg-red-900/30 text-red-600 dark:text-red-400',
            task.priority === 'medium' && 'bg-yellow-50 dark:bg-yellow-900/30 text-yellow-600 dark:text-yellow-400',
            task.priority === 'low' && 'bg-gray-50 dark:bg-gray-700 text-gray-600 dark:text-gray-400'
          )}>
            {task.priority}
          </span>
        )}
      </div>
    </button>
  );
};

const TicketDetailModal: React.FC<TicketDetailModalProps> = ({ ticketId, onClose, onNavigateToSearchTab }) => {
  const [selectedCommitSha, setSelectedCommitSha] = useState<string | null>(null);
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null);
  const [selectedBlockedTicketId, setSelectedBlockedTicketId] = useState<string | null>(null);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [commentText, setCommentText] = useState('');
  const [isEditingDescription, setIsEditingDescription] = useState(false);
  const [editedDescription, setEditedDescription] = useState('');
  const [expandedSections, setExpandedSections] = useState({
    description: true,
    relatedTasks: true,
    activity: true,
    comments: true,
    blocking: true,
    blocks: true,
    commits: true,
    related: false,
  });

  const queryClient = useQueryClient();

  // Fetch ticket details
  const { data, isLoading } = useQuery({
    queryKey: ['ticket', ticketId],
    queryFn: () => apiService.getTicket(ticketId),
    enabled: !!ticketId,
    refetchInterval: 5000, // Refresh every 5 seconds
  });

  const ticket = data?.ticket;
  const comments = data?.comments || [];
  const history = data?.history || [];
  const commits = data?.commits || [];

  // Add comment mutation
  const addCommentMutation = useMutation({
    mutationFn: (text: string) => apiService.addTicketComment(ticketId, text),
    onSuccess: () => {
      setCommentText('');
      queryClient.invalidateQueries({ queryKey: ['ticket', ticketId] });
      toast.success('Comment added');
    },
    onError: () => {
      toast.error('Failed to add comment');
    },
  });

  // Update description mutation
  const updateDescriptionMutation = useMutation({
    mutationFn: (description: string) =>
      apiService.updateTicket(ticketId, { description }),
    onSuccess: () => {
      setIsEditingDescription(false);
      queryClient.invalidateQueries({ queryKey: ['ticket', ticketId] });
      toast.success('Description updated');
    },
    onError: () => {
      toast.error('Failed to update description');
    },
  });

  const toggleSection = (section: keyof typeof expandedSections) => {
    setExpandedSections((prev) => ({ ...prev, [section]: !prev[section] }));
  };

  const handleSubmitComment = () => {
    if (!commentText.trim()) return;
    addCommentMutation.mutate(commentText);
  };

  const handleSaveDescription = () => {
    if (!editedDescription.trim()) return;
    updateDescriptionMutation.mutate(editedDescription);
  };

  const startEditingDescription = () => {
    setEditedDescription(ticket?.description || '');
    setIsEditingDescription(true);
  };

  if (isLoading || !ticket) {
    return (
      <AnimatePresence>
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center p-4 z-50"
          onClick={onClose}
        >
          <div className="bg-white dark:bg-gray-800 rounded-lg p-8">
            <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-blue-500 dark:border-blue-400 mx-auto"></div>
          </div>
        </motion.div>
      </AnimatePresence>
    );
  }

  return (
    <>
      <AnimatePresence>
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center p-4 z-50"
          onClick={onClose}
        >
          <motion.div
            initial={{ scale: 0.95, opacity: 0 }}
            animate={{ scale: 1, opacity: 1 }}
            exit={{ scale: 0.95, opacity: 0 }}
            className="bg-white dark:bg-gray-800 rounded-lg shadow-2xl w-full max-w-7xl max-h-[90vh] overflow-hidden flex flex-col"
            onClick={(e) => e.stopPropagation()}
          >
            {/* Header */}
            <div className="px-6 py-4 border-b bg-gradient-to-r from-blue-50 to-indigo-50 dark:from-blue-900/30 dark:to-indigo-900/30">
              <div className="flex items-start justify-between">
                <div className="flex-1">
                  <div className="flex items-center space-x-3 mb-2">
                    <span className="text-sm font-mono text-gray-600 dark:text-gray-400">
                      {ticket.id}
                    </span>
                    {ticket.is_blocked && (
                      <span className="flex items-center px-2 py-1 bg-red-100 dark:bg-red-900/40 text-red-700 dark:text-red-300 text-xs rounded">
                        <Lock className="w-3 h-3 mr-1" />
                        Blocked
                      </span>
                    )}
                    {ticket.is_resolved && (
                      <span className="flex items-center px-2 py-1 bg-green-100 dark:bg-green-900/40 text-green-700 dark:text-green-300 text-xs rounded">
                        <CheckCircle className="w-3 h-3 mr-1" />
                        Resolved
                      </span>
                    )}
                  </div>
                  <h2 className="text-2xl font-bold text-gray-900 dark:text-gray-100">{ticket.title}</h2>
                </div>
                <button
                  onClick={onClose}
                  className="p-2 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors"
                >
                  <X className="w-5 h-5 text-gray-500 dark:text-gray-400" />
                </button>
              </div>
            </div>

            {/* Approval UI - show if pending review */}
            {ticket.approval_status === 'pending_review' && (
              <div className="px-6 pt-6">
                <TicketApprovalUI
                  ticketId={ticketId}
                  onApproved={() => {
                    queryClient.invalidateQueries({ queryKey: ['ticket', ticketId] });
                  }}
                  onRejected={onClose} // Close modal after rejection since ticket will be deleted
                />
              </div>
            )}

            {/* Content */}
            <div className="flex-1 overflow-y-auto">
              <div className="grid grid-cols-3 gap-6 p-6">
                {/* LEFT PANEL - 2/3 width */}
                <div className="col-span-2 space-y-6">
                  {/* Description Section */}
                  <div>
                    <button
                      onClick={() => toggleSection('description')}
                      className="flex items-center justify-between w-full text-lg font-semibold text-gray-900 dark:text-gray-100 mb-3"
                    >
                      <span>Description</span>
                      {expandedSections.description ? (
                        <ChevronUp className="w-5 h-5" />
                      ) : (
                        <ChevronDown className="w-5 h-5" />
                      )}
                    </button>

                    {expandedSections.description && (
                      <div className="bg-gray-50 dark:bg-gray-800/50 rounded-lg p-4 border border-gray-200 dark:border-gray-700">
                        {isEditingDescription ? (
                          <div>
                            <textarea
                              value={editedDescription}
                              onChange={(e) => setEditedDescription(e.target.value)}
                              className="w-full h-48 p-3 border border-gray-200 dark:border-gray-600 rounded-lg resize-none bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 focus:ring-2 focus:ring-blue-500"
                              placeholder="Enter description..."
                            />
                            <div className="flex space-x-2 mt-2">
                              <button
                                onClick={handleSaveDescription}
                                className="px-4 py-2 bg-blue-600 dark:bg-blue-700 text-white rounded-lg hover:bg-blue-700 dark:hover:bg-blue-600"
                              >
                                Save
                              </button>
                              <button
                                onClick={() => setIsEditingDescription(false)}
                                className="px-4 py-2 bg-gray-200 dark:bg-gray-700 text-gray-700 dark:text-gray-300 rounded-lg hover:bg-gray-300 dark:hover:bg-gray-600"
                              >
                                Cancel
                              </button>
                            </div>
                          </div>
                        ) : (
                          <div className="relative group">
                            <div className="prose prose-sm max-w-none">
                              <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]}>
                                {ticket.description}
                              </ReactMarkdown>
                            </div>
                            <button
                              onClick={startEditingDescription}
                              className="absolute top-0 right-0 p-2 opacity-0 group-hover:opacity-100 transition-opacity bg-white dark:bg-gray-700 rounded-lg shadow hover:bg-gray-50 dark:hover:bg-gray-600"
                            >
                              <Edit2 className="w-4 h-4 text-gray-600 dark:text-gray-400" />
                            </button>
                          </div>
                        )}
                      </div>
                    )}
                  </div>

                  {/* Related Tasks Section */}
                  <div>
                    <button
                      onClick={() => toggleSection('relatedTasks')}
                      className="flex items-center justify-between w-full text-lg font-semibold text-gray-900 dark:text-gray-100 mb-3"
                    >
                      <span className="flex items-center">
                        Related Tasks
                        {ticket.related_task_ids && ticket.related_task_ids.length > 0 && (
                          <span className="ml-2 px-2 py-0.5 bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-300 text-xs rounded-full font-medium">
                            {ticket.related_task_ids.length}
                          </span>
                        )}
                      </span>
                      {expandedSections.relatedTasks ? (
                        <ChevronUp className="w-5 h-5" />
                      ) : (
                        <ChevronDown className="w-5 h-5" />
                      )}
                    </button>

                    {expandedSections.relatedTasks && (
                      <div>
                        {!ticket.related_task_ids || ticket.related_task_ids.length === 0 ? (
                          <div className="bg-gray-50 dark:bg-gray-800/50 rounded-lg p-4 border border-gray-200 dark:border-gray-700 text-center">
                            <p className="text-sm text-gray-500 dark:text-gray-400">No related tasks yet</p>
                            <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">
                              Tasks will appear here when they reference this ticket
                            </p>
                          </div>
                        ) : (
                          <div className="space-y-3 max-h-96 overflow-y-auto">
                            {ticket.related_task_ids.map((taskId) => (
                              <RelatedTaskItem
                                key={taskId}
                                taskId={taskId}
                                onClick={setSelectedTaskId}
                              />
                            ))}
                          </div>
                        )}
                      </div>
                    )}
                  </div>

                  {/* Activity Timeline */}
                  <div>
                    <button
                      onClick={() => toggleSection('activity')}
                      className="flex items-center justify-between w-full text-lg font-semibold text-gray-900 dark:text-gray-100 mb-3"
                    >
                      <span>Activity Timeline</span>
                      {expandedSections.activity ? (
                        <ChevronUp className="w-5 h-5" />
                      ) : (
                        <ChevronDown className="w-5 h-5" />
                      )}
                    </button>

                    {expandedSections.activity && (
                      <div className="space-y-3 max-h-96 overflow-y-auto">
                        {history.length === 0 ? (
                          <p className="text-sm text-gray-500 dark:text-gray-400">No activity yet</p>
                        ) : (
                          history.map((entry) => (
                            <div
                              key={entry.id}
                              className="flex space-x-3 p-3 bg-gray-50 dark:bg-gray-800/50 rounded-lg border border-gray-200 dark:border-gray-700"
                            >
                              <div className="flex-shrink-0">
                                {entry.change_type === 'created' && (
                                  <div className="w-8 h-8 bg-blue-100 dark:bg-blue-800/50 rounded-full flex items-center justify-center">
                                    <CheckCircle className="w-4 h-4 text-blue-600 dark:text-blue-400" />
                                  </div>
                                )}
                                {entry.change_type === 'status_changed' && (
                                  <div className="w-8 h-8 bg-purple-100 dark:bg-purple-800/50 rounded-full flex items-center justify-center">
                                    <AlertCircle className="w-4 h-4 text-purple-600 dark:text-purple-400" />
                                  </div>
                                )}
                                {entry.change_type === 'commented' && (
                                  <div className="w-8 h-8 bg-green-100 dark:bg-green-800/50 rounded-full flex items-center justify-center">
                                    <MessageCircle className="w-4 h-4 text-green-600 dark:text-green-400" />
                                  </div>
                                )}
                                {entry.change_type === 'commit_linked' && (
                                  <div className="w-8 h-8 bg-orange-100 dark:bg-orange-800/50 rounded-full flex items-center justify-center">
                                    <GitCommit className="w-4 h-4 text-orange-600 dark:text-orange-400" />
                                  </div>
                                )}
                              </div>
                              <div className="flex-1">
                                <p className="text-sm text-gray-900 dark:text-gray-100">{entry.change_description}</p>
                                <div className="flex items-center space-x-2 mt-1 text-xs">
                                  <ClickableAgentId
                                    agentId={entry.agent_id}
                                    onClick={setSelectedAgentId}
                                  />
                                  <span className="text-gray-500 dark:text-gray-400">•</span>
                                  <span className="text-gray-500 dark:text-gray-400">{formatDistanceToNow(new Date(entry.changed_at), { addSuffix: true })}</span>
                                </div>
                              </div>
                            </div>
                          ))
                        )}
                      </div>
                    )}
                  </div>

                  {/* Comments Section */}
                  <div>
                    <button
                      onClick={() => toggleSection('comments')}
                      className="flex items-center justify-between w-full text-lg font-semibold text-gray-900 dark:text-gray-100 mb-3"
                    >
                      <span>Comments ({comments.length})</span>
                      {expandedSections.comments ? (
                        <ChevronUp className="w-5 h-5" />
                      ) : (
                        <ChevronDown className="w-5 h-5" />
                      )}
                    </button>

                    {expandedSections.comments && (
                      <div className="space-y-4">
                        {/* Comment List */}
                        <div className="space-y-3 max-h-64 overflow-y-auto">
                          {comments.map((comment) => (
                            <div key={comment.id} className="p-3 bg-gray-50 dark:bg-gray-800/50 rounded-lg border border-gray-200 dark:border-gray-700">
                              <div className="flex items-start justify-between mb-2">
                                <div className="flex items-center space-x-2">
                                  <User className="w-4 h-4 text-gray-600 dark:text-gray-400" />
                                  <ClickableAgentId
                                    agentId={comment.agent_id}
                                    onClick={setSelectedAgentId}
                                    className="text-sm"
                                  />
                                </div>
                                <span className="text-xs text-gray-500 dark:text-gray-400">
                                  {formatDistanceToNow(new Date(comment.created_at), { addSuffix: true })}
                                </span>
                              </div>
                              <p className="text-sm text-gray-700 dark:text-gray-300 whitespace-pre-wrap">{comment.comment_text}</p>
                            </div>
                          ))}
                        </div>

                        {/* Add Comment Form */}
                        <div className="border-t pt-4">
                          <textarea
                            value={commentText}
                            onChange={(e) => setCommentText(e.target.value)}
                            placeholder="Add a comment..."
                            className="w-full px-3 py-2 border border-gray-200 dark:border-gray-600 rounded-lg resize-none bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 focus:ring-2 focus:ring-blue-500"
                            rows={3}
                          />
                          <div className="flex justify-end mt-2">
                            <button
                              onClick={handleSubmitComment}
                              disabled={!commentText.trim() || addCommentMutation.isPending}
                              className="flex items-center px-4 py-2 bg-blue-600 dark:bg-blue-700 text-white rounded-lg hover:bg-blue-700 dark:hover:bg-blue-600 disabled:opacity-50 disabled:cursor-not-allowed"
                            >
                              <Send className="w-4 h-4 mr-2" />
                              Comment
                            </button>
                          </div>
                        </div>
                      </div>
                    )}
                  </div>
                </div>

                {/* RIGHT PANEL - 1/3 width */}
                <div className="space-y-6">
                  {/* Details Section */}
                  <div className="bg-gray-50 dark:bg-gray-800/50 rounded-lg p-4 border border-gray-200 dark:border-gray-700">
                    <h3 className="font-semibold text-gray-900 dark:text-gray-100 mb-3">Details</h3>
                    <div className="space-y-3 text-sm">
                      <div>
                        <div className="text-gray-600 dark:text-gray-400 mb-1">Type</div>
                        <div className="font-medium capitalize">{ticket.ticket_type}</div>
                      </div>
                      <div>
                        <div className="text-gray-600 dark:text-gray-400 mb-1">Priority</div>
                        <div className={cn(
                          'font-medium capitalize',
                          ticket.priority === 'critical' && 'text-red-600 dark:text-red-400',
                          ticket.priority === 'high' && 'text-orange-600 dark:text-orange-400',
                          ticket.priority === 'medium' && 'text-yellow-600 dark:text-yellow-400',
                          ticket.priority === 'low' && 'text-gray-600 dark:text-gray-400'
                        )}>
                          {ticket.priority}
                        </div>
                      </div>
                      <div>
                        <div className="text-gray-600 dark:text-gray-400 mb-1">Status</div>
                        <div className="font-medium capitalize text-gray-900 dark:text-gray-100">{ticket.status}</div>
                      </div>
                      <div>
                        <div className="text-gray-600 dark:text-gray-400 mb-1">Created</div>
                        <div className="font-medium text-gray-900 dark:text-gray-100">
                          {format(new Date(ticket.created_at), 'MMM d, yyyy HH:mm')}
                        </div>
                      </div>
                      {ticket.started_at && (
                        <div>
                          <div className="text-gray-600 dark:text-gray-400 mb-1">Started</div>
                          <div className="font-medium text-gray-900 dark:text-gray-100">
                            {format(new Date(ticket.started_at), 'MMM d, yyyy HH:mm')}
                          </div>
                        </div>
                      )}
                      {ticket.completed_at && (
                        <div>
                          <div className="text-gray-600 dark:text-gray-400 mb-1">Completed</div>
                          <div className="font-medium text-gray-900 dark:text-gray-100">
                            {format(new Date(ticket.completed_at), 'MMM d, yyyy HH:mm')}
                          </div>
                        </div>
                      )}
                    </div>
                  </div>

                  {/* Agents Section */}
                  <div className="bg-gray-50 dark:bg-gray-800/50 rounded-lg p-4 border border-gray-200 dark:border-gray-700">
                    <h3 className="font-semibold text-gray-900 dark:text-gray-100 mb-3">Agents</h3>
                    <div className="space-y-3 text-sm">
                      {/* Collect all unique agents involved */}
                      {(() => {
                        const agentRoles = new Map<string, string[]>();

                        // Creator
                        if (ticket.created_by_agent_id) {
                          agentRoles.set(ticket.created_by_agent_id, ['Creator']);
                        }

                        // Assigned
                        if (ticket.assigned_agent_id) {
                          const roles = agentRoles.get(ticket.assigned_agent_id) || [];
                          roles.push('Assigned');
                          agentRoles.set(ticket.assigned_agent_id, roles);
                        }

                        // Contributors from history
                        history.forEach((entry) => {
                          if (entry.agent_id && !agentRoles.has(entry.agent_id)) {
                            agentRoles.set(entry.agent_id, ['Contributor']);
                          }
                        });

                        // Contributors from comments
                        comments.forEach((comment) => {
                          if (comment.agent_id && !agentRoles.has(comment.agent_id)) {
                            agentRoles.set(comment.agent_id, ['Contributor']);
                          }
                        });

                        return Array.from(agentRoles.entries()).map(([agentId, roles]) => (
                          <div key={agentId} className="pb-2 border-b last:border-b-0 last:pb-0">
                            <div className="text-gray-600 dark:text-gray-400 text-xs mb-1">
                              {roles.join(', ')}
                            </div>
                            <ClickableAgentId
                              agentId={agentId}
                              onClick={setSelectedAgentId}
                            />
                          </div>
                        ));
                      })()}
                    </div>
                  </div>

                  {/* Blocking Section */}
                  {ticket.blocked_by_ticket_ids && ticket.blocked_by_ticket_ids.length > 0 && (
                    <div className="bg-red-50 dark:bg-red-900/30 rounded-lg p-4 border border-red-200 dark:border-red-600">
                      <button
                        onClick={() => toggleSection('blocking')}
                        className="flex items-center justify-between w-full font-semibold text-gray-900 dark:text-gray-100 mb-3"
                      >
                        <span className="flex items-center">
                          <Lock className="w-4 h-4 mr-2 text-red-600" />
                          Blocked By
                        </span>
                        {expandedSections.blocking ? (
                          <ChevronUp className="w-4 h-4" />
                        ) : (
                          <ChevronDown className="w-4 h-4" />
                        )}
                      </button>
                      {expandedSections.blocking && (
                        <div className="space-y-2">
                          {ticket.blocked_by_ticket_ids.map((id) => (
                            <BlockedByTicketItem
                              key={id}
                              ticketId={id}
                              onClick={setSelectedBlockedTicketId}
                            />
                          ))}
                        </div>
                      )}
                    </div>
                  )}

                  {/* Blocks Section */}
                  {ticket.blocks_ticket_ids && ticket.blocks_ticket_ids.length > 0 && (
                    <div className="bg-orange-50 dark:bg-orange-900/30 rounded-lg p-4 border border-orange-200 dark:border-orange-600">
                      <button
                        onClick={() => toggleSection('blocks')}
                        className="flex items-center justify-between w-full font-semibold text-gray-900 dark:text-gray-100 mb-3"
                      >
                        <span className="flex items-center">
                          <AlertCircle className="w-4 h-4 mr-2 text-orange-600" />
                          Blocks
                        </span>
                        {expandedSections.blocks ? (
                          <ChevronUp className="w-4 h-4" />
                        ) : (
                          <ChevronDown className="w-4 h-4" />
                        )}
                      </button>
                      {expandedSections.blocks && (
                        <div className="space-y-2">
                          {ticket.blocks_ticket_ids.map((id) => (
                            <BlockedByTicketItem
                              key={id}
                              ticketId={id}
                              onClick={setSelectedBlockedTicketId}
                            />
                          ))}
                        </div>
                      )}
                    </div>
                  )}

                  {/* Commits Section */}
                  {commits.length > 0 && (
                    <div className="bg-gray-50 rounded-lg p-4 border">
                      <button
                        onClick={() => toggleSection('commits')}
                        className="flex items-center justify-between w-full font-semibold text-gray-900 dark:text-gray-100 mb-3"
                      >
                        <span className="flex items-center">
                          <GitCommit className="w-4 h-4 mr-2" />
                          Commits ({commits.length})
                        </span>
                        {expandedSections.commits ? (
                          <ChevronUp className="w-4 h-4" />
                        ) : (
                          <ChevronDown className="w-4 h-4" />
                        )}
                      </button>
                      {expandedSections.commits && (
                        <div className="space-y-2">
                          {commits.map((commit) => (
                            <div
                              key={commit.id}
                              className="p-3 bg-white dark:bg-gray-800 rounded border border-gray-200 dark:border-gray-700 cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-700 transition-colors"
                              onClick={() => setSelectedCommitSha(commit.commit_sha)}
                            >
                              <div className="flex items-center justify-between mb-1">
                                <span className="text-xs font-mono text-blue-600 dark:text-blue-400">
                                  {commit.commit_sha.substring(0, 7)}
                                </span>
                                <ExternalLink className="w-3 h-3 text-gray-400 dark:text-gray-500" />
                              </div>
                              <p className="text-sm text-gray-700 dark:text-gray-300 line-clamp-2">
                                {commit.commit_message}
                              </p>
                              <div className="flex items-center space-x-3 mt-2 text-xs text-gray-500 dark:text-gray-400">
                                <span>+{commit.insertions} -{commit.deletions}</span>
                                <span>{commit.files_changed} files</span>
                              </div>
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  )}

                  {/* Related Tickets */}
                  {ticket.related_ticket_ids && ticket.related_ticket_ids.length > 0 && (
                    <div className="bg-gray-50 dark:bg-gray-800/50 rounded-lg p-4 border border-gray-200 dark:border-gray-700">
                      <button
                        onClick={() => toggleSection('related')}
                        className="flex items-center justify-between w-full font-semibold text-gray-900 dark:text-gray-100 mb-3"
                      >
                        <span>Related Tickets</span>
                        {expandedSections.related ? (
                          <ChevronUp className="w-4 h-4" />
                        ) : (
                          <ChevronDown className="w-4 h-4" />
                        )}
                      </button>
                      {expandedSections.related && (
                        <div className="space-y-2">
                          {ticket.related_ticket_ids.map((id) => (
                            <div
                              key={id}
                              className="p-2 bg-white dark:bg-gray-800 rounded border border-gray-200 dark:border-gray-700 text-sm font-mono cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-700"
                            >
                              {id}
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  )}

                  {/* Tags */}
                  {ticket.tags && ticket.tags.length > 0 && (
                    <div className="bg-gray-50 dark:bg-gray-800/50 rounded-lg p-4 border border-gray-200 dark:border-gray-700">
                      <h3 className="font-semibold text-gray-900 dark:text-gray-100 mb-3 flex items-center">
                        <Tag className="w-4 h-4 mr-2" />
                        Tags
                      </h3>
                      <div className="flex flex-wrap gap-2">
                        {ticket.tags.map((tag, index) => (
                          <button
                            key={index}
                            onClick={() => {
                              if (onNavigateToSearchTab) {
                                onNavigateToSearchTab(tag);
                                onClose(); // Close the modal after navigating
                              }
                            }}
                            className={cn(
                              "px-2 py-1 bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-300 text-xs rounded-full transition-colors",
                              onNavigateToSearchTab && "cursor-pointer hover:bg-blue-200 dark:hover:bg-blue-800/50 hover:text-blue-800 dark:hover:text-blue-200"
                            )}
                            disabled={!onNavigateToSearchTab}
                            title={onNavigateToSearchTab ? `Search for tickets with tag: ${tag}` : undefined}
                          >
                            {tag}
                          </button>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              </div>
            </div>
          </motion.div>
        </motion.div>
      </AnimatePresence>

      {/* Git Diff Modal */}
      {selectedCommitSha && (
        <GitDiffModal
          commitSha={selectedCommitSha}
          onClose={() => setSelectedCommitSha(null)}
        />
      )}

      {/* Agent Detail Modal */}
      {selectedAgentId && (
        <AgentDetailModal
          agentId={selectedAgentId}
          onClose={() => setSelectedAgentId(null)}
        />
      )}

      {/* Recursive Ticket Detail Modal for Blocked By tickets */}
      {selectedBlockedTicketId && (
        <TicketDetailModal
          ticketId={selectedBlockedTicketId}
          onClose={() => setSelectedBlockedTicketId(null)}
          onNavigateToSearchTab={onNavigateToSearchTab}
        />
      )}

      {/* Task Detail Modal */}
      {selectedTaskId && (
        <TaskDetailModal
          taskId={selectedTaskId}
          onClose={() => setSelectedTaskId(null)}
        />
      )}
    </>
  );
};

export default TicketDetailModal;
