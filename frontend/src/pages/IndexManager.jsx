import { useEffect } from "react";
import {
  Database,
  RefreshCw,
  CheckCircle2,
  AlertTriangle,
  Loader2,
  Zap,
} from "lucide-react";
import { rebuildIndex, rebuildAllIndex } from "@/lib/api";
import { useIndexStore } from "@/lib/indexStore";

export default function IndexManager() {
  // 全局 store：chats + progress + 开始构建的操作切走再切回不丢
  const {
    chats,
    progress,
    rebuilding,
    refreshChats,
    refreshProgress,
    startPolling,
  } = useIndexStore();

  // 组件 mount 时 soft-refresh：确保显示最新状态（store 已有旧值，用户看到旧值后即刻被新值覆盖，没空窗）
  useEffect(() => {
    refreshChats();
    refreshProgress();
  }, [refreshChats, refreshProgress]);

  const handleRebuild = async (chatId, force = false) => {
    if (force && !window.confirm("强制全量重建会清空所有旧话题重新划分（token 开销较大），确定继续？")) return;
    try {
      await rebuildIndex(chatId, force);
      startPolling();
    } catch {}
  };

  const handleRebuildAll = async (force = false) => {
    if (force && !window.confirm("强制全量重建所有群聊会消耗大量 token。\n请仅在切换了 embedding model 或数据损坏时使用，确定继续？")) return;
    try {
      await rebuildAllIndex(force);
      startPolling();
    } catch {}
  };

  const staleChats = chats.filter((c) => !c.index_built);
  const builtChats = chats.filter((c) => c.index_built);

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold">索引管理</h1>
        <div className="flex gap-2">
          {staleChats.length > 0 && (
            <button
              onClick={() => handleRebuildAll(false)}
              disabled={rebuilding}
              className="flex items-center gap-2 bg-amber-600 text-white px-4 py-2 rounded-md text-sm hover:bg-amber-700 transition-colors disabled:opacity-50"
              title="增量更新待索引的群聊（仅处理新消息）"
            >
              <RefreshCw size={16} className={rebuilding ? "animate-spin" : ""} />
              构建过期 ({staleChats.length})
              <span className="text-[10px] bg-amber-700/40 rounded px-1">增量</span>
            </button>
          )}
          <button
            onClick={() => handleRebuildAll(true)}
            disabled={rebuilding || chats.length === 0}
            className="flex items-center gap-2 border border-border px-4 py-2 rounded-md text-sm hover:bg-secondary transition-colors disabled:opacity-50"
            title="强制全量重建（token 开销大，慢）"
          >
            <RefreshCw size={16} className={rebuilding ? "animate-spin" : ""} />
            全部重建
            <span className="text-[10px] bg-secondary rounded px-1">全量</span>
          </button>
        </div>
      </div>

      {/* 构建进度 */}
      {progress && progress.running && (
        <div className="bg-blue-50 border border-blue-200 rounded-lg p-4 mb-6">
          <div className="flex items-center gap-2 mb-2">
            <Loader2 size={18} className="text-blue-600 animate-spin" />
            <span className="font-medium text-blue-800">
              正在构建索引 ({progress.completed}/{progress.total})
              {progress.queued > 0 && ` · 排队中 ${progress.queued}`}
            </span>
          </div>
          <div className="w-full bg-blue-100 rounded-full h-2 mb-3">
            <div
              className="bg-blue-500 h-2 rounded-full transition-all duration-500"
              style={{
                width: progress.total
                  ? `${(progress.completed / progress.total) * 100}%`
                  : "0%",
              }}
            />
          </div>
          {progress.chat_details && Object.keys(progress.chat_details).length > 0 && (
            <div className="space-y-2">
              {Object.entries(progress.chat_details).map(([name, d]) => {
                const isTopics = d.stage === "topics";
                const pct = isTopics
                  ? (d.topic_total > 0 ? (d.topic_done / d.topic_total) * 100 : 0)
                  : (d.index_total > 0 ? (d.index_done / d.index_total) * 100 : 0);
                const label = isTopics
                  ? `语义切分 ${d.topic_done}/${d.topic_total}`
                  : `向量索引 ${d.index_done}/${d.index_total}`;
                return (
                  <div key={name} className="flex items-center gap-2">
                    <span className="text-xs text-blue-700 w-36 truncate shrink-0" title={name}>{name}</span>
                    <span className={`text-[10px] px-1.5 py-0.5 rounded shrink-0 ${
                      isTopics ? "bg-purple-100 text-purple-700" : "bg-green-100 text-green-700"
                    }`}>{label}</span>
                    <div className="flex-1 bg-blue-100 rounded-full h-1.5">
                      <div
                        className={`h-full rounded-full transition-all duration-300 ${
                          isTopics ? "bg-purple-500" : "bg-green-500"
                        }`}
                        style={{ width: `${Math.min(pct, 100)}%` }}
                      />
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}

      {/* 构建结果 */}
      {progress && !progress.running && progress.results?.length > 0 && (
        <div className="bg-green-50 border border-green-200 rounded-lg p-4 mb-6">
          <p className="font-medium text-green-800 mb-2">
            最近一次构建完成 ({progress.completed}/{progress.total})
          </p>
          <div className="space-y-0.5">
            {progress.results.map((r, i) => (
              <p
                key={i}
                className={`text-xs ${
                  r.status === "ok" ? "text-green-700" : "text-red-600"
                }`}
              >
                {r.status === "ok" ? "✓" : "✗"} {r.chat_name}
                {r.status === "ok" &&
                  r.topics != null &&
                  ` · ${r.topics} 个话题已索引`}
                {r.status === "error" && ` · ${r.error}`}
              </p>
            ))}
          </div>
        </div>
      )}

      {/* 需要重建的群聊 */}
      {staleChats.length > 0 && (
        <div className="mb-6">
          <h2 className="text-sm font-semibold text-amber-700 mb-2 flex items-center gap-2">
            <AlertTriangle size={16} />
            待索引 / 索引过期 ({staleChats.length})
          </h2>
          <div className="border border-amber-200 rounded-lg overflow-hidden">
            {staleChats.map((chat) => (
              <div
                key={chat.chat_id}
                className="flex items-center justify-between px-4 py-3 bg-amber-50 border-b border-amber-100 last:border-b-0"
              >
                <div>
                  <p className="text-sm font-medium">{chat.chat_name}</p>
                  <p className="text-xs text-muted-foreground">
                    {chat.message_count.toLocaleString()} 条消息 ·{" "}
                    {chat.date_range}
                  </p>
                </div>
                <div className="flex items-center gap-1">
                  <button
                    onClick={() => handleRebuild(chat.chat_id, false)}
                    disabled={rebuilding}
                    className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-md bg-amber-600 text-white hover:bg-amber-700 disabled:opacity-50 transition-colors"
                    title="增量构建（仅处理新消息）"
                  >
                    <RefreshCw size={12} />
                    构建索引
                  </button>
                  <button
                    onClick={() => handleRebuild(chat.chat_id, true)}
                    disabled={rebuilding}
                    className="flex items-center text-xs p-1.5 rounded-md border border-amber-300 text-amber-700 hover:bg-amber-100 disabled:opacity-50 transition-colors"
                    title="强制全量重建（token 开销大）"
                  >
                    <Zap size={12} />
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* 已索引的群聊 */}
      {builtChats.length > 0 && (
        <div>
          <h2 className="text-sm font-semibold text-green-700 mb-2 flex items-center gap-2">
            <CheckCircle2 size={16} />
            已索引 ({builtChats.length})
          </h2>
          <div className="border border-border rounded-lg overflow-hidden">
            {builtChats.map((chat) => (
              <div
                key={chat.chat_id}
                className="flex items-center justify-between px-4 py-3 border-b border-border last:border-b-0"
              >
                <div>
                  <p className="text-sm font-medium">{chat.chat_name}</p>
                  <p className="text-xs text-muted-foreground">
                    {chat.message_count.toLocaleString()} 条消息 ·{" "}
                    {chat.date_range}
                  </p>
                </div>
                <div className="flex items-center gap-1">
                  <button
                    onClick={() => handleRebuild(chat.chat_id, false)}
                    disabled={rebuilding}
                    className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-md border border-border text-muted-foreground hover:bg-secondary disabled:opacity-50 transition-colors"
                    title="增量重建（仅处理新消息）"
                  >
                    <RefreshCw size={12} />
                    重建
                  </button>
                  <button
                    onClick={() => handleRebuild(chat.chat_id, true)}
                    disabled={rebuilding}
                    className="flex items-center text-xs p-1.5 rounded-md border border-border text-muted-foreground hover:bg-secondary hover:text-foreground disabled:opacity-50 transition-colors"
                    title="强制全量重建（token 开销大）"
                  >
                    <Zap size={12} />
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {chats.length === 0 && (
        <div className="text-center py-20 text-muted-foreground">
          <Database size={48} className="mx-auto mb-4 opacity-50" />
          <p>暂无已导入的群聊，请先导入数据</p>
        </div>
      )}
    </div>
  );
}
