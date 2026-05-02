import { useCallback, useEffect, useRef, useState } from "react";
import {
  Database,
  RefreshCw,
  CheckCircle2,
  AlertTriangle,
  Loader2,
  Clock,
} from "lucide-react";
import { getChats, getIndexProgress, rebuildIndex, rebuildAllIndex } from "@/lib/api";

export default function IndexManager() {
  const [chats, setChats] = useState([]);
  const [progress, setProgress] = useState(null);
  const [rebuilding, setRebuilding] = useState(false);
  const pollRef = useRef(null);

  const loadChats = useCallback(() => {
    getChats().then(setChats).catch(() => {});
  }, []);

  const stopPolling = useCallback(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  const startPolling = useCallback(() => {
    stopPolling();
    setRebuilding(true);
    pollRef.current = setInterval(async () => {
      try {
        const prog = await getIndexProgress();
        setProgress(prog);
        if (!prog.running) {
          stopPolling();
          setRebuilding(false);
          loadChats();
        }
      } catch {
        stopPolling();
        setRebuilding(false);
      }
    }, 1500);
  }, [stopPolling, loadChats]);

  useEffect(() => {
    loadChats();
    getIndexProgress()
      .then((prog) => {
        setProgress(prog);
        if (prog.running) {
          setRebuilding(true);
          startPolling();
        }
      })
      .catch(() => {});
    return stopPolling;
  }, [loadChats, startPolling, stopPolling]);

  const handleRebuild = async (chatId) => {
    try {
      await rebuildIndex(chatId);
      startPolling();
    } catch {}
  };

  const handleRebuildAll = async (force = false) => {
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
            >
              <RefreshCw size={16} className={rebuilding ? "animate-spin" : ""} />
              构建过期 ({staleChats.length})
            </button>
          )}
          <button
            onClick={() => handleRebuildAll(true)}
            disabled={rebuilding || chats.length === 0}
            className="flex items-center gap-2 border border-border px-4 py-2 rounded-md text-sm hover:bg-secondary transition-colors disabled:opacity-50"
          >
            <RefreshCw size={16} className={rebuilding ? "animate-spin" : ""} />
            全部重建
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
                <button
                  onClick={() => handleRebuild(chat.chat_id)}
                  disabled={rebuilding}
                  className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-md bg-amber-600 text-white hover:bg-amber-700 disabled:opacity-50 transition-colors"
                >
                  <RefreshCw size={12} />
                  构建索引
                </button>
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
                <button
                  onClick={() => handleRebuild(chat.chat_id)}
                  disabled={rebuilding}
                  className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-md border border-border text-muted-foreground hover:bg-secondary disabled:opacity-50 transition-colors"
                >
                  <RefreshCw size={12} />
                  重建
                </button>
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
