import { useState, useCallback } from 'react';

/**
 * Manage a set of independently-toggled disclosure (expand/collapse) sections.
 *
 * SOLID review 5.1: TaskDetailModal and tickets/TicketDetailModal each had
 * their own copy of the same `useState({...}) + toggleSection` pair, keyed
 * by different section names. Same shape, duplicated with variation.
 *
 * Pass the initial open/closed state keyed by section name; `expanded` has
 * the same shape and `toggle` flips one key, exactly as the inlined
 * versions did.
 */
export function useDisclosure<T extends Record<string, boolean>>(initial: T) {
  const [expanded, setExpanded] = useState<T>(initial);

  const toggle = useCallback((section: keyof T) => {
    setExpanded((prev) => ({ ...prev, [section]: !prev[section] }));
  }, []);

  return { expanded, toggle, setExpanded };
}
