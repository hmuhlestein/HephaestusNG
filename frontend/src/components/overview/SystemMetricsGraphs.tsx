import React, { useState } from 'react';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Legend, Area, AreaChart } from 'recharts';
import { TrendingUp, Activity, Users, Filter } from 'lucide-react';
import { formatDistanceToNow, format } from 'date-fns';
import { useTheme } from '@/context/ThemeContext';


interface MetricsDataPoint {
  timestamp: string;
  coherence_score?: number;
  avg_alignment?: number;
  active_agents?: number;
  phase?: string;
}

interface SystemMetricsGraphsProps {
  metricsHistory: MetricsDataPoint[];
  phases?: string[];
}

export default function SystemMetricsGraphs({ metricsHistory, phases = [] }: SystemMetricsGraphsProps) {
  const [selectedPhase, setSelectedPhase] = useState<string | null>(null);
  const [timeRange, setTimeRange] = useState<'1h' | '6h' | '24h' | 'all'>('6h');
  const { theme } = useTheme();
  const isDark = theme === 'dark';
  const gridColor = isDark ? '#374151' : '#e5e7eb';
  const axisColor = isDark ? '#6b7280' : '#9ca3af';
  const tickStyle = { fontSize: 11, fill: isDark ? '#9ca3af' : '#374151' };

  // Filter data based on selected phase and time range
  const filteredData = React.useMemo(() => {
    let data = [...metricsHistory];

    // Filter by phase if selected
    if (selectedPhase) {
      data = data.filter(d => d.phase === selectedPhase);
    }

    // Filter by time range
    const now = Date.now();
    const timeRanges = {
      '1h': 60 * 60 * 1000,
      '6h': 6 * 60 * 60 * 1000,
      '24h': 24 * 60 * 60 * 1000,
      'all': Infinity
    };

    if (timeRange !== 'all') {
      const cutoff = now - timeRanges[timeRange];
      data = data.filter(d => new Date(d.timestamp).getTime() > cutoff);
    }

    return data.map(point => ({
      ...point,
      time: new Date(point.timestamp).getTime(),
      displayTime: format(new Date(point.timestamp), 'HH:mm'),
      coherencePercent: Math.round((point.coherence_score || 0) * 100),
      alignmentPercent: Math.round((point.avg_alignment || 0) * 100),
      agentCount: point.active_agents || 0
    }));
  }, [metricsHistory, selectedPhase, timeRange]);

  // Custom tooltip
  const CustomTooltip = ({ active, payload }: any) => {
    if (!active || !payload || !payload[0]) return null;

    const data = payload[0].payload;

    return (
      <div className="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg shadow-lg p-3">
        <div className="text-sm font-medium text-gray-900 dark:text-gray-100">
          {formatDistanceToNow(new Date(data.timestamp), { addSuffix: true })}
        </div>
        {data.phase && (
          <div className="text-xs text-gray-500 dark:text-gray-400 mt-1">Phase: {data.phase}</div>
        )}
        <div className="space-y-1 mt-2">
          {payload.map((entry: any) => (
            <div key={entry.dataKey} className="text-sm flex items-center">
              <div
                className="w-3 h-3 rounded mr-2"
                style={{ backgroundColor: entry.color }}
              />
              <span className="text-gray-600 dark:text-gray-400">
                {entry.name}: <span className="font-semibold text-gray-900 dark:text-gray-100">{entry.value}{entry.dataKey.includes('Percent') ? '%' : ''}</span>
              </span>
            </div>
          ))}
        </div>
      </div>
    );
  };

  return (
    <div className="space-y-4">
      {/* Controls */}
      <div className="flex items-center justify-between">
        <div className="flex items-center space-x-2">
          <Filter className="w-4 h-4 text-gray-500 dark:text-gray-400" />
          <div className="flex space-x-1">
            {['1h', '6h', '24h', 'all'].map((range) => (
              <button
                key={range}
                onClick={() => setTimeRange(range as any)}
                className={`px-2 py-1 text-xs rounded ${
                  timeRange === range
                    ? 'bg-blue-500 text-white'
                    : 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600'
                }`}
              >
                {range === 'all' ? 'All' : range}
              </button>
            ))}
          </div>
        </div>

        {phases.length > 0 && (
          <div className="flex items-center space-x-2">
            <span className="text-xs text-gray-500 dark:text-gray-400">Phase:</span>
            <select
              value={selectedPhase || ''}
              onChange={(e) => setSelectedPhase(e.target.value || null)}
              className="text-xs border border-gray-300 dark:border-gray-600 rounded px-2 py-1 bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100"
            >
              <option value="">All Phases</option>
              {phases.map(phase => (
                <option key={phase} value={phase}>{phase}</option>
              ))}
            </select>
          </div>
        )}
      </div>

      {/* Coherence & Alignment Graph */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center text-base">
            <TrendingUp className="w-4 h-4 mr-2 text-blue-600 dark:text-blue-400" />
            System Coherence & Alignment
          </CardTitle>
          <CardDescription className="text-xs">
            Tracking system-wide coherence and average agent alignment over time
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={filteredData} margin={{ top: 5, right: 30, left: 20, bottom: 5 }}>
                <CartesianGrid strokeDasharray="3 3" stroke={gridColor} />
                <XAxis
                  dataKey="displayTime"
                  tick={tickStyle}
                  axisLine={{ stroke: axisColor }}
                />
                <YAxis
                  domain={[0, 100]}
                  ticks={[0, 25, 50, 75, 100]}
                  tick={tickStyle}
                  axisLine={{ stroke: axisColor }}
                  label={{ value: 'Percentage', angle: -90, position: 'insideLeft', style: { fontSize: 11, fill: tickStyle.fill } }}
                />
                <Tooltip content={<CustomTooltip />} />
                <Legend
                  wrapperStyle={{ fontSize: '12px' }}
                  iconType="line"
                />
                <Line
                  type="monotone"
                  dataKey="coherencePercent"
                  name="Coherence"
                  stroke="#3b82f6"
                  strokeWidth={2}
                  dot={false}
                  activeDot={{ r: 4 }}
                />
                <Line
                  type="monotone"
                  dataKey="alignmentPercent"
                  name="Avg Alignment"
                  stroke="#10b981"
                  strokeWidth={2}
                  dot={false}
                  activeDot={{ r: 4 }}
                />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </CardContent>
      </Card>

      {/* Active Agents Graph */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center text-base">
            <Users className="w-4 h-4 mr-2 text-green-600 dark:text-green-400" />
            Active Agents Over Time
          </CardTitle>
          <CardDescription className="text-xs">
            Number of active agents in the system
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="h-48">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={filteredData} margin={{ top: 5, right: 30, left: 20, bottom: 5 }}>
                <CartesianGrid strokeDasharray="3 3" stroke={gridColor} />
                <XAxis
                  dataKey="displayTime"
                  tick={tickStyle}
                  axisLine={{ stroke: axisColor }}
                />
                <YAxis
                  tick={tickStyle}
                  axisLine={{ stroke: axisColor }}
                  label={{ value: 'Agent Count', angle: -90, position: 'insideLeft', style: { fontSize: 11, fill: tickStyle.fill } }}
                />
                <Tooltip content={<CustomTooltip />} />
                <Area
                  type="monotone"
                  dataKey="agentCount"
                  name="Active Agents"
                  stroke="#10b981"
                  fill="#10b981"
                  fillOpacity={0.3}
                  strokeWidth={2}
                />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        </CardContent>
      </Card>

      {/* Summary Stats */}
      {filteredData.length > 0 && (
        <div className="grid grid-cols-3 gap-4">
          <Card className="p-4">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-xs text-gray-500 dark:text-gray-400">Current Coherence</p>
                <p className="text-2xl font-bold text-blue-600 dark:text-blue-400">
                  {filteredData[filteredData.length - 1].coherencePercent}%
                </p>
              </div>
              <Activity className="w-8 h-8 text-blue-200 dark:text-blue-800" />
            </div>
          </Card>

          <Card className="p-4">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-xs text-gray-500 dark:text-gray-400">Current Alignment</p>
                <p className="text-2xl font-bold text-green-600 dark:text-green-400">
                  {filteredData[filteredData.length - 1].alignmentPercent}%
                </p>
              </div>
              <TrendingUp className="w-8 h-8 text-green-200 dark:text-green-800" />
            </div>
          </Card>

          <Card className="p-4">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-xs text-gray-500 dark:text-gray-400">Active Agents</p>
                <p className="text-2xl font-bold text-purple-600 dark:text-purple-400">
                  {filteredData[filteredData.length - 1].agentCount}
                </p>
              </div>
              <Users className="w-8 h-8 text-purple-200 dark:text-purple-800" />
            </div>
          </Card>
        </div>
      )}
    </div>
  );
}