import { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertCircle,
  Check,
  ChevronDown,
  Copy,
  Download,
  Loader2,
  Trash2,
} from "lucide-react";
import Markdown from "@/components/Markdown";
import {
  deleteArtifact,
  exportArtifactUrl,
  getArtifact,
  listArtifactVersions,
} from "@/lib/api";

/**
 * Artifact 内容视图（单条 artifact 的详情区）：
 * 标题 + Markdown 正文 + 底部工具栏（版本下拉、复制、导出、删除、自定义按钮）。
 *
 * 不负责"多 artifact 切换"（那是父组件的职责）。被 ArtifactPanel（QA 滑出面板）和
 * Articles 页面（草稿 Tab 右栏）共同使用。
 *
 * Props:
 *  - sessionId: string                    必填
 *  - artifactKey: string                  必填；变化时重置 selectedVersion = 最新
 *  - refreshTrigger?: number              递增时强制重新拉取（外部 publish/update 后通知刷新）
 *  - onDeleted?: () => void               删除成功后调用，父组件刷新列表
 *  - extraToolbarActions?: ReactNode      底部工具栏右侧追加的按钮（Publish 等）
 *  - headerExtra?: ReactNode              标题右侧追加（如"去 QA 会话"链接）
 *  - showDelete?: boolean                 是否展示删除按钮，默认 true
 */
