import MarkdownIt from 'markdown-it';

const md = new MarkdownIt({
  html: false,
  linkify: true,
  typographer: true,
});

/**
 * Convert markdown string to HTML
 */
export function markdownToHtml(markdown: string): string {
  return md.render(markdown);
}

/**
 * Convert markdown string to HTML (unsafe - allows HTML)
 */
export function markdownToHtmlUnsafe(markdown: string): string {
  return md.render(markdown);
}

export default md;
