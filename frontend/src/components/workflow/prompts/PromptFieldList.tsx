import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { Plus, X, GripVertical } from 'lucide-react';

interface PromptFieldListProps {
  items: string[];
  onChange: (items: string[]) => void;
  disabled?: boolean;
}

export default function PromptFieldList({ items, onChange, disabled }: PromptFieldListProps) {
  const [newItem, setNewItem] = useState('');

  const handleAdd = () => {
    if (newItem.trim()) {
      onChange([...items, newItem.trim()]);
      setNewItem('');
    }
  };

  const handleRemove = (index: number) => {
    onChange(items.filter((_, i) => i !== index));
  };

  const handleUpdate = (index: number, value: string) => {
    const updated = [...items];
    updated[index] = value;
    onChange(updated);
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      handleAdd();
    }
  };

  return (
    <div className="space-y-1.5">
      {items.map((item, index) => (
        <div key={index} className="flex items-center gap-1.5 group">
          <GripVertical className="w-3 h-3 text-gray-300 opacity-0 group-hover:opacity-100 flex-shrink-0" />
          <span className="text-xs text-gray-400 w-4 text-right flex-shrink-0">{index + 1}.</span>
          <input
            type="text"
            value={item}
            onChange={(e) => handleUpdate(index, e.target.value)}
            disabled={disabled}
            className="flex-1 text-sm border border-gray-200 rounded px-2 py-1 focus:outline-none focus:ring-1 focus:ring-blue-200 disabled:opacity-50"
          />
          {!disabled && (
            <Button
              variant="ghost"
              size="sm"
              className="h-6 w-6 p-0 opacity-0 group-hover:opacity-100"
              onClick={() => handleRemove(index)}
            >
              <X className="w-3 h-3 text-red-400" />
            </Button>
          )}
        </div>
      ))}

      {/* Add new item */}
      {!disabled && (
        <div className="flex items-center gap-1.5">
          <input
            type="text"
            value={newItem}
            onChange={(e) => setNewItem(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Add criterion..."
            className="flex-1 text-sm border border-dashed border-gray-300 rounded px-2 py-1 focus:outline-none focus:ring-1 focus:ring-blue-200 placeholder:text-gray-400"
          />
          <Button
            variant="ghost"
            size="sm"
            className="h-6 w-6 p-0"
            onClick={handleAdd}
            disabled={!newItem.trim()}
          >
            <Plus className="w-3 h-3 text-blue-500" />
          </Button>
        </div>
      )}
    </div>
  );
}