export default function ArtifactView({
  sessionId,
  artifactKey,
  refreshTrigger = 0,
  onDeleted,
  extraToolbarActions,
  headerExtra,
  showDelete = true,
}) {
  const [detail, setDetail] = useState(null);
  const [versions, setVersions] = useState([]);
  const [selectedVersion, setSelectedVersion] = useState(null); // null = 最新
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [copyOk, setCopyOk] = useState(false);
  const [versionMenuOpen, setVersionMenuOpen] = useState(false);

  const onDeletedRef = useRef(onDeleted);
  useEffect(() => {
    onDeletedRef.current = onDeleted;
  }, [onDeleted]);

  // 切 artifactKey 时重置版本选择
  useEffect(() => {
    setSelectedVersion(null);
    setVersionMenuOpen(false);
    setError(null);
  }, [artifactKey]);

  // 加载详情 + 版本列表
  useEffect(() => {
    if (!sessionId || !artifactKey) {
      setDetail(null);
      setVersions([]);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    Promise.all([
      getArtifact(sessionId, artifactKey, selectedVersion),
      listArtifactVersions(sessionId, artifactKey),
    ])
      .then(([d, vs]) => {
        if (cancelled) return;
        setDetail(d);
        setVersions(vs);
      })
      .catch((e) => {
        if (cancelled) return;
        setError(`加载失败：${e.response?.data?.detail || e.message}`);
        setDetail(null);
        setVersions([]);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId, artifactKey, selectedVersion, refreshTrigger]);

  const isLatest = selectedVersion == null;
  const versionLabel = useMemo(() => {
    if (!detail) return "";
    return isLatest ? `v${detail.current_version} (最新)` : `v${detail.version}`;
  }, [detail, isLatest]);

  const handleCopy = async () => {
    if (!detail?.content) return;
    try {
      await navigator.clipboard.writeText(detail.content);
      setCopyOk(true);
      setTimeout(() => setCopyOk(false), 1500);
    } catch {
      alert("复制失败，请手动选中文本");
    }
  };

  const handleExport = () => {
    if (!sessionId || !artifactKey) return;
    const url = exportArtifactUrl(sessionId, artifactKey, selectedVersion);
    window.open(url, "_blank");
  };

  const handleDelete = async () => {
    if (!sessionId || !artifactKey) return;
    const ok = window.confirm(
      `确认删除 artifact 《${detail?.title || artifactKey}》及其全部 ${versions.length} 个版本吗？`
    );
    if (!ok) return;
    try {
      await deleteArtifact(sessionId, artifactKey);
      onDeletedRef.current?.();
    } catch (e) {
      alert(`删除失败：${e.response?.data?.detail || e.message}`);
    }
  };

  if (!sessionId || !artifactKey) {
    return null;
  }

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* Body */}
      <div className="flex-1 overflow-auto px-4 py-3 bg-card/10">
        {error && (
          <div className="text-xs text-red-600 inline-flex items-center gap-1 mb-2">
            <AlertCircle size={12} />
            {error}
          </div>
        )}

        {!error && detail && (
          <div className="prose prose-sm max-w-none">
            {!isLatest && (
              <div className="not-prose mb-3 px-2 py-1.5 rounded bg-amber-50 text-amber-700 text-xs inline-flex items-center gap-1">
                <AlertCircle size={11} />
                正在查看历史版本 v{detail.version}（共 {detail.current_version} 版）
              </div>
            )}
            {loading ? (
              <div className="flex items-center gap-2 text-muted-foreground text-xs">
                <Loader2 size={12} className="animate-spin" />
                加载中...
              </div>
            ) : (
              <>
                <div className="not-prose flex items-start justify-between gap-3 mb-2">
                  <h1 className="text-xl font-bold flex-1 min-w-0">
                    {detail.title}
                  </h1>
                  {headerExtra && (
                    <div className="shrink-0 flex items-center gap-1">
                      {headerExtra}
                    </div>
                  )}
                </div>
                <Markdown>{detail.content}</Markdown>
              </>
            )}
          </div>
        )}

        {!error && !detail && !loading && (
          <div className="text-muted-foreground text-xs">未找到内容</div>
        )}
      </div>

      {/* Footer toolbar */}
      {detail && (
        <div className="px-3 py-2 border-t border-border bg-card/40 flex items-center gap-2 shrink-0 flex-wrap">
          {/* 版本下拉 */}
          <div className="relative">
            <button
              onClick={() => setVersionMenuOpen((v) => !v)}
              className="inline-flex items-center gap-1 text-xs px-2 py-1 rounded border border-border hover:bg-accent"
              disabled={versions.length === 0}
            >
              {versionLabel}
              <ChevronDown size={12} />
            </button>
            {versionMenuOpen && versions.length > 0 && (
              <>
                <div
                  className="fixed inset-0 z-10"
                  onClick={() => setVersionMenuOpen(false)}
                />
                <div className="absolute bottom-full mb-1 left-0 z-20 bg-card border border-border rounded shadow-md min-w-[180px] max-h-64 overflow-auto">
                  <button
                    onClick={() => {
                      setSelectedVersion(null);
                      setVersionMenuOpen(false);
                    }}
                    className={`w-full text-left px-2 py-1.5 text-xs hover:bg-accent ${
                      isLatest ? "bg-primary/10 text-primary" : ""
                    }`}
                  >
                    v{detail.current_version} (最新)
                  </button>
                  <div className="border-t border-border" />
                  {[...versions]
                    .reverse()
                    .filter((v) => v.version !== detail.current_version)
                    .map((v) => (
                      <button
                        key={v.version}
                        onClick={() => {
                          setSelectedVersion(v.version);
                          setVersionMenuOpen(false);
                        }}
                        className={`w-full text-left px-2 py-1.5 text-xs hover:bg-accent ${
                          v.version === selectedVersion
                            ? "bg-primary/10 text-primary"
                            : ""
                        }`}
                      >
                        <span className="font-mono">v{v.version}</span>
                        <span className="ml-2 text-muted-foreground">
                          {v.op}
                          {v.created_at &&
                            ` · ${new Date(v.created_at).toLocaleDateString()}`}
                        </span>
                      </button>
                    ))}
                </div>
              </>
            )}
          </div>

          <div className="flex-1" />

          <button
            onClick={handleCopy}
            disabled={!detail.content}
            className="inline-flex items-center gap-1 text-xs px-2 py-1 rounded border border-border hover:bg-accent disabled:opacity-50"
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
          {extraToolbarActions}
          {showDelete && (
            <button
              onClick={handleDelete}
              className="inline-flex items-center gap-1 text-xs px-2 py-1 rounded border border-red-200 text-red-600 hover:bg-red-50"
              title="删除整个 artifact 及其历史版本"
            >
              <Trash2 size={12} />
              删除
            </button>
          )}
        </div>
      )}
    </div>
  );
}
