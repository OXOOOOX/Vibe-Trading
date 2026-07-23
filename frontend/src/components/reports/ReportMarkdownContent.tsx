import ReactMarkdown from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import remarkGfm from "remark-gfm";

interface Props {
  content: string;
}

const remarkPlugins = [remarkGfm];
const rehypePlugins = [rehypeHighlight];

/** Shared Markdown renderer for Session and report-center preview panes. */
export function ReportMarkdownContent({ content }: Props) {
  return (
    <article className="prose prose-sm max-w-none break-words dark:prose-invert prose-headings:scroll-mt-4 prose-table:border prose-table:border-border/50 prose-th:bg-muted/30 prose-th:px-3 prose-th:py-1.5 prose-td:px-3 prose-td:py-1.5 prose-th:text-left prose-th:text-xs prose-th:font-medium prose-td:text-xs [&_table]:block [&_table]:max-w-full [&_table]:overflow-x-auto [&_sup_a]:no-underline [&_sup_a]:font-semibold [&_section[data-footnotes]]:mt-8 [&_section[data-footnotes]]:text-xs [&_section[data-footnotes]]:text-muted-foreground">
      <ReactMarkdown remarkPlugins={remarkPlugins} rehypePlugins={rehypePlugins}>{content}</ReactMarkdown>
    </article>
  );
}
