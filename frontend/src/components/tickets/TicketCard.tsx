import React from 'react';
import { Bug, Lightbulb, Wrench, CheckSquare, Beaker, FileText, Lock, CheckCircle, MessageCircle, GitCommit, User, Clock } from 'lucide-react';
import { TicketDetail } from '@/types';
import { cn } from '@/lib/utils';
import { Tooltip } from '@/components/ui/tooltip';

interface TicketCardProps {
  ticket: TicketDetail;
  onClick: () => void;
  onDragStart?: (e: React.DragEvent) => void;
  onDragEnd?: (e: React.DragEvent) => void;
  draggable?: boolean;
  onTagClick?: (tag: string) => void;
}

const getTicketTypeIcon = (type: string) => {
  switch (type) {
    case 'bug':
      return <Bug className="w-4 h-4 text-red-600 dark:text-red-400" />;
    case 'feature':
      return <Lightbulb className="w-4 h-4 text-yellow-600 dark:text-yellow-400" />;
    case 'improvement':
      return <Wrench className="w-4 h-4 text-blue-600 dark:text-blue-400" />;
    case 'task':
      return <CheckSquare className="w-4 h-4 text-green-600 dark:text-green-400" />;
    case 'spike':
      return <Beaker className="w-4 h-4 text-purple-600 dark:text-purple-400" />;
    case 'documentation':
      return <FileText className="w-4 h-4 text-gray-600 dark:text-gray-400" />;
    default:
      return <CheckSquare className="w-4 h-4 text-gray-600 dark:text-gray-400" />;
  }
};

const getPriorityColor = (priority: string) => {
  switch (priority) {
    case 'critical':
      return 'bg-red-100 dark:bg-red-900/40 text-red-800 dark:text-red-300 border-red-200 dark:border-red-600';
    case 'high':
      return 'bg-orange-100 dark:bg-orange-900/40 text-orange-800 dark:text-orange-300 border-orange-200 dark:border-orange-600';
    case 'medium':
      return 'bg-yellow-100 dark:bg-yellow-900/40 text-yellow-800 dark:text-yellow-300 border-yellow-200 dark:border-yellow-600';
    case 'low':
      return 'bg-gray-100 dark:bg-gray-700 text-gray-800 dark:text-gray-300 border-gray-200 dark:border-gray-600';
    default:
      return 'bg-gray-100 dark:bg-gray-700 text-gray-800 dark:text-gray-300 border-gray-200 dark:border-gray-600';
  }
};

const formatTooltipContent = (ticket: TicketDetail): string => {
  const descriptionPreview = ticket.description.length > 100
    ? `${ticket.description.substring(0, 100)}...`
    : ticket.description;

  return `${ticket.title}\n\n${descriptionPreview}`;
};

