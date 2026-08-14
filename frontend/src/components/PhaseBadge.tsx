import { cn } from '@/lib/utils';

interface PhaseBadgeProps {
  phaseOrder: number;
  phaseName: string;
  totalPhases?: number;
  className?: string;
}

export function PhaseBadge({ phaseOrder, phaseName, totalPhases = 3, className }: PhaseBadgeProps) {
  // Dynamic intensity based on phase order
  const getPhaseIntensity = () => {
    const opacity = 0.3 + (0.7 * ((phaseOrder - 1) / Math.max(totalPhases - 1, 1)));
    return `rgba(59, 130, 246, ${opacity})`;
  };

  const backgroundColor = getPhaseIntensity();
  // Dark navy for lighter (early-phase) badges in light mode -- but that
  // same dark navy has almost no contrast against the dark modal/card
  // backgrounds those badges sit on in dark mode, so it needs to switch to
  // a light color there instead of staying fixed via inline style.
  const isLightBackground = phaseOrder <= totalPhases / 2;

  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium",
        isLightBackground ? "text-blue-900 dark:text-gray-100" : "text-white",
        className
      )}
      style={{ backgroundColor }}
      title={`Phase ${phaseOrder}: ${phaseName}`}
    >
      <span className="font-bold">P{phaseOrder}</span>
      <span className="truncate max-w-[150px]">{phaseName}</span>
    </span>
  );
}