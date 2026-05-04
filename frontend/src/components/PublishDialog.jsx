import { useEffect, useState } from "react";
import { AlertCircle, Bookmark, Loader2, X } from "lucide-react";
import {
  getArtifactPublications,
  publishArtifact,
} from "@/lib/api";

/**
 * Publish 弹窗：把指定 artifact 当前版本发布到文章库。
 *
 * 流程：
 * 1. 打开时先 GET /publications 查现有发布
 * 2. 没有 → 简单确认 → publish(append)
 * 3. 有 → 单选"追加新文章" / "覆盖现有"（带下拉选哪条），确认后调用对应 mode
 *
 * Props:
 *  - isOpen: boolean
 *  - onClose: () => void
 *  - sessionId: string
 *  - artifactKey: string
 *  - artifactTitle?: string                    用于标题展示
 *  - onPublished?: (article: ArticleDetail) => void   成功后回调
 */
export default function PublishDialog({
  isOpen,
  onClose,
  sessionId,
  artifactKey,
  artifactTitle,
  onPublished,
}) {
  const [loading, setLoading] = useState(false);
  const [publishing, setPublishing] = useState(false);
  const [publications, setPublications] = useState([]); // ArticleItem[]
  const [error, setError] = useState(null);
  const [mode, setMode] = useState("append"); // "append" | "overwrite"
  const [targetId, setTargetId] = useState(null);

  // 打开时拉取现有发布
  useEffect(() => {
    if (!isOpen || !sessionId || !artifactKey) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    setMode("append");
    setTargetId(null);
    getArtifactPublications(sessionId, artifactKey)
      .then((list) => {
        if (cancelled) return;
        setPublications(list);
        if (list.length > 0) {
          setTargetId(list[0].id); // 默认选最近一篇作为覆盖目标
        }
      })
      .catch((e) => {
        if (cancelled) return;
        setError(`加载发布历史失败：${e.response?.data?.detail || e.message}`);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [isOpen, sessionId, artifactKey]);

  if (!isOpen) return null;

  const hasPublications = publications.length > 0;

  const handleConfirm = async () => {
    if (publishing) return;
    setPublishing(true);
    setError(null);
    try {
      const opts = { mode };
      if (mode === "overwrite") {
        if (!targetId) {
          setError("请选择要覆盖的目标文章");
          setPublishing(false);
          return;
        }
        opts.targetArticleId = targetId;
      }
      const article = await publishArtifact(sessionId, artifactKey, opts);
      onPublished?.(article);
      onClose?.();
    } catch (e) {
      setError(`发布失败：${e.response?.data?.detail || e.message}`);
    } finally {
      setPublishing(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      onClick={(e) => {
        if (e.target === e.currentTarget && !publishing) onClose?.();
      }}
    >
      <div className="bg-card border border-border rounded-lg shadow-xl w-full max-w-md overflow-hidden">
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-border">
          <div className="flex items-center gap-2">
            <Bookmark size={16} className="text-primary" />
            <h3 className="text-sm font-semibold">发布到文章库</h3>
          </div>
          <button
            onClick={() => !publishing && onClose?.()}
            disabled={publishing}
            className="p-1 rounded hover:bg-accent text-muted-foreground disabled:opacity-50"
          >
            <X size={14} />
          </button>
        </div>

        {/* Body */}
        <div className="px-4 py-3 space-y-3">
          {artifactTitle && (
            <div className="text-xs text-muted-foreground">
              要发布的内容：
              <span className="ml-1 font-medium text-foreground">
                《{artifactTitle}》
              </span>
            </div>
          )}

          {loading && (
            <div className="flex items-center gap-2 text-xs text-muted-foreground py-4">
              <Loader2 size={12} className="animate-spin" />
              检查发布历史...
            </div>
          )}

          {!loading && error && (
            <div className="flex items-start gap-2 text-xs text-red-600 px-2 py-1.5 rounded bg-red-50 border border-red-100">
              <AlertCircle size={12} className="shrink-0 mt-0.5" />
              <span>{error}</span>
            </div>
          )}

          {!loading && !hasPublications && (
            <div className="text-sm">
              这是首次发布。点击「确认发布」会把当前版本作为一条新文章保存到文章库。
            </div>
          )}

          {!loading && hasPublications && (
            <div className="space-y-2">
              <div className="text-sm">
                这个 artifact 已经发布过 {publications.length} 次。
              </div>

              <label className="flex items-start gap-2 px-2 py-2 rounded border border-border hover:bg-accent/30 cursor-pointer">
                <input
                  type="radio"
                  name="publish-mode"
                  value="append"
                  checked={mode === "append"}
                  onChange={() => setMode("append")}
                  className="mt-0.5"
                />
                <div className="flex-1 text-sm">
                  <div className="font-medium">追加为新文章（推荐）</div>
                  <div className="text-xs text-muted-foreground mt-0.5">
                    在文章库新建一条独立快照，旧版本保留，作为里程碑。
                  </div>
                </div>
              </label>

              <label className="flex items-start gap-2 px-2 py-2 rounded border border-border hover:bg-accent/30 cursor-pointer">
                <input
                  type="radio"
                  name="publish-mode"
                  value="overwrite"
                  checked={mode === "overwrite"}
                  onChange={() => setMode("overwrite")}
                  className="mt-0.5"
                />
                <div className="flex-1 text-sm">
                  <div className="font-medium">覆盖现有文章</div>
                  <div className="text-xs text-muted-foreground mt-0.5 mb-2">
                    把已有的某篇文章更新为当前版本（旧内容会被替换）。
                  </div>
                  <select
                    value={targetId || ""}
                    onChange={(e) => setTargetId(e.target.value)}
                    disabled={mode !== "overwrite"}
                    className="w-full text-xs px-2 py-1.5 rounded border border-border bg-background disabled:opacity-50"
                  >
                    {publications.map((p) => (
                      <option key={p.id} value={p.id}>
                        《{p.title}》 · 基于 v{p.source_version_number} ·{" "}
                        {formatDate(p.content_created_at)}
                      </option>
                    ))}
                  </select>
                </div>
              </label>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="px-4 py-3 border-t border-border flex items-center justify-end gap-2 bg-card/40">
          <button
            onClick={() => !publishing && onClose?.()}
            disabled={publishing}
            className="text-xs px-3 py-1.5 rounded border border-border hover:bg-accent disabled:opacity-50"
          >
            取消
          </button>
          <button
            onClick={handleConfirm}
            disabled={loading || publishing || (hasPublications && mode === "overwrite" && !targetId)}
            className="inline-flex items-center gap-1 text-xs px-3 py-1.5 rounded bg-primary text-primary-foreground hover:opacity-90 disabled:opacity-50"
          >
            {publishing && <Loader2 size={12} className="animate-spin" />}
            {publishing ? "发布中..." : "确认发布"}
          </button>
        </div>
      </div>
    </div>
  );
}

function formatDate(ts) {
  if (!ts) return "";
  try {
    const d = new Date(ts);
    return d.toLocaleString("zh-CN", {
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
