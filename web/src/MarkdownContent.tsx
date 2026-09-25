import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

function safeMarkdownUrl(url: string): string {
  const normalized = url.trim().toLowerCase();
  if (
    normalized.startsWith("#")
    || normalized.startsWith("/")
    || normalized.startsWith("./")
    || normalized.startsWith("../")
  ) {
    return url;
  }
  try {
    const parsed = new URL(url);
    return ["http:", "https:", "mailto:"].includes(parsed.protocol) ? url : "";
  } catch {
    return "";
  }
}

export function MarkdownContent({ content }: { readonly content: string }) {
  return (
    <div className="markdown-body">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        skipHtml
        urlTransform={safeMarkdownUrl}
        components={{
          a: ({ children, href }) => (
            <a href={href} rel="noreferrer noopener" target="_blank">{children}</a>
          ),
          img: ({ alt }) => (
            <span className="markdown-image-placeholder">[图片：{alt ?? "未命名"}]</span>
          ),
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}
