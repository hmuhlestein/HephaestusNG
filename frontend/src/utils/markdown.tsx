import ReactMarkdown from 'react-markdown';
import rehypeHighlight from 'rehype-highlight';
import remarkGfm from 'remark-gfm';

interface MarkdownRendererProps {
  content: string;
  className?: string;
  // SOLID review 5.4: Results.tsx duplicated this exact set of component
  // overrides (external links open in a new tab; code/paragraphs/headings
  // break long unbroken strings instead of overflowing) across its two
  // dialogs. Opt-in via this flag rather than made the default, so the
  // other existing MarkdownRenderer callers (TaskDetailModal,
  // FeatureDetailModal, etc.) keep their exact current rendering.
  wrapLongContent?: boolean;
}

const wrappingComponents = {
  a: ({ node, ...props }: any) => (
    <a
      {...props}
      target="_blank"
      rel="noreferrer"
      className="text-blue-600 underline hover:text-blue-700"
    />
  ),
  pre: ({ children, ...props }: any) => (
    <pre className="overflow-x-auto" {...props}>
      {children}
    </pre>
  ),
  code: ({ className, children, ...props }: any) => (
    !className ? (
      <code className="bg-gray-100 dark:bg-gray-700 px-1.5 py-0.5 rounded text-xs break-words" {...props}>
        {children}
      </code>
    ) : (
      <code className={className} {...props}>
        {children}
      </code>
    )
  ),
  p: ({ children, ...props }: any) => (
    <p className="break-words overflow-wrap-anywhere" {...props}>
      {children}
    </p>
  ),
  h1: ({ children, ...props }: any) => (
    <h1 className="break-words" {...props}>
      {children}
    </h1>
  ),
  h2: ({ children, ...props }: any) => (
    <h2 className="break-words" {...props}>
      {children}
    </h2>
  ),
  h3: ({ children, ...props }: any) => (
    <h3 className="break-words" {...props}>
      {children}
    </h3>
  ),
};

/**
 * Render markdown content as React components
 * Uses react-markdown with GFM and syntax highlighting
 */
export function MarkdownRenderer({ content, className, wrapLongContent }: MarkdownRendererProps) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      rehypePlugins={[rehypeHighlight]}
      className={className}
      components={wrapLongContent ? wrappingComponents : undefined}
    >
      {content}
    </ReactMarkdown>
  );
}

export default MarkdownRenderer;
