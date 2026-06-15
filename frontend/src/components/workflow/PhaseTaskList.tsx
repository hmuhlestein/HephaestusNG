import { ScrollArea } from '@/components/ui/scroll-area';
import TaskRow from './TaskRow';
import { ListTodo } from 'lucide-react';

interface PhaseTaskListProps {
  tasks: any[];
  onTerminateAgent?: (agentId: string) => void;
}

export default function PhaseTaskList({ tasks, onTerminateAgent }: PhaseTaskListProps) {
  if (!tasks || tasks.length === 0) {
    return (
      <div className="text-center py-6 text-gray-500 text-sm">
        <ListTodo className="w-6 h-6 mx-auto mb-2 text-gray-300" />
        No tasks in this phase yet.
      </div>
    );
  }

  return (
    <ScrollArea className="max-h-[300px]">
      <div className="space-y-2">
        {tasks.map((task) => (
          <TaskRow
            key={task.id}
            task={task}
            onTerminateAgent={onTerminateAgent}
          />
        ))}
      </div>
    </ScrollArea>
  );
}
