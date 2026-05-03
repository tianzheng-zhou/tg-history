import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

const URL_RE = /^https?:\/\/[^\s`]+$/i;

const components = {
  // 把形如 `https://...` 的行内代码渲染成可点击链接（保留等宽样式）
  // react-markdown v10 移除了 inline 属性：行内代码没有 className，代码块有 language-xxx
  code({ className, children, ...props }) {
    const isBlock = className && className.startsWith("language-");
    const text = String(children ?? "").trim();
    if (!isBlock && URL_RE.test(text)) {
      return (
        <a
          href={text}
          target="_blank"
          rel="noopener noreferrer"
          className="font-mono text-xs bg-muted px-1 py-0.5 rounded text-primary hover:underline break-all"
        >
          {text}
        </a>
      );
    }
    return (
      <code className={className} {...props}>
        {children}
      </code>
    );
  },

  // 普通 Markdown 链接：新标签页打开
  a({ href, children, ...props }) {
    return (
      <a href={href} target="_blank" rel="noopener noreferrer" {...props}>
        {children}
      </a>
    );
  },
};

/**
 * 统一的 Markdown 渲染器（启用 GFM + 安全外链 + 智能识别代码块里的 URL）
 */
export default function Markdown({ children }) {
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
      {children || ""}
    </ReactMarkdown>
  );
}
