import React, { useRef, useEffect, useState, useCallback, useMemo } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import {
  X,
  Copy,
  Maximize2,
  Minimize2,
  Search,
  RefreshCw,
  Pause,
  Play,
  AlertCircle,
  Wifi,
  WifiOff,
  Clock,
  MessageCircle,
  Send,
  Check,
  XCircle
} from 'lucide-react';
import { formatDistanceToNow } from 'date-fns';
import { useRealTimeAgentOutput } from '@/hooks/useRealTimeAgentOutput';
import { Agent } from '@/types';
import { apiService } from '@/services/api';
import { isNoiseLine, stripAnsiCodes } from '@/lib/agentOutput';
import StatusBadge from './StatusBadge';
import AnsiToHtml from 'ansi-to-html';

interface RealTimeAgentOutputProps {
  agent: Agent | null;
  onClose: () => void;
  isFullscreen?: boolean;
  // The caller's own already-known phase name (e.g. the task row's
  // task.phase_name), preferred over agent.current_task?.phase_info?.name
  // in the title -- that field comes from a separately, freshly-polled
  // fetch of the agent and goes null whenever agent.current_task_id is
  // transiently unset (e.g. between task handoffs on a reused CLI
  // session), silently falling back to the generic agent_type ("phase")
  // + id and losing the actual phase name from the title bar.
  fallbackPhaseName?: string;
}

