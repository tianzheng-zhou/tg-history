import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  AlertCircle,
  ArrowUpRight,
  Check,
  Copy,
  Download,
  Loader2,
  Trash2,
} from "lucide-react";
import Markdown from "@/components/Markdown";
import {
  deleteArticle,
  exportArticleUrl,
  getArticle,
} from "@/lib/api";

/**
 * 已发布文章详情视图。Article 是冻结快照，没有版本选择。
 *
 * 头部展示：标题 + 生成时间（来自源 ArtifactVersion.created_at）+ 源追溯链接 + 基于 v?
 * 工具栏：复制 / 导出 / 删除（撤回）
 *
 * Props:
 *  - articleId: string                必填
 *  - refreshTrigger?: number          递增 → 重拉
 *  - onDeleted?: () => void           删除成功后调用
 */
export default function ArticleView({ articleId, refreshTrigger = 0, onDeleted }) {
  const [article, setArticle] = useState(null); // ArticleDetail
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [copyOk, setCopyOk] = useState(false);

  const onDeletedRef = useRef(onDeleted);
  useEffect(() => {
    onDeletedRef.current = onDeleted;
  }, [onDeleted]);

  useEffect(() => {
    if (!articleId) {
      setArticle(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    getArticle(articleId)
      .then((d) => {
        if (cancelled) return;
        setArticle(d);
      })
      .catch((e) => {
        if (cancelled) return;
        setError(`加载失败：${e.response?.data?.detail || e.message}`);
        setArticle(null);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [articleId, refreshTrigger]);

  const handleCopy = async () => {
    if (!article?.content) return;
    try {
      await navigator.clipboard.writeText(article.content);
      setCopyOk(true);
      setTimeout(() => setCopyOk(false), 1500);
    } catch {
      alert("复制失败，请手动选中文本");
    }
  };

  const handleExport = () => {
    if (!articleId) return;
    window.open(exportArticleUrl(articleId), "_blank");
  };

  const handleDelete = async () => {
    if (!articleId) return;
    const ok = window.confirm(
      `确认从文章库撤回《${article?.title || ""}》吗？\n（不会影响源 artifact）`
    );
    if (!ok) return;
    try {
      await deleteArticle(articleId);
      onDeletedRef.current?.();
    } catch (e) {
      alert(`撤回失败：${e.response?.data?.detail || e.message}`);
    }
  };

  if (!articleId) {
    return (
      <div className="flex items-center justify-center flex-1 text-muted-foreground text-sm">
        请选择一篇文章
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full overflow-hidden">
      <div className="flex-1 overflow-auto px-4 py-3 bg-card/10">
        {error && (
          <div className="text-xs text-red-600 inline-flex items-center gap-1 mb-2">
            <AlertCircle size={12} />
            {error}
          </div>
        )}

        {loading && (
          <div className="flex items-center gap-2 text-muted-foreground text-xs py-4">
            <Loader2 size={12} className="animate-spin" />
            加载中...
          </div>
        )}

        {!loading && !error && article && (
          <div className="prose prose-sm max-w-none">
            {/* 标题区 */}
            <div className="not-prose mb-3">
              <h1 className="text-xl font-bold mb-2">{article.title}</h1>
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
                <span>
                  生成于{" "}
                  <span className="text-foreground font-medium">
                    {formatDateTime(article.content_created_at)}
                  </span>
                </span>
                <span>·</span>
                <SourceLink article={article} />
                <span>·</span>
                <span>
                  基于版本{" "}
                  <span className="font-mono text-foreground">
                    v{article.source_version_number}
                  </span>
                </span>
                <span
                  className="ml-auto opacity-60"
                  title={`发布于 ${formatDateTime(article.published_at)}${
                    article.updated_at !== article.published_at
                      ? `\n上次改动 ${formatDateTime(article.updated_at)}`
                      : ""
                  }`}
                >
                  发布 {relativeTime(article.published_at)}
                </span>
              </div>
            </div>

            <Markdown>{article.content}</Markdown>
          </div>
        )}
      </div>

      {/* 工具栏 */}
      {article && !loading && (
        <div className="px-3 py-2 border-t border-border bg-card/40 flex items-center gap-2 shrink-0 flex-wrap">
          <span className="text-xs text-muted-foreground">
            {article.content_length} 字符
          </span>
          <div className="flex-1" />
          <button
            onClick={handleCopy}
            className="inline-flex items-center gap-1 text-xs px-2 py-1 rounded border border-border hover:bg-accent"
            title="复制 markdown 到剪贴板"
          >
            {copyOk ? (
              <>
                <Check size={12} className="text-green-600" />
                已复制
              </>
            ) : (
              <>
                <Copy size={12} />
                复制
              </>
            )}
          </button>
          <button
            onClick={handleExport}
            className="inline-flex items-center gap-1 text-xs px-2 py-1 rounded border border-border hover:bg-accent"
            title="导出为 .md 文件"
          >
            <Download size={12} />
            导出
          </button>
          <button
            onClick={handleDelete}
            className="inline-flex items-center gap-1 text-xs px-2 py-1 rounded border border-red-200 text-red-600 hover:bg-red-50"
            title="从文章库撤回（不影响源 artifact）"
          >
            <Trash2 size={12} />
            撤回
          </button>
        </div>
      )}
    </div>
  );
}

function SourceLink({ article }) {
  const sessionTitle = article.source_session_title || "（未命名会话）";
  if (article.source_exists && article.source_session_id) {
    const url = `/qa/${article.source_session_id}?artifact=${encodeURIComponent(
      article.source_artifact_key
    )}`;
    return (
      <Link
        to={url}
        className="inline-flex items-center gap-0.5 text-primary hover:underline"
        title="跳转到源会话查看上下文"
      >
        来自《{sessionTitle}》
        <ArrowUpRight size={11} />
      </Link>
    );
  }
  return (
    <span
      className="opacity-60 line-through"
      title="源会话或源 artifact 已被删除"
    >
      来自《{sessionTitle}》
    </span>
  );
}

function formatDateTime(ts) {
  if (!ts) return "";
  try {
    return new Date(ts).toLocaleString("zh-CN", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return String(ts);
  }
}

function relativeTime(ts) {
  if (!ts) return "";
  const d = new Date(ts).getTime();
  const now = Date.now();
  const diff = (now - d) / 1000; // seconds
  if (diff < 60) return "刚刚";
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  if (diff < 86400 * 30) return `${Math.floor(diff / 86400)} 天前`;
  if (diff < 86400 * 365) return `${Math.floor(diff / (86400 * 30))} 个月前`;
  return `${Math.floor(diff / (86400 * 365))} 年前`;
}
