import React, { useEffect, useState, useCallback, useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import ReactFlow, {
  Node,
  Edge,
  Controls,
  Background,
  BackgroundVariant,
  useNodesState,
  useEdgesState,
  Position,
  Handle,
  MiniMap,
  Panel,
  MarkerType,
} from 'reactflow';
import dagre from 'dagre';
import { GitBranch, FileText, RefreshCw, Play, Pause, CheckCircle, Clock, AlertCircle, Circle } from 'lucide-react';
import { apiService } from '@/services/api';
import { GraphNode, GraphEdge } from '@/types';
import { useWebSocket } from '@/context/WebSocketContext';
import { useWorkflow } from '@/context/WorkflowContext';
import ExecutionSelector from '@/components/ExecutionSelector';
import StatusBadge from '@/components/StatusBadge';
import TaskDetailModal from '@/components/TaskDetailModal';
import { useTheme } from '@/context/ThemeContext';
import 'reactflow/dist/style.css';

// Layout direction type
type LayoutDirection = 'TB' | 'LR';

// Status to border color mapping
const statusBorderColors: Record<string, string> = {
  done: 'border-green-500',
  completed: 'border-green-500',
  in_progress: 'border-blue-500',
  working: 'border-blue-500',
  failed: 'border-red-500',
  pending: 'border-gray-400',
  queued: 'border-gray-400',
  assigned: 'border-yellow-500',
  blocked: 'border-orange-500',
};

// Phase background colors
const phaseBackgroundColors: Record<number, string> = {
  1: 'bg-green-50 dark:bg-green-900/20',
  2: 'bg-blue-50 dark:bg-blue-900/20',
  3: 'bg-yellow-50 dark:bg-yellow-900/20',
  4: 'bg-pink-50 dark:bg-pink-900/20',
  5: 'bg-indigo-50 dark:bg-indigo-900/20',
};

// Custom node component for tasks
const TaskNode: React.FC<{ data: any }> = ({ data }) => {
  const isHighlighted = data.isHighlighted;
  const isDimmed = data.isDimmed;

  const formatTime = (timestamp: string) => {
    if (!timestamp) return '';
    const date = new Date(timestamp);
    return date.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' });
  };

  const bgClass = phaseBackgroundColors[data.phase_order] || 'bg-gray-50 dark:bg-gray-800';
  const borderClass = statusBorderColors[data.status] || 'border-gray-400';
  const highlightClasses = isHighlighted ? 'ring-4 ring-red-400 ring-opacity-75 shadow-2xl scale-105' : '';
  const dimClasses = isDimmed ? 'opacity-30' : '';

  // Status icon
  const StatusIcon = () => {
    switch (data.status) {
      case 'done':
      case 'completed':
        return <CheckCircle className="w-3 h-3 text-green-600 dark:text-green-400" />;
      case 'in_progress':
      case 'working':
        return <Clock className="w-3 h-3 text-blue-600 dark:text-blue-400" />;
      case 'failed':
        return <AlertCircle className="w-3 h-3 text-red-600 dark:text-red-400" />;
      default:
        return <Circle className="w-3 h-3 text-gray-400 dark:text-gray-500" />;
    }
  };

  return (
    <div className={`${bgClass} ${borderClass} ${highlightClasses} ${dimClasses} border-3 rounded-lg p-3 min-w-[200px] max-w-[280px] shadow-md relative transition-all duration-300`}
         style={{ borderWidth: '3px' }}>
      <Handle
        type="target"
        position={Position.Top}
        style={{ background: '#F59E0B', width: 10, height: 10 }}
      />
      <Handle
        type="source"
        position={Position.Bottom}
        style={{ background: '#F59E0B', width: 10, height: 10 }}
      />

      {/* Header with phase badge and status icon */}
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-1">
          <FileText className="w-4 h-4 text-gray-600 dark:text-gray-400" />
          <span className="text-xs font-semibold text-gray-700 dark:text-gray-300">Task</span>
        </div>
        <div className="flex items-center gap-2">
          <StatusIcon />
          {data.phase_order && (
            <span className="text-xs px-1.5 py-0.5 bg-white dark:bg-gray-900 bg-opacity-80 dark:bg-opacity-80 text-gray-700 dark:text-gray-300 rounded font-bold shadow-sm">
              P{data.phase_order}
            </span>
          )}
        </div>
      </div>

      {/* Description */}
      <p className="text-xs text-gray-700 dark:text-gray-300 line-clamp-2 mb-2">
        {data.description?.substring(0, 80)}{data.description?.length > 80 ? '...' : ''}
      </p>

      {/* Footer with time and status badge */}
      <div className="flex items-center justify-between">
        {data.created_at && (
          <p className="text-xs text-gray-500 dark:text-gray-400">{formatTime(data.created_at)}</p>
        )}
        <StatusBadge status={data.status} size="sm" />
      </div>
    </div>
  );
};

const nodeTypes = {
  task: TaskNode,
};

// Dagre layout function
const getLayoutedElements = (
  nodes: Node[],
  edges: Edge[],
  direction: LayoutDirection = 'TB'
): Node[] => {
  if (nodes.length === 0) return [];

  const dagreGraph = new dagre.graphlib.Graph();
  dagreGraph.setDefaultEdgeLabel(() => ({}));

  const nodeWidth = 280;
  const nodeHeight = 120;

  dagreGraph.setGraph({
    rankdir: direction,
    nodesep: 80,
    ranksep: 100,
    marginx: 50,
    marginy: 50,
  });

  nodes.forEach((node) => {
    dagreGraph.setNode(node.id, { width: nodeWidth, height: nodeHeight });
  });

  edges.forEach((edge) => {
    dagreGraph.setEdge(edge.source, edge.target);
  });

  dagre.layout(dagreGraph);

  return nodes.map((node) => {
    const nodeWithPosition = dagreGraph.node(node.id);
    return {
      ...node,
      position: {
        x: nodeWithPosition.x - nodeWidth / 2,
        y: nodeWithPosition.y - nodeHeight / 2,
      },
      sourcePosition: direction === 'TB' ? Position.Bottom : Position.Right,
      targetPosition: direction === 'TB' ? Position.Top : Position.Left,
    };
  });
};

const Graph: React.FC = () => {
  const [nodes, setNodes, onNodesChange] = useNodesState([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState([]);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [layoutDirection, setLayoutDirection] = useState<LayoutDirection>('TB');
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [refreshInterval, setRefreshInterval] = useState(15);
  const { subscribe } = useWebSocket();
  const { selectedExecution } = useWorkflow();
  const { theme } = useTheme();
  const isDark = theme === 'dark';

  const workflowId = selectedExecution?.id;

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['graph', workflowId],
    queryFn: () => apiService.getGraphData(workflowId),
    refetchInterval: autoRefresh ? refreshInterval * 1000 : false,
    enabled: !!workflowId,
  });

  // Function to find all connected nodes in the chain
  const findConnectedChain = useCallback((nodeId: string, graphEdges: Edge[]): { nodes: Set<string>, edges: Set<string> } => {
    const visitedNodes = new Set<string>();
    const connectedEdges = new Set<string>();
    const queue = [nodeId];

    while (queue.length > 0) {
      const currentNode = queue.shift()!;
      if (visitedNodes.has(currentNode)) continue;
      visitedNodes.add(currentNode);

      graphEdges.forEach(edge => {
        if (edge.source === currentNode || edge.target === currentNode) {
          connectedEdges.add(edge.id);
          const otherNode = edge.source === currentNode ? edge.target : edge.source;
          if (!visitedNodes.has(otherNode)) {
            queue.push(otherNode);
          }
        }
      });
    }

    return { nodes: visitedNodes, edges: connectedEdges };
  }, []);

  // Process and layout data. SOLID review 5.5: this used to also depend on
  // highlightedNodes/highlightedEdges/hoveredNode, so hovering a node re-ran
  // the full filter/phase-join/dagre-layout pipeline on every mouseenter --
  // a real O(n log n) re-layout per hover on a large graph, for what's
  // purely a styling overlay. Highlight/dim state is applied directly by
  // onNodeMouseEnter/onNodeMouseLeave below instead, patching the
  // already-laid-out nodes/edges in place -- dagre only reruns when the
  // underlying data or layout direction actually changes.
  useEffect(() => {
    if (!data) return;

    // Filter to only task nodes
    const taskNodes = data.nodes.filter((n: GraphNode) => n.type === 'task');

    // Filter to only subtask edges (task-to-task relationships)
    const subtaskEdges = data.edges.filter((e: GraphEdge) => e.type === 'subtask');

    // Add phase info to task nodes
    const phases = data.phases || {};
    const nodesWithPhaseInfo = taskNodes.map((node: GraphNode) => {
      if (node.data.phase_id && phases[node.data.phase_id]) {
        const phase = phases[node.data.phase_id];
        return {
          ...node,
          data: {
            ...node.data,
            phase_name: phase.name,
            phase_order: phase.order,
          }
        };
      }
      return node;
    });

    // Convert to React Flow nodes
    const flowNodes: Node[] = nodesWithPhaseInfo.map((node: GraphNode) => ({
      id: node.id,
      type: 'task',
      position: { x: 0, y: 0 }, // Will be set by layout
      data: {
        ...node.data,
        isHighlighted: false,
        isDimmed: false,
      },
    }));

    // Convert to React Flow edges
    const flowEdges: Edge[] = subtaskEdges.map((edge: GraphEdge) => ({
      id: edge.id,
      source: edge.source,
      target: edge.target,
      type: 'smoothstep',
      animated: false,
      markerEnd: {
        type: MarkerType.ArrowClosed,
        width: 16,
        height: 16,
      },
      style: {
        stroke: '#F59E0B',
        strokeWidth: 2,
        opacity: 1,
      },
      label: 'spawned',
      labelStyle: {
        fontSize: 10,
        fill: isDark ? '#d1d5db' : '#6B7280',
      },
      labelBgStyle: {
        fill: isDark ? '#1f2937' : '#ffffff',
        fillOpacity: 0.8,
      },
    }));

    // Apply Dagre layout
    const layoutedNodes = getLayoutedElements(flowNodes, flowEdges, layoutDirection);

    setNodes(layoutedNodes);
    setEdges(flowEdges);
  }, [data, layoutDirection, setNodes, setEdges, isDark]);

  // Subscribe to WebSocket updates
  useEffect(() => {
    const unsubscribeTask = subscribe('task_created', () => {
      refetch();
    });

    return () => {
      unsubscribeTask();
    };
  }, [subscribe, refetch]);

  const onNodeClick = useCallback((_event: React.MouseEvent, node: Node) => {
    setSelectedTaskId(node.data.id);
  }, []);

  const onNodeMouseEnter = useCallback((_event: React.MouseEvent, node: Node) => {
    const chain = findConnectedChain(node.id, edges);
    setNodes((nds) => nds.map((n) => ({
      ...n,
      data: {
        ...n.data,
        isHighlighted: chain.nodes.has(n.id),
        isDimmed: !chain.nodes.has(n.id),
      },
    })));
    setEdges((eds) => eds.map((e) => {
      const isHighlighted = chain.edges.has(e.id);
      return {
        ...e,
        animated: isHighlighted,
        style: {
          ...e.style,
          stroke: isHighlighted ? '#FF6B6B' : '#F59E0B',
          strokeWidth: isHighlighted ? 4 : 2,
          opacity: isHighlighted ? 1 : 0.3,
        },
      };
    }));
  }, [edges, findConnectedChain, setNodes, setEdges]);

  const onNodeMouseLeave = useCallback(() => {
    setNodes((nds) => nds.map((n) => ({
      ...n,
      data: { ...n.data, isHighlighted: false, isDimmed: false },
    })));
    setEdges((eds) => eds.map((e) => ({
      ...e,
      animated: false,
      style: { ...e.style, stroke: '#F59E0B', strokeWidth: 2, opacity: 1 },
    })));
  }, [setNodes, setEdges]);

  // Calculate stats
  const stats = useMemo(() => {
    if (!data) return { total: 0, done: 0, inProgress: 0, pending: 0, failed: 0 };

    const tasks = data.nodes.filter((n: GraphNode) => n.type === 'task');
    return {
      total: tasks.length,
      done: tasks.filter((t: GraphNode) => t.data.status === 'done' || t.data.status === 'completed').length,
      inProgress: tasks.filter((t: GraphNode) => t.data.status === 'in_progress' || t.data.status === 'working').length,
      pending: tasks.filter((t: GraphNode) => t.data.status === 'pending' || t.data.status === 'queued').length,
      failed: tasks.filter((t: GraphNode) => t.data.status === 'failed').length,
    };
  }, [data]);

  if (!workflowId) {
    return (
      <div className="h-full flex flex-col">
        <div className="mb-4">
          <h1 className="text-3xl font-bold text-gray-800 dark:text-gray-100">Task Hierarchy</h1>
          <p className="text-gray-600 dark:text-gray-400 mt-1">Task spawning relationships</p>
        </div>
        <div className="flex-1 flex items-center justify-center bg-gray-50 dark:bg-gray-800 rounded-lg border-2 border-dashed border-gray-300 dark:border-gray-600">
          <div className="text-center">
            <GitBranch className="w-16 h-16 text-gray-400 dark:text-gray-500 mx-auto mb-4" />
            <p className="text-gray-500 dark:text-gray-400 text-lg mb-2">No workflow selected</p>
            <p className="text-gray-400 dark:text-gray-500 text-sm">Select a workflow from the header to view its task hierarchy</p>
          </div>
        </div>
      </div>
    );
  }

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-full">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-primary"></div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg p-6">
        <p className="text-red-600 dark:text-red-400">Failed to load graph data</p>
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col">
      {/* Header */}
      <div className="mb-4">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-3xl font-bold text-gray-800 dark:text-gray-100">Task Hierarchy</h1>
            <p className="text-gray-600 dark:text-gray-400 mt-1">
              {selectedExecution ? (
                <>Viewing: {selectedExecution.definition_name || selectedExecution.description?.split('\n')[0]}</>
              ) : (
                'Task spawning relationships'
              )}
            </p>
          </div>

          {/* Controls */}
          <div className="flex items-center gap-3">
            <ExecutionSelector />
            <div className="flex items-center gap-2 text-sm">
              <span className="text-gray-600 dark:text-gray-400">Auto-refresh:</span>
              <select
                value={refreshInterval}
                onChange={(e) => setRefreshInterval(Number(e.target.value))}
                className="px-2 py-1 border border-gray-300 dark:border-gray-600 rounded text-sm bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100"
                disabled={!autoRefresh}
              >
                <option value={5}>5s</option>
                <option value={10}>10s</option>
                <option value={15}>15s</option>
                <option value={30}>30s</option>
                <option value={60}>60s</option>
              </select>

              <button
                onClick={() => setAutoRefresh(!autoRefresh)}
                className={`p-2 rounded-lg transition-colors flex items-center ${
                  autoRefresh
                    ? 'bg-green-100 dark:bg-green-900/40 text-green-700 dark:text-green-300 hover:bg-green-200 dark:hover:bg-green-900/60'
                    : 'bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600'
                }`}
                title={autoRefresh ? 'Pause auto-refresh' : 'Resume auto-refresh'}
              >
                {autoRefresh ? <Pause className="w-4 h-4" /> : <Play className="w-4 h-4" />}
              </button>
            </div>

            <button
              onClick={() => refetch()}
              className="px-4 py-2 bg-primary text-white rounded-lg hover:bg-blue-600 transition-colors flex items-center"
            >
              <RefreshCw className="w-4 h-4 mr-2" />
              Refresh
            </button>
          </div>
        </div>
      </div>

      {/* Graph */}
      <div className="bg-white dark:bg-gray-800 rounded-lg shadow-md h-[700px] w-full relative">
        <ReactFlow
          nodes={nodes}
          edges={edges}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onNodeClick={onNodeClick}
          onNodeMouseEnter={onNodeMouseEnter}
          onNodeMouseLeave={onNodeMouseLeave}
          nodeTypes={nodeTypes}
          fitView
          fitViewOptions={{ padding: 0.2, maxZoom: 1 }}
          minZoom={0.1}
          maxZoom={2}
        >
          <Background variant={BackgroundVariant.Dots} gap={20} size={1} color={isDark ? '#4b5563' : '#d1d5db'} />
          <Controls />
          <MiniMap
            nodeColor={(node) => {
              const status = node.data?.status;
              if (status === 'done' || status === 'completed') return '#22c55e';
              if (status === 'in_progress' || status === 'working') return '#3b82f6';
              if (status === 'failed') return '#ef4444';
              return '#9ca3af';
            }}
            maskColor={isDark ? 'rgba(0, 0, 0, 0.5)' : 'rgba(0, 0, 0, 0.1)'}
            className="bg-white dark:bg-gray-800 border dark:border-gray-600 rounded"
            pannable
            zoomable
          />

          {/* Layout Control Panel */}
          <Panel position="top-right" className="bg-white dark:bg-gray-800 p-4 rounded-lg shadow-lg space-y-3">
            <div className="font-semibold text-sm text-gray-900 dark:text-gray-100">Layout</div>
            <div className="flex space-x-2">
              <button
                onClick={() => setLayoutDirection('TB')}
                className={`px-3 py-1.5 text-xs rounded font-medium ${
                  layoutDirection === 'TB'
                    ? 'bg-blue-600 text-white'
                    : 'bg-gray-200 dark:bg-gray-700 text-gray-700 dark:text-gray-300 hover:bg-gray-300 dark:hover:bg-gray-600'
                }`}
              >
                Top-Down
              </button>
              <button
                onClick={() => setLayoutDirection('LR')}
                className={`px-3 py-1.5 text-xs rounded font-medium ${
                  layoutDirection === 'LR'
                    ? 'bg-blue-600 text-white'
                    : 'bg-gray-200 dark:bg-gray-700 text-gray-700 dark:text-gray-300 hover:bg-gray-300 dark:hover:bg-gray-600'
                }`}
              >
                Left-Right
              </button>
            </div>
          </Panel>

          {/* Legend Panel */}
          <Panel position="bottom-right" className="bg-white dark:bg-gray-800 p-4 rounded-lg shadow-lg">
            <div className="font-semibold text-sm text-gray-900 dark:text-gray-100 mb-3">Legend</div>

            {/* Status (borders) */}
            <div className="space-y-2 text-xs mb-3">
              <div className="text-gray-600 dark:text-gray-400 font-medium mb-1">Status (border)</div>
              <div className="flex items-center space-x-2">
                <div className="w-4 h-4 border-3 border-green-500 rounded" style={{ borderWidth: '3px' }}></div>
                <span className="text-gray-700 dark:text-gray-300">Done ({stats.done})</span>
              </div>
              <div className="flex items-center space-x-2">
                <div className="w-4 h-4 border-3 border-blue-500 rounded" style={{ borderWidth: '3px' }}></div>
                <span className="text-gray-700 dark:text-gray-300">In Progress ({stats.inProgress})</span>
              </div>
              <div className="flex items-center space-x-2">
                <div className="w-4 h-4 border-3 border-red-500 rounded" style={{ borderWidth: '3px' }}></div>
                <span className="text-gray-700 dark:text-gray-300">Failed ({stats.failed})</span>
              </div>
              <div className="flex items-center space-x-2">
                <div className="w-4 h-4 border-3 border-gray-400 rounded" style={{ borderWidth: '3px' }}></div>
                <span className="text-gray-700 dark:text-gray-300">Pending ({stats.pending})</span>
              </div>
            </div>

            {/* Phase (backgrounds) */}
            <div className="space-y-2 text-xs border-t dark:border-gray-700 pt-3">
              <div className="text-gray-600 dark:text-gray-400 font-medium mb-1">Phase (background)</div>
              <div className="flex items-center space-x-2">
                <div className="w-4 h-4 bg-green-100 dark:bg-green-900/40 rounded border border-gray-300 dark:border-gray-600"></div>
                <span className="text-gray-700 dark:text-gray-300">Phase 1</span>
              </div>
              <div className="flex items-center space-x-2">
                <div className="w-4 h-4 bg-blue-100 dark:bg-blue-900/40 rounded border border-gray-300 dark:border-gray-600"></div>
                <span className="text-gray-700 dark:text-gray-300">Phase 2</span>
              </div>
              <div className="flex items-center space-x-2">
                <div className="w-4 h-4 bg-yellow-100 dark:bg-yellow-900/40 rounded border border-gray-300 dark:border-gray-600"></div>
                <span className="text-gray-700 dark:text-gray-300">Phase 3</span>
              </div>
            </div>

            {/* Stats */}
            <div className="mt-3 pt-3 border-t dark:border-gray-700 text-xs text-gray-500 dark:text-gray-400">
              <div className="flex items-center gap-1">
                <GitBranch className="w-3 h-3" />
                <span>{stats.total} tasks, {edges.length} connections</span>
              </div>
              <div className="mt-1">Click node for details</div>
              <div>Drag to pan, scroll to zoom</div>
            </div>
          </Panel>
        </ReactFlow>
      </div>

      {/* Task Detail Modal */}
      <TaskDetailModal
        taskId={selectedTaskId}
        onClose={() => setSelectedTaskId(null)}
        onNavigateToTask={(taskId) => setSelectedTaskId(taskId)}
        onNavigateToGraph={(_taskId) => {
          setSelectedTaskId(null);
        }}
      />
    </div>
  );
};

export default Graph;
