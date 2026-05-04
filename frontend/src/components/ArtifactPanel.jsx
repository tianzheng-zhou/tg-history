import { useCallback, useEffect, useRef, useState } from "react";
import { AlertCircle, FileText, Loader2, RefreshCw, X } from "lucide-react";
import ArtifactView from "./ArtifactView";
import { listArtifacts } from "@/lib/api";

/**
 * Artifact 侧边面板（QA 页右侧滑出）。
 *
 * 职责：管理多 artifact tab 切换 + 关闭按钮 + 列表刷新。
 * 单条 artifact 的详情 / 版本 / 工具栏委托给 <ArtifactView>。
 *
 * Props:
 *  - sessionId: string | null    当前 session
 *  - isOpen: boolean             父组件控制的可见性
 *  - onClose: () => void         点击 × 时调用
 *  - lastArtifactKey: string | null  当 run 推送 artifact_event 时父组件透传过来
 *  - artifactEventCounter: number    artifact_event 的单调计数（变化 → 重新拉取列表 + 切到 lastKey）
 *  - onArtifactsChange?: (count: number) => void  通知父组件最新数量（用于 header 徽章）
 *  - preferredKey?: string | null    深链场景下父组件指定优先打开的 artifact key
 *  - renderActiveExtraToolbar?: (ctx: { sessionId, artifactKey, refresh }) => ReactNode
 *      为当前选中 artifact 的工具栏右侧追加按钮（如 "🔖 发布"）；ctx.refresh 让外部触发重载
 */
export default function ArtifactPanel({
  sessionId,
  isOpen,
  onClose,
  lastArtifactKey,
  artifactEventCounter = 0,
  onArtifactsChange,
  preferredKey,
  renderActiveExtraToolbar,
}) {
  const [artifacts, setArtifacts] = useState([]); // ArtifactSummary[]
  const [selectedKey, setSelectedKey] = useState(null);
  const [listLoading, setListLoading] = useState(false);
  const [error, setError] = useState(null);
  // 子视图刷新触发器（外部 publish/update 后递增）
  const [viewRefreshKey, setViewRefreshKey] = useState(0);

  const onArtifactsChangeRef = useRef(onArtifactsChange);
  useEffect(() => {
    onArtifactsChangeRef.current = onArtifactsChange;
  }, [onArtifactsChange]);

  // 拉取 artifact 列表
  const refreshList = useCallback(
    async (preferKey = null) => {
      if (!sessionId) {
        setArtifacts([]);
        setSelectedKey(null);
        return;
      }
      setListLoading(true);
      setError(null);
      try {
        const list = await listArtifacts(sessionId);
        setArtifacts(list);
        onArtifactsChangeRef.current?.(list.length);
        const keys = list.map((a) => a.artifact_key);
        if (preferKey && keys.includes(preferKey)) {
          setSelectedKey(preferKey);
        } else if (selectedKey && keys.includes(selectedKey)) {
          // 保留
        } else if (list.length > 0) {
          setSelectedKey(list[0].artifact_key);
        } else {
          setSelectedKey(null);
        }
      } catch (e) {
        setError(`加载 artifact 列表失败：${e.message}`);
      } finally {
        setListLoading(false);
      }
    },
    [sessionId, selectedKey]
  );

  // sessionId 或 preferredKey 变化 → 重新加载并尝试切到 preferredKey
  useEffect(() => {
    setSelectedKey(null);
    refreshList(preferredKey || null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, preferredKey]);

  // artifactEventCounter 跳一下 → 拉新列表 + 切到最新 key + 让 ArtifactView 重载内容
  useEffect(() => {
    if (artifactEventCounter > 0) {
      refreshList(lastArtifactKey || null);
      setViewRefreshKey((k) => k + 1);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [artifactEventCounter]);

  // 子视图删除成功 → 从 tab 列表移除当前选中 + 刷新
  const handleDeleted = useCallback(() => {
    setSelectedKey(null);
    refreshList();
  }, [refreshList]);

  // 子视图请求重新加载（如发布完成后想刷新 publication 标记，但 artifact 内容本身没变；保留接口）
  const triggerViewRefresh = useCallback(() => {
    setViewRefreshKey((k) => k + 1);
  }, []);

  if (!isOpen) return null;

  return (
    <aside className="w-[45%] max-w-[720px] min-w-[360px] border-l border-border bg-background flex flex-col h-full overflow-hidden">
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2 border-b border-border shrink-0 bg-card/40">
        <div className="flex items-center gap-2 min-w-0">
          <FileText size={16} className="text-primary shrink-0" />
          <h2 className="text-sm font-semibold truncate">
            Artifacts ({artifacts.length})
          </h2>
          {listLoading && (
            <Loader2 size={12} className="animate-spin text-muted-foreground shrink-0" />
          )}
        </div>
        <div className="flex items-center gap-1">
          <button
            onClick={() => refreshList()}
            className="p-1 rounded hover:bg-accent text-muted-foreground"
            title="刷新"
          >
            <RefreshCw size={14} />
          </button>
          <button
            onClick={onClose}
            className="p-1 rounded hover:bg-accent text-muted-foreground"
            title="关闭"
          >
            <X size={16} />
          </button>
        </div>
      </div>

      {/* Tabs（多 artifact 切换） */}
      {artifacts.length > 0 && (
        <div className="flex gap-1 px-2 py-1.5 border-b border-border overflow-x-auto shrink-0 bg-card/20">
          {artifacts.map((a) => (
            <button
              key={a.artifact_key}
              onClick={() => setSelectedKey(a.artifact_key)}
              className={`shrink-0 px-2.5 py-1 rounded text-xs font-medium transition-colors max-w-[180px] truncate ${
                a.artifact_key === selectedKey
                  ? "bg-primary text-primary-foreground"
                  : "bg-muted text-muted-foreground hover:bg-accent hover:text-foreground"
              }`}
              title={`${a.title} (${a.artifact_key})`}
            >
              {a.title}
              <span className="ml-1 opacity-70">v{a.current_version}</span>
            </button>
          ))}
        </div>
      )}

      {/* Content */}
      <div className="flex-1 overflow-hidden flex flex-col">
        {error && (
          <div className="text-xs text-red-600 inline-flex items-center gap-1 m-3">
            <AlertCircle size={12} />
            {error}
          </div>
        )}

        {!error && artifacts.length === 0 && !listLoading && <EmptyState />}

        {!error && selectedKey && (
          <ArtifactView
            sessionId={sessionId}
            artifactKey={selectedKey}
            refreshTrigger={viewRefreshKey}
            onDeleted={handleDeleted}
            extraToolbarActions={
              renderActiveExtraToolbar?.({
                sessionId,
                artifactKey: selectedKey,
                refresh: triggerViewRefresh,
              })
            }
          />
        )}
      </div>
    </aside>
  );
}

function EmptyState() {
  return (
    <div className="flex flex-col items-center justify-center flex-1 text-muted-foreground gap-2 text-center py-12 px-6">
      <FileText size={32} className="text-muted-foreground/40" />
      <p className="text-sm">本会话还没有 artifact</p>
      <p className="text-xs max-w-xs leading-relaxed">
        当你向 Agent 请求"梳理 / 汇总 / 列表 / 报告"等长篇结构化产出时，
        Agent 会主动在这里生成可迭代的 markdown 文档。
      </p>
    </div>
  );
}
