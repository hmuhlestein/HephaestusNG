/**
 * Hephaestus Cost Tracker - Pi Extension
 *
 * Hooks into pi turn_end events to capture LLM costs in real-time.
 * Posts costs to Hephaestus API and shows running cost in TUI.
 *
 * Environment Variables:
 * - HEPHAESTUS_API_URL: Base URL for Hephaestus API (default: http://localhost:8300)
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

interface CostEntry {
  task_id?: string;
  agent_id?: string;
  workflow_id?: string;
  source: string;
  model?: string;
  input_tokens?: number;
  output_tokens?: number;
  cache_read_tokens?: number;
  cache_write_tokens?: number;
  reasoning_tokens?: number;
  cost_usd: number;
  raw_usage?: Record<string, any>;
}

export default function (pi: ExtensionAPI) {
  const apiUrl = process.env.HEPHAESTUS_API_URL || 'http://localhost:8300';
  const agentId = process.env.HEPHAESTUS_AGENT_ID;
  const taskId = process.env.HEPHAESTUS_TASK_ID;
  const workflowId = process.env.HEPHAESTUS_WORKFLOW_ID;
  let sessionCost = 0;

  async function postCost(entry: CostEntry): Promise<void> {
    const url = `${apiUrl}/api/autopilot/cost-entries`;

    const response = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Agent-ID': agentId || 'pi-extension',
      },
      body: JSON.stringify(entry),
    });

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}: ${response.statusText}`);
    }
  }

  pi.on('turn_end', async (event, ctx) => {
    try {
      const message = event.message;
      if (message.role !== 'assistant') return;

      const usage = message.usage;
      if (!usage) return;

      const costUsd = usage.cost?.total || 0;
      if (costUsd <= 0) return;

      // Update running total
      sessionCost += costUsd;

      // Update TUI status
      ctx.ui.setStatus('cost-tracker', `💰 $${sessionCost.toFixed(2)}`);

      // Build cost entry
      const entry: CostEntry = {
        task_id: taskId,
        agent_id: agentId,
        workflow_id: workflowId,
        source: 'pi',
        model: message.model,
        input_tokens: usage.input,
        output_tokens: usage.output,
        cache_read_tokens: usage.cacheRead,
        cache_write_tokens: usage.cacheWrite,
        reasoning_tokens: usage.reasoning,
        cost_usd: costUsd,
        raw_usage: usage,
      };

      // Post to API (fire-and-forget)
      postCost(entry).catch((err) => {
        // Don't block the turn - just log
        console.warn(`[CostTracker] Failed to post cost: ${err.message}`);
      });
    } catch (err) {
      // Never block the turn on cost tracking errors
      console.warn(`[CostTracker] Error in turn_end: ${err}`);
    }
  });

  pi.on('session_start', async (_event, ctx) => {
    ctx.ui.setStatus('cost-tracker', '💰 Cost tracker active');
  });
}
