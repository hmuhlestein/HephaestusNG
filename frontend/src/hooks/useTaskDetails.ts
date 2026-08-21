import { useEffect } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';

import { apiService } from '@/services/api';
import { Task, TaskFullDetails, TicketDetail } from '@/types';
import { useWebSocket } from '@/context/WebSocketContext';

/**
 * Bundle every data dependency TaskDetailModal needs for one task: the task
 * itself, its agent's guardian analyses and steering interventions, its
 * related tickets, and (when it's a duplicate) the original task -- plus the
 * WebSocket subscription that invalidates the two agent-scoped queries.
 *
 * SOLID review 5.1: these were 5 separate useQuery calls and a raw subscribe
 * effect inlined in the component, interleaved with its rendering concerns.
 * Moved verbatim -- same query keys, same enabled/refetchInterval settings,
 * same invalidation behavior.
 */
export function useTaskDetails(taskId: string | null) {
  const queryClient = useQueryClient();
  const { subscribe } = useWebSocket();

  const {
    data: taskDetails,
    isLoading,
    error,
  } = useQuery<TaskFullDetails | null>({
    queryKey: ['task-full-details', taskId],
    queryFn: () => (taskId ? apiService.getTaskFullDetails(taskId) : null),
    enabled: !!taskId,
    refetchInterval: 5000, // Refresh every 5 seconds for runtime updates
  });

  // Fetch guardian analyses for the task's agent
  const { data: guardianAnalyses } = useQuery({
    queryKey: ['guardian-analyses', taskDetails?.agent_info?.id],
    queryFn: () =>
      taskDetails?.agent_info?.id
        ? apiService.getGuardianAnalyses(taskDetails.agent_info.id)
        : null,
    enabled: !!taskDetails?.agent_info?.id,
    refetchInterval: 10000, // Refresh every 10 seconds
  });

  // Fetch steering interventions for the task's agent
  const { data: steeringInterventions } = useQuery({
    queryKey: ['steering-interventions', taskDetails?.agent_info?.id],
    queryFn: () =>
      taskDetails?.agent_info?.id
        ? apiService.getSteeringInterventions(taskDetails.agent_info.id)
        : null,
    enabled: !!taskDetails?.agent_info?.id,
    refetchInterval: 10000, // Refresh every 10 seconds
  });

  // Fetch related tickets
  const { data: relatedTickets, isLoading: relatedTicketsLoading } = useQuery<
    TicketDetail[]
  >({
    queryKey: ['related-tickets', taskDetails?.related_ticket_ids],
    queryFn: async () => {
      if (
        !taskDetails?.related_ticket_ids ||
        taskDetails.related_ticket_ids.length === 0
      ) {
        return [];
      }

      // Fetch each ticket's details
      const ticketPromises = taskDetails.related_ticket_ids.map(
        async (ticketId) => {
          try {
            const result = await apiService.getTicket(ticketId);
            return result.ticket;
          } catch (error) {
            console.error(`Failed to fetch ticket ${ticketId}:`, error);
            return null;
          }
        }
      );

      const tickets = await Promise.all(ticketPromises);
      return tickets.filter(
        (ticket): ticket is TicketDetail => ticket !== null
      );
    },
    enabled:
      !!taskDetails?.related_ticket_ids &&
      taskDetails.related_ticket_ids.length > 0,
  });

  // Fetch original task if this is a duplicate
  const { data: originalTask } = useQuery<Task>({
    queryKey: ['task', taskDetails?.duplicate_of_task_id],
    queryFn: async () => {
      if (!taskDetails?.duplicate_of_task_id) {
        throw new Error('No duplicate ID');
      }
      return apiService.getTaskById(taskDetails.duplicate_of_task_id);
    },
    enabled:
      !!taskDetails?.duplicate_of_task_id &&
      taskDetails?.status === 'duplicated',
  });

  // Subscribe to WebSocket events for real-time updates
  useEffect(() => {
    if (!taskDetails?.agent_info?.id) return;

    const agentId = taskDetails.agent_info.id;

    const unsubscribeGuardian = subscribe('guardian_analysis', (data: any) => {
      // Check if the analysis is for this task's agent
      if (data.agent_id === agentId) {
        queryClient.invalidateQueries({
          queryKey: ['guardian-analyses', agentId],
        });
      }
    });

    const unsubscribeSteering = subscribe(
      'steering_intervention',
      (data: any) => {
        // Check if the intervention is for this task's agent
        if (data.agent_id === agentId) {
          queryClient.invalidateQueries({
            queryKey: ['steering-interventions', agentId],
          });
        }
      }
    );

    return () => {
      unsubscribeGuardian();
      unsubscribeSteering();
    };
  }, [taskDetails?.agent_info?.id, subscribe, queryClient]);

  return {
    taskDetails,
    isLoading,
    error,
    guardianAnalyses,
    steeringInterventions,
    relatedTickets,
    relatedTicketsLoading,
    originalTask,
  };
}
