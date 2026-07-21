/**
 * Hephaestus Cost Tracker - Pi Extension
 * 
 * Hooks into pi turn_end events to capture LLM costs in real-time.
 * Posts costs to Hephaestus API and shows running cost in TUI.
 * 
 * Environment Variables:
 * - HEPHAESTUS_API_URL: Base URL for Hephaestus API (default: http://localhost:8000)
 */

// Pi extension API types (simplified)
interface PiContext {
  ui: {
    setStatus(message: string): void;
  };
  config: Record<string, any>;
}

interface TurnData {
  message: {
    usage?: {
      cost?: {
        total?: number;
      };
      input?: number;
      output?: number;
      cacheRead?: number;
      cacheWrite?: number;
      reasoning?: number;
    };
    model?: string;
  };
}

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

class HephaestusCostTracker {
  private sessionCost: number = 0;
  private apiUrl: string;
  private agentId?: string;
  private taskId?: string;
  private workflowId?: string;

  constructor() {
    this.apiUrl = process.env.HEPHAESTUS_API_URL || 'http://localhost:8000';
  }

  /**
   * Called when the extension is loaded by pi.
   * Sets up the turn_end hook.
   */
  async initialize(ctx: PiContext): Promise<void> {
    // Extract context from environment if available
    this.agentId = process.env.HEPHAESTUS_AGENT_ID;
    this.taskId = process.env.HEPHAESTUS_TASK_ID;
    this.workflowId = process.env.HEPHAESTUS_WORKFLOW_ID;

    ctx.ui.setStatus('💰 Cost tracker active');
  }

  /**
   * Called after each LLM turn completes.
   * Extracts cost and posts to Hephaestus API.
   */
  async turn_end(ctx: PiContext, turn: TurnData): Promise<void> {
    try {
      const usage = turn.message?.usage;
      if (!usage) return;

      const costUsd = usage.cost?.total || 0;
      if (costUsd <= 0) return;

      // Update running total
      this.sessionCost += costUsd;

      // Update TUI status
      ctx.ui.setStatus(`💰 $${this.sessionCost.toFixed(2)}`);

      // Build cost entry
      const entry: CostEntry = {
        task_id: this.taskId,
        agent_id: this.agentId,
        workflow_id: this.workflowId,
        source: 'pi',
        model: turn.message.model,
        input_tokens: usage.input,
        output_tokens: usage.output,
        cache_read_tokens: usage.cacheRead,
        cache_write_tokens: usage.cacheWrite,
        reasoning_tokens: usage.reasoning,
        cost_usd: costUsd,
        raw_usage: usage,
      };

      // Post to API (fire-and-forget)
      this.postCost(entry).catch((err) => {
        // Don't block the turn - just log
        console.warn(`[CostTracker] Failed to post cost: ${err.message}`);
      });
    } catch (err) {
      // Never block the turn on cost tracking errors
      console.warn(`[CostTracker] Error in turn_end: ${err}`);
    }
  }

  /**
   * Post cost entry to Hephaestus API.
   */
  private async postCost(entry: CostEntry): Promise<void> {
    const url = `${this.apiUrl}/cost-entries`;
    
    const response = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Agent-ID': this.agentId || 'pi-extension',
      },
      body: JSON.stringify(entry),
    });

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}: ${response.statusText}`);
    }
  }
}

// Export the extension instance
export default new HephaestusCostTracker();
