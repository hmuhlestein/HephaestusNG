// Shared helpers for working with raw agent transcript text (tmux/CLI
// output, includes ANSI color codes and TUI chrome) -- extracted from
// RealTimeAgentOutput.tsx so other views that need to strip it reuse the
// same rules instead of a second, independently drifting copy.

/** Remove ANSI SGR/cursor codes and OSC sequences, leaving plain text. */
export function stripAnsiCodes(text: string): string {
  return text
    .replace(/\x1b\[[?]?[0-9;]*[a-zA-Z]/g, '')
    .replace(/\x1b\][^\x07]*\x07/g, '');
}

/** True if an already-ANSI-stripped, trimmed line is TUI chrome/noise
 * (separators, spinners, "Thinking...", etc.) rather than real content. */
export function isNoiseLine(stripped: string): boolean {
  if (/^[─━═▬▪▫\-=\s]{20,}$/.test(stripped)) return true;
  if (/^(?:[\s⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]*\s*)?Working\.{0,3}\s*$/.test(stripped)) return true;
  if (/^Thinking\.{0,3}\s*$/.test(stripped)) return true;
  if (/^[\s⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏Working\.]+$/.test(stripped) && stripped.includes('Working')) return true;
  if (/^\.\.\. \(\d+ earlier lines/.test(stripped)) return true;
  if (/^[AGBCD\s]+$/.test(stripped) && stripped.length < 10) return true;
  // Spinner character followed by terminal escape sequence residue (e.g. "⠸ 2;128;128;1")
  if (/^[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]\s*[0-9;]+\s*$/.test(stripped)) return true;
  return false;
}