const RealTimeAgentOutput: React.FC<RealTimeAgentOutputProps> = ({
  agent,
  onClose,
  isFullscreen: initialFullscreen = false,
  fallbackPhaseName,
}) => {
  const [isFullscreen, setIsFullscreen] = useState(initialFullscreen);
  const [searchTerm, setSearchTerm] = useState('');
  const [isPaused, setIsPaused] = useState(false);
  const [isSelectionPaused, setIsSelectionPaused] = useState(false);
  const [autoScroll, setAutoScroll] = useState(true);
  const [currentStatus, setCurrentStatus] = useState(agent?.status || 'working');

  // Poll agent status every 3 seconds
  useEffect(() => {
    if (!agent?.id) return;
    const interval = setInterval(async () => {
      try {
        const updated = await apiService.getAgent(agent.id);
        if (updated) setCurrentStatus(updated.status);
      } catch {}
    }, 3000);
    return () => clearInterval(interval);
  }, [agent?.id]);

  // Update status when agent prop changes
  useEffect(() => {
    if (agent?.status) setCurrentStatus(agent.status);
  }, [agent?.status]);

  // Message input state
  const [messageText, setMessageText] = useState('');
  const [isSending, setIsSending] = useState(false);
  const [sendStatus, setSendStatus] = useState<'idle' | 'success' | 'error'>('idle');
  const [sendErrorMessage, setSendErrorMessage] = useState('');

  const outputRef = useRef<HTMLPreElement>(null);
  const messageInputRef = useRef<HTMLTextAreaElement>(null);
  const searchInputRef = useRef<HTMLInputElement>(null);
  const lastScrollPosition = useRef(0);

  // ANSI to HTML converter
  const ansiConverter = useMemo(() => new AnsiToHtml({
    fg: '#d4d4d4',
    bg: '#1e1e1e',
    newline: true,
    escapeXML: true,
    stream: false,
    colors: {
      0: '#1e1e1e',   // Black matches background
      1: '#cd3131',
      2: '#0dbc79',
      3: '#e5e510',
      4: '#2472c8',
      5: '#bc3fbc',
      6: '#11a8cd',
      7: '#e5e5e5',
      8: '#666666',
      9: '#f14c4c',
      10: '#23d18b',
      11: '#f5f543',
      12: '#3b8eea',
      13: '#d670d6',
      14: '#29b8db',
      15: '#ffffff',
    },
  }), []);

  const {
    output,
    isLoading,
    error,
    isConnected,
    lastUpdateTime,
    retry,
    setPauseUpdates,
  } = useRealTimeAgentOutput(agent?.id || null, {
    enabled: !isPaused && !!agent,
    updateInterval: 1000,
    // This is the full single-agent detail/fullscreen viewer, where
    // scrollback matters most -- unlike Observability's multi-agent grid
    // (useMultiAgentOutput, which keeps the 2000-line default since it
    // polls several agents at once), so it requests much more history.
    // The backend reads+filters the whole transcript file once per
    // change (cached by mtime/size) and only tails it to `lines`
    // afterward, so this doesn't add backend read/filter cost -- only
    // more text over the wire and more DOM content.
    lines: 20000,
  });

  // Auto-scroll to bottom when new content arrives -- and, when the user has
  // scrolled away from the bottom to read earlier output, explicitly pin
  // scrollTop back to where they left it (lastScrollPosition, updated by
  // handleScroll below) on every poll instead. dangerouslySetInnerHTML
  // replaces the pane's entire content on every update (new output arrives
  // every ~1s from a live agent); nothing was restoring scroll position in
  // the !autoScroll case, so the browser's own post-replace scroll behavior
  // -- not consistently "leave scrollTop alone" -- effectively dragged the
  // view back toward the bottom, making it impossible to read scrollback
  // while an agent was still actively producing output. Only reachable
  // once the agent finished and updates stopped arriving.
  useEffect(() => {
    if (!outputRef.current || !lastUpdateTime) return;
    // Use requestAnimationFrame to ensure DOM has updated before scrolling
    requestAnimationFrame(() => {
      if (!outputRef.current) return;
      if (autoScroll) {
        outputRef.current.scrollTop = outputRef.current.scrollHeight;
      } else {
        outputRef.current.scrollTop = lastScrollPosition.current;
      }
    });
  }, [output, autoScroll, lastUpdateTime]);

  // Handle scroll events to determine if user is at bottom
  const handleScroll = useCallback(() => {
    if (outputRef.current) {
      const element = outputRef.current;
      const isNearBottom = element.scrollTop + element.clientHeight >= element.scrollHeight - 50;
      setAutoScroll(isNearBottom);
      lastScrollPosition.current = element.scrollTop;
    }
  }, []);

  // Handle text selection to pause updates
  const handleMouseDown = useCallback(() => {
    setIsSelectionPaused(true);
    setPauseUpdates(true);
  }, [setPauseUpdates]);

  const handleMouseUp = useCallback(() => {
    // Check if text is actually selected - if so, keep paused
    const sel = window.getSelection();
    if (sel && sel.toString().length > 0) {
      // Text is selected, stay paused
      return;
    }
    // No text selected, resume updates
    setIsSelectionPaused(false);
    setPauseUpdates(false);
  }, [setPauseUpdates]);

  // Safety net for handleMouseUp: that handler only re-checks the selection
  // once, right at mouseup, and only for mouseups inside this pane. Select
  // text and just release the mouse to read/copy it (never clicking again
  // with an empty selection) left updates paused indefinitely -- silently,
  // since this pause has no visual indicator of its own -- so the viewer
  // looked frozen at whatever it showed at that moment while the agent kept
  // working, until the whole component remounted (e.g. closing and
  // reopening after the agent finished) and a fresh fetch caught it up all
  // at once. Losing the selection ANY way (click elsewhere on the page,
  // arrow keys, Escape, programmatic clear) now resumes updates promptly.
  useEffect(() => {
    const handleSelectionChange = () => {
      const sel = window.getSelection();
      if (!sel || sel.toString().length === 0) {
        setIsSelectionPaused(false);
        setPauseUpdates(false);
      }
    };
    document.addEventListener('selectionchange', handleSelectionChange);
    return () => document.removeEventListener('selectionchange', handleSelectionChange);
  }, [setPauseUpdates]);

  // Copy to clipboard functionality
  const copyToClipboard = async () => {
    try {
      const textToCopy = searchTerm ?
        output.split('\n').filter(line =>
          line.toLowerCase().includes(searchTerm.toLowerCase())
        ).join('\n') :
        output;

      await navigator.clipboard.writeText(textToCopy);
      // Could add toast notification here
    } catch (err) {
      console.error('Failed to copy to clipboard:', err);
    }
  };

  // Send message to agent
  const handleSendMessage = async () => {
    if (!messageText.trim() || !agent || isSending) return;

    if (currentStatus === 'terminated') {
      setSendStatus('error');
      setSendErrorMessage('Cannot send message to terminated agent');
      setTimeout(() => {
        setSendStatus('idle');
        setSendErrorMessage('');
      }, 3000);
      return;
    }

    setIsSending(true);
    setSendStatus('idle');

    try {
      const response = await apiService.sendMessage(messageText, agent.id);

      if (response.success) {
        setSendStatus('success');
        setMessageText('');

        // Reset status after 2 seconds
        setTimeout(() => {
          setSendStatus('idle');
        }, 2000);
      } else {
        setSendStatus('error');
        setSendErrorMessage(response.message || 'Failed to send message');
        setTimeout(() => {
          setSendStatus('idle');
          setSendErrorMessage('');
        }, 3000);
      }
    } catch (error: any) {
      setSendStatus('error');
      setSendErrorMessage(error.response?.data?.detail || 'Failed to send message');
      setTimeout(() => {
        setSendStatus('idle');
        setSendErrorMessage('');
      }, 3000);
    } finally {
      setIsSending(false);
    }
  };

  // Collapse carriage returns (\r) first, then filter
  const processedOutput = useMemo(() => {
    if (!output) return '';
    // Expand tabs to 4-space stops (tmux uses 8, but 4 is more readable
    // in a web UI) before any other processing.
    const expanded = output.replace(/\t/g, '    ');
    const lines = expanded.split('\n');
    const collapsed: string[] = [];
    for (const line of lines) {
      if (line.includes('\r')) {
        // Keep only the last segment after the last \r
        const last = line.split('\r').pop() || '';
        collapsed.push(last);
      } else {
        collapsed.push(line);
      }
    }
    return collapsed.join('\n');
  }, [output]);

  // Filter output based on search and remove separator/spinner lines,
  // but keep the last line if it's a Thinking/Working spinner.
  const filteredOutput = useMemo(() => {
    if (!processedOutput) return '';
    const lines = processedOutput.split('\n');
    const filtered: string[] = [];
    // Find the last Thinking/Working line index for spinner display
    let lastSpinnerIdx = -1;
    for (let i = lines.length - 1; i >= 0; i--) {
      const s = stripAnsiCodes(lines[i]).trim();
      if (/^(Thinking|Working)\.\.?\.?$/.test(s)) {
        lastSpinnerIdx = i;
        break;
      }
    }
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      const stripped = stripAnsiCodes(line).trim();
      if (isNoiseLine(stripped)) {
        // Keep only the last Thinking/Working spinner
        if (i === lastSpinnerIdx) {
          filtered.push(line);
        }
        continue;
      }
      if (searchTerm && !line.toLowerCase().includes(searchTerm.toLowerCase())) continue;
      filtered.push(line);
    }
    return filtered.join('\n');
  }, [processedOutput, searchTerm]);

  // Convert ANSI codes to HTML
  const htmlOutput = useMemo(() => {
    const text = filteredOutput || '';
    if (!text) return '';
    try {
      return ansiConverter.toHtml(text);
    } catch {
      return text;
    }
  }, [filteredOutput, ansiConverter]);

  // Keyboard shortcuts
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      // Don't interfere with message input
      const isMessageInputFocused = messageInputRef.current?.matches(':focus');

      if (e.key === 'Escape') {
        if (isMessageInputFocused) {
          messageInputRef.current?.blur();
        } else if (searchTerm) {
          setSearchTerm('');
          searchInputRef.current?.blur();
        } else {
          onClose();
        }
      } else if (e.ctrlKey || e.metaKey) {
        if (e.key === 'c' && !window.getSelection()?.toString() && !isMessageInputFocused) {
          e.preventDefault();
          copyToClipboard();
        } else if (e.key === 'f' && !isMessageInputFocused) {
          e.preventDefault();
          searchInputRef.current?.focus();
        } else if (e.key === 'r' && !isMessageInputFocused) {
          e.preventDefault();
          retry();
        }
      } else if (e.key === ' ' && !searchInputRef.current?.matches(':focus') && !isMessageInputFocused) {
        e.preventDefault();
        setIsPaused(!isPaused);
      }
    };

    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [onClose, copyToClipboard, retry, isPaused, searchTerm]);

  if (!agent) return null;

  const modalClass = isFullscreen
    ? 'fixed inset-0 z-50 bg-gray-900'
    : 'fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center p-4 z-50';

  const contentClass = isFullscreen
    ? 'w-full h-full flex flex-col'
    : 'bg-white dark:bg-gray-900 rounded-lg shadow-xl w-full max-w-[80vw] h-[90vh] flex flex-col';

  return (
    <AnimatePresence>
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        className={modalClass}
        onClick={isFullscreen ? undefined : onClose}
      >
        <motion.div
          initial={{ scale: isFullscreen ? 1 : 0.9 }}
          animate={{ scale: 1 }}
          className={contentClass}
          onClick={(e) => e.stopPropagation()}
        >
          {/* Header */}
          <div className="px-6 py-4 border-b border-gray-200 dark:border-gray-700 flex items-center justify-between bg-white dark:bg-gray-900 rounded-t-lg">
            <div className="flex items-center space-x-3">
              <div className="flex items-center space-x-2">
                <div className={`w-2 h-2 rounded-full ${
                  isConnected ? 'bg-green-500 animate-pulse' : 'bg-red-500'
                }`} />
                <h3 className="text-lg font-semibold text-gray-800 dark:text-white">
                  {agent.current_task?.phase_info?.name || fallbackPhaseName || agent.agent_type || 'Agent'} {agent.id.substring(0, 8)} - Output
                </h3>
                {isConnected ? (
                  <Wifi className="w-4 h-4 text-green-500" />
                ) : (
                  <WifiOff className="w-4 h-4 text-red-500" />
                )}

                <StatusBadge status={currentStatus} size="sm" />
              </div>

              {lastUpdateTime && (
                <div className="flex items-center text-sm text-gray-500 dark:text-gray-400">
                  <Clock className="w-3 h-3 mr-1" />
                  Updated {formatDistanceToNow(lastUpdateTime, { addSuffix: true })}
                </div>
              )}
            </div>

            <div className="flex items-center space-x-2">
              {/* Search */}
              <div className="relative">
                <Search className="absolute left-2 top-1/2 transform -translate-y-1/2 w-4 h-4 text-gray-400" />
                <input
                  ref={searchInputRef}
                  type="text"
                  value={searchTerm}
                  onChange={(e) => setSearchTerm(e.target.value)}
                  placeholder="Search output..."
                  className="pl-8 pr-3 py-1 text-sm border border-gray-300 rounded-md focus:ring-2 focus:ring-blue-500 focus:border-blue-500 dark:bg-gray-700 dark:border-gray-600 dark:text-white"
                />
              </div>

              {/* Controls */}
              <button
                onClick={() => setIsPaused(!isPaused)}
                className={`p-2 rounded-lg transition-colors ${
                  isPaused
                    ? 'bg-green-100 text-green-700 hover:bg-green-200 dark:bg-green-800 dark:text-green-200'
                    : 'bg-yellow-100 text-yellow-700 hover:bg-yellow-200 dark:bg-yellow-800 dark:text-yellow-200'
                }`}
                title={isPaused ? 'Resume updates (Space)' : 'Pause updates (Space)'}
              >
                {isPaused ? <Play className="w-4 h-4" /> : <Pause className="w-4 h-4" />}
              </button>

              {error && (
                <button
                  onClick={retry}
                  className="p-2 rounded-lg bg-red-100 text-red-700 hover:bg-red-200 dark:bg-red-800 dark:text-red-200 transition-colors"
                  title="Retry connection (Ctrl+R)"
                >
                  <RefreshCw className="w-4 h-4" />
                </button>
              )}

              <button
                onClick={copyToClipboard}
                className="p-2 rounded-lg bg-gray-100 text-gray-700 hover:bg-gray-200 dark:bg-gray-700 dark:text-gray-300 transition-colors"
                title="Copy output (Ctrl+C)"
              >
                <Copy className="w-4 h-4" />
              </button>

              <button
                onClick={() => setIsFullscreen(!isFullscreen)}
                className="p-2 rounded-lg bg-gray-100 text-gray-700 hover:bg-gray-200 dark:bg-gray-700 dark:text-gray-300 transition-colors"
                title="Toggle fullscreen"
              >
                {isFullscreen ? <Minimize2 className="w-4 h-4" /> : <Maximize2 className="w-4 h-4" />}
              </button>

              {!isFullscreen && (
                <button
                  onClick={onClose}
                  className="p-2 rounded-lg text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200 transition-colors"
                  title="Close (Escape)"
                >
                  <X className="w-4 h-4" />
                </button>
              )}
            </div>
          </div>

          {/* Error state */}
          {error && (
            <div className="px-6 py-3 bg-red-50 dark:bg-red-900/20 border-b border-red-200 dark:border-red-800">
              <div className="flex items-center space-x-2 text-red-700 dark:text-red-400">
                <AlertCircle className="w-4 h-4" />
                <span className="text-sm">{error}</span>
                <button
                  onClick={retry}
                  className="text-sm underline hover:no-underline"
                >
                  Retry
                </button>
              </div>
            </div>
          )}

          {/* Output content */}
          <div className="flex-1 min-h-0 relative bg-gray-50 dark:bg-gray-800">
            {isLoading && !output && (
              <div className="flex items-center justify-center h-32">
                <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-500"></div>
              </div>
            )}

            <pre
              ref={outputRef}
              onScroll={handleScroll}
              onMouseDown={handleMouseDown}
              onMouseUp={handleMouseUp}
              className="absolute inset-0 p-6 overflow-auto text-xs bg-[#1e1e1e] text-[#d4d4d4] whitespace-pre-wrap break-all selection:bg-blue-500 selection:text-white ansi-output"
              style={{
                fontFamily: 'Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace'
              }}
              dangerouslySetInnerHTML={{ __html: htmlOutput || (output ? 'No matching lines found' : 'No output available yet...') }}
            />

            {/* Scroll indicator */}
            {!autoScroll && (
              <motion.div
                initial={{ opacity: 0, y: 20 }}
                animate={{ opacity: 1, y: 0 }}
                className="absolute bottom-4 right-4 bg-blue-500 text-white px-3 py-1 rounded-full text-xs cursor-pointer hover:bg-blue-600 transition-colors"
                onClick={() => {
                  setAutoScroll(true);
                  if (outputRef.current) {
                    outputRef.current.scrollTop = outputRef.current.scrollHeight;
                  }
                }}
              >
                Scroll to bottom
              </motion.div>
            )}
          </div>

          {/* Message Input */}
          {currentStatus !== 'terminated' && (
            <div className="px-6 py-3 border-t border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900">
              <div className="flex items-start space-x-3">
                <MessageCircle className="w-5 h-5 text-gray-400 mt-2 flex-shrink-0" />
                <div className="flex-1">
                  <textarea
                    ref={messageInputRef}
                    value={messageText}
                    onChange={(e) => setMessageText(e.target.value)}
                    onKeyDown={(e) => {
                      if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
                        e.preventDefault();
                        handleSendMessage();
                      }
                      // Don't interfere with other keyboard shortcuts
                      e.stopPropagation();
                    }}
                    placeholder="Send a message to this agent..."
                    rows={1}
                    disabled={isSending}
                    className="w-full px-3 py-2 text-sm border border-gray-300 dark:border-gray-600 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent resize-none dark:bg-gray-800 dark:text-white disabled:opacity-50 disabled:cursor-not-allowed"
                    style={{
                      minHeight: '40px',
                      maxHeight: '120px',
                      height: 'auto',
                      overflowY: messageText.split('\n').length > 3 ? 'auto' : 'hidden'
                    }}
                    onInput={(e) => {
                      const target = e.target as HTMLTextAreaElement;
                      target.style.height = 'auto';
                      target.style.height = Math.min(target.scrollHeight, 120) + 'px';
                    }}
                  />
                  {sendStatus !== 'idle' && (
                    <motion.div
                      initial={{ opacity: 0, y: -5 }}
                      animate={{ opacity: 1, y: 0 }}
                      className="mt-2"
                    >
                      {sendStatus === 'success' ? (
                        <div className="flex items-center text-xs text-green-600 dark:text-green-400">
                          <Check className="w-3 h-3 mr-1" />
                          Message sent successfully
                        </div>
                      ) : (
                        <div className="flex items-center text-xs text-red-600 dark:text-red-400">
                          <XCircle className="w-3 h-3 mr-1" />
                          {sendErrorMessage || 'Failed to send message'}
                        </div>
                      )}
                    </motion.div>
                  )}
                </div>
                <button
                  onClick={handleSendMessage}
                  disabled={isSending || !messageText.trim()}
                  className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors flex items-center space-x-2 flex-shrink-0"
                  title="Send message (Ctrl/Cmd + Enter)"
                >
                  {isSending ? (
                    <RefreshCw className="w-4 h-4 animate-spin" />
                  ) : (
                    <>
                      <Send className="w-4 h-4" />
                      <span className="text-sm">Send</span>
                    </>
                  )}
                </button>
              </div>
            </div>
          )}

          {/* Footer with stats */}
          <div className="px-6 py-2 border-t border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800 text-xs text-gray-500 dark:text-gray-400 flex justify-between items-center rounded-b-lg">
            <div className="flex space-x-4">
              <span>Lines: {output.split('\n').length}</span>
              <span>Characters: {output.length}</span>
              {searchTerm && (
                <span>Filtered: {filteredOutput.split('\n').length} lines</span>
              )}
            </div>

            <div className="flex items-center space-x-2">
              <span>Auto-scroll: {autoScroll ? 'ON' : 'OFF'}</span>
              {isPaused && <span className="text-yellow-500">• PAUSED</span>}
              {!isPaused && isSelectionPaused && (
                <span className="text-yellow-500" title="Updates paused while text is selected -- click elsewhere to resume">
                  • PAUSED (text selected)
                </span>
              )}
            </div>
          </div>
        </motion.div>
      </motion.div>

      <style>{`
        .ansi-output {
          line-height: 1.2 !important;
          padding: 0 !important;
          margin: 0 !important;
        }
      `}</style>
    </AnimatePresence>
  );
};

export default RealTimeAgentOutput;