const TicketCard: React.FC<TicketCardProps> = ({
  ticket,
  onClick,
  onDragStart,
  onDragEnd,
  draggable = true,
  onTagClick,
}) => {
  const isPendingReview = ticket.approval_status === 'pending_review';

  return (
    <Tooltip content={formatTooltipContent(ticket)}>
      <div
        className={cn(
          'bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-3 mb-2 shadow-sm hover:shadow-md transition-all cursor-pointer group',
          isPendingReview && 'border-l-4 border-l-orange-500 bg-orange-50 dark:bg-orange-900/30 ring-2 ring-orange-200 dark:ring-orange-500',
          ticket.is_blocked && 'border-l-4 border-l-red-500 bg-red-50 dark:bg-red-900/30',
          ticket.is_resolved && 'bg-green-50 dark:bg-green-900/30 border-l-4 border-l-green-500'
        )}
        onClick={onClick}
        draggable={draggable && !ticket.is_blocked}
        onDragStart={onDragStart}
        onDragEnd={onDragEnd}
      >
      {/* Header */}
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center space-x-2">
          <span className="text-xs font-mono text-gray-500 dark:text-gray-400">
            {ticket.id.split('-')[1]?.substring(0, 8) || ticket.id.substring(0, 8)}
          </span>
          {getTicketTypeIcon(ticket.ticket_type)}
        </div>
        <div className="flex items-center space-x-1">
          {isPendingReview && (
            <div className="p-1 bg-orange-100 dark:bg-orange-800/50 rounded animate-pulse" title="Pending Human Review">
              <Clock className="w-3 h-3 text-orange-600 dark:text-orange-400" />
            </div>
          )}
          {ticket.is_blocked && (
            <div className="p-1 bg-red-100 dark:bg-red-800/50 rounded" title="Blocked">
              <Lock className="w-3 h-3 text-red-600 dark:text-red-400" />
            </div>
          )}
          {ticket.is_resolved && (
            <div className="p-1 bg-green-100 dark:bg-green-800/50 rounded" title="Resolved">
              <CheckCircle className="w-3 h-3 text-green-600 dark:text-green-400" />
            </div>
          )}
        </div>
      </div>

      {/* Pending Review Banner */}
      {isPendingReview && (
        <div className="mb-2 px-2 py-1 bg-orange-100 dark:bg-orange-900/40 border border-orange-300 dark:border-orange-600 rounded text-xs font-semibold text-orange-800 dark:text-orange-300 text-center">
          ⏳ Needs Human Review
        </div>
      )}

      {/* Title */}
      <h3 className="text-sm font-medium text-gray-900 dark:text-gray-100 mb-2 line-clamp-2 group-hover:text-blue-600 dark:group-hover:text-blue-400 transition-colors">
        {ticket.title}
      </h3>

      {/* Tags */}
      {ticket.tags && ticket.tags.length > 0 && (
        <div className="flex flex-wrap gap-1 mb-2">
          {ticket.tags.slice(0, 3).map((tag, index) => (
            <span
              key={index}
              onClick={(e) => {
                if (onTagClick) {
                  e.stopPropagation();
                  onTagClick(tag);
                }
              }}
              className={cn(
                'text-xs px-2 py-0.5 bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300 rounded-full transition-all',
                onTagClick &&
                  'cursor-pointer hover:bg-blue-100 dark:hover:bg-blue-900/40 hover:text-blue-700 dark:hover:text-blue-300 hover:ring-1 hover:ring-blue-400'
              )}
              title={onTagClick ? `Filter by tag: ${tag}` : tag}
            >
              {tag}
            </span>
          ))}
          {ticket.tags.length > 3 && (
            <span className="text-xs px-2 py-0.5 bg-gray-100 dark:bg-gray-700 text-gray-500 dark:text-gray-400 rounded-full">
              +{ticket.tags.length - 3}
            </span>
          )}
        </div>
      )}

      {/* Footer */}
      <div className="flex items-center justify-between mt-2 pt-2 border-t border-gray-100 dark:border-gray-700">
        {/* Priority */}
        <span
          className={cn(
            'text-xs px-2 py-0.5 rounded border font-medium',
            getPriorityColor(ticket.priority)
          )}
        >
          {ticket.priority}
        </span>

        {/* Agent & Metadata */}
        <div className="flex items-center space-x-3 text-xs text-gray-500 dark:text-gray-400">
          {ticket.assigned_agent_id && (
            <div className="flex items-center" title={ticket.assigned_agent_id}>
              <User className="w-3 h-3 mr-1" />
              <span className="max-w-[60px] truncate">
                {ticket.assigned_agent_id.split('-')[0]}
              </span>
            </div>
          )}
          {ticket.comment_count > 0 && (
            <div className="flex items-center" title={`${ticket.comment_count} comments`}>
              <MessageCircle className="w-3 h-3 mr-1" />
              <span>{ticket.comment_count}</span>
            </div>
          )}
          {ticket.commit_count > 0 && (
            <div className="flex items-center" title={`${ticket.commit_count} commits`}>
              <GitCommit className="w-3 h-3 mr-1" />
              <span>{ticket.commit_count}</span>
            </div>
          )}
        </div>
      </div>
      </div>
    </Tooltip>
  );
};

export default TicketCard;
