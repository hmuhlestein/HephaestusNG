import React, { useState } from 'react';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { MessageSquare, Loader2, Check, AlertCircle } from 'lucide-react';
import { apiService } from '@/services/api';

interface BroadcastMessageDialogProps {
  open: boolean;
  onClose: () => void;
  activeAgentCount: number;
}

export default function BroadcastMessageDialog({
  open,
  onClose,
  activeAgentCount,
}: BroadcastMessageDialogProps) {
  const [message, setMessage] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [status, setStatus] = useState<'idle' | 'success' | 'error'>('idle');
  const [statusMessage, setStatusMessage] = useState('');

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();

    if (!message.trim()) {
      setStatus('error');
      setStatusMessage('Message cannot be empty');
      return;
    }

    if (activeAgentCount === 0) {
      setStatus('error');
      setStatusMessage('No active agents to broadcast to');
      return;
    }

    setIsLoading(true);
    setStatus('idle');

    try {
      const response = await apiService.broadcastMessage(message);

      if (response.success) {
        setStatus('success');
        setStatusMessage(`Message broadcast to ${response.recipient_count} agent(s)`);

        // Reset form and close after brief delay
        setTimeout(() => {
          setMessage('');
          setStatus('idle');
          setStatusMessage('');
          onClose();
        }, 1500);
      } else {
        setStatus('error');
        setStatusMessage(response.message || 'Failed to broadcast message');
      }
    } catch (error: any) {
      setStatus('error');
      setStatusMessage(error.response?.data?.detail || 'Failed to broadcast message');
    } finally {
      setIsLoading(false);
    }
  };

  const handleClose = () => {
    if (!isLoading) {
      setMessage('');
      setStatus('idle');
      setStatusMessage('');
      onClose();
    }
  };

  return (
    <Dialog open={open} onOpenChange={handleClose}>
      <DialogContent className="sm:max-w-[500px]">
        <DialogHeader>
          <DialogTitle className="flex items-center">
            <MessageSquare className="w-5 h-5 mr-2 text-blue-600 dark:text-blue-400" />
            Broadcast Message to All Agents
          </DialogTitle>
          <DialogDescription>
            Send a message to all active agents in the system
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit}>
          <div className="space-y-4 py-4">
            {/* Active Agent Count */}
            <div className="bg-blue-50 dark:bg-blue-900/20 border border-blue-200 dark:border-blue-800 rounded-lg p-3">
              <p className="text-sm text-blue-800 dark:text-blue-300">
                This message will be sent to{' '}
                <span className="font-semibold">{activeAgentCount}</span> active agent(s)
              </p>
            </div>

            {/* Message Input */}
            <div className="space-y-2">
              <label htmlFor="message" className="text-sm font-medium text-gray-700 dark:text-gray-300">
                Message
              </label>
              <textarea
                id="message"
                value={message}
                onChange={(e) => setMessage(e.target.value)}
                onKeyDown={(e) => {
                  // Allow all keys to work normally in textarea
                  e.stopPropagation();
                }}
                placeholder="Type your broadcast message here..."
                rows={6}
                className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent resize-none"
                disabled={isLoading}
              />
              <p className="text-xs text-gray-500 dark:text-gray-400">
                {message.length} characters
              </p>
            </div>

            {/* Status Message */}
            {status !== 'idle' && (
              <div
                className={`flex items-center p-3 rounded-lg ${
                  status === 'success'
                    ? 'bg-green-50 dark:bg-green-900/20 border border-green-200 dark:border-green-800 text-green-800 dark:text-green-300'
                    : 'bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 text-red-800 dark:text-red-300'
                }`}
              >
                {status === 'success' ? (
                  <Check className="w-4 h-4 mr-2 flex-shrink-0" />
                ) : (
                  <AlertCircle className="w-4 h-4 mr-2 flex-shrink-0" />
                )}
                <p className="text-sm">{statusMessage}</p>
              </div>
            )}
          </div>

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={handleClose}
              disabled={isLoading}
            >
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={isLoading || !message.trim() || activeAgentCount === 0}
              className="bg-blue-600 hover:bg-blue-700"
            >
              {isLoading ? (
                <>
                  <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                  Broadcasting...
                </>
              ) : (
                <>
                  <MessageSquare className="w-4 h-4 mr-2" />
                  Broadcast Message
                </>
              )}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}