import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Markdown from "@/components/Markdown";
import { FileText, Loader2, RefreshCw, Sparkles } from "lucide-react";
import { getChats, getSummaries, getSummaryProgress, triggerSummarize, triggerSummarizeAll } from "@/lib/api";

const CATEGORIES = [
  { key: "full", label: "完整报告" },
  { key: "tech", label: "技术信息" },
  { key: "business", label: "商业信息" },
  { key: "resource", label: "资源与链接" },
  { key: "decision", label: "关键决策" },
  { key: "opinion", label: "重要观点" },
];

export default function Summary() {
  const [chats, setChats] = useState([]);
  const [selectedChat, setSelectedChat] = useState(null);
  const [summaries, setSummaries] = useState([]);
  const [activeTab, setActiveTab] = useState("full");
  const [generating, setGenerating] = useState(false);
  const [progress, setProgress] = useState(null);
  const [error, setError] = useState(null);
  const pollRef = useRef(null);

  useEffect(() => {
    getChats().then((data) => {
      setChats(data);
      const sorted = [...data].sort((a, b) => b.message_count - a.message_count);
      if (sorted.length > 0) setSelectedChat(sorted[0].chat_id);
    });
  }, []);

  const sortedChats = useMemo(() => {
    return [...chats].sort((a, b) => b.message_count - a.message_count);
  }, [chats]);

  const maxCount = useMemo(() => {
    return sortedChats.length > 0 ? sortedChats[0].message_count : 1;
  }, [sortedChats]);

  useEffect(() => {
    if (selectedChat) {
      getSummaries(selectedChat).then(setSummaries).catch(() => setSummaries([]));
    }
  }, [selectedChat]);

  const stopPolling = useCallback(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  const startPolling = useCallback(() => {
    stopPolling();
    setGenerating(true);
    pollRef.current = setInterval(async () => {
      try {
        const prog = await getSummaryProgress();
        setProgress(prog);
        if (!prog.running) {
          stopPolling();
          setGenerating(false);
          if (selectedChat) {
            getSummaries(selectedChat).then(setSummaries).catch(() => {});
          }
        }
      } catch {
        stopPolling();
        setGenerating(false);
      }
    }, 2000);
  }, [stopPolling, selectedChat]);

  useEffect(() => {
    getSummaryProgress().then((prog) => {
      if (prog.running) {
        setProgress(prog);
        setGenerating(true);
        startPolling();
      }
    }).catch(() => {});
    return stopPolling;
  }, [startPolling, stopPolling]);

  const handleGenerate = async (force = false) => {
    if (!selectedChat) return;
    setError(null);
    try {
      await triggerSummarize(selectedChat, force);
      startPolling();
    } catch (err) {
      setError(err.response?.data?.detail || "摘要生成失败");
    }
  };

  const handleGenerateAll = async (force = false) => {
    setError(null);
    try {
      const res = await triggerSummarizeAll(force);
      if (res.status === "exists") {
        setError("所有群聊摘要均已存在，可点击「全部重建」强制重新生成");
        return;
      }
      startPolling();
    } catch (err) {
      setError(err.response?.data?.detail || "批量摘要生成失败");
    }
  };

  const indexedChats = chats.filter((c) => c.index_built);
  const summarizedCount = chats.filter((c) => c.index_built).length;

  const activeSummary = summaries.find((s) => s.category === activeTab);

  const selectedChatObj = chats.find((c) => c.chat_id === selectedChat);

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <h1 className="text-2xl font-bold">摘要报告</h1>
        <div className="flex items-center gap-2">
          <button
            onClick={() => handleGenerateAll(false)}
            disabled={generating || indexedChats.length === 0}
            className="inline-flex items-center gap-2 bg-purple-600 text-white px-3 py-1.5 rounded-md text-sm hover:bg-purple-700 disabled:opacity-50"
            title="为所有已索引但未生成摘要的群聊批量生成"
          >
            <Sparkles size={14} />
            一键生成全部
          </button>
          <button
            onClick={() => handleGenerateAll(true)}
            disabled={generating || indexedChats.length === 0}
            className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground px-2 py-1.5"
            title="强制重新生成所有摘要"
          >
            <RefreshCw size={12} />
            全部重建
          </button>
          <div className="w-px h-6 bg-border mx-1" />
          <button
            onClick={() => handleGenerate(false)}
            disabled={generating || !selectedChat}
            className="inline-flex items-center gap-2 bg-primary text-primary-foreground px-3 py-1.5 rounded-md text-sm hover:opacity-90 disabled:opacity-50"
          >
            {generating ? (
              <Loader2 size={14} className="animate-spin" />
            ) : (
              <FileText size={14} />
            )}
            {generating ? "生成中..." : "生成当前"}
          </button>
          {summaries.length > 0 && (
            <button
              onClick={() => handleGenerate(true)}
              disabled={generating}
              className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
            >
              <RefreshCw size={14} />
              重建当前
            </button>
          )}
        </div>
      </div>

      {/* 群聊选择卡片 */}
      <div className="flex gap-2 overflow-x-auto pb-2 mb-4">
        {sortedChats.map((c) => (
          <button
            key={c.chat_id}
            onClick={() => setSelectedChat(c.chat_id)}
            className={`shrink-0 text-left px-3 py-2 rounded-lg border-y border-r border-l-4 text-sm transition-all ${
              selectedChat === c.chat_id
                ? "border-y-primary border-r-primary bg-primary/5 ring-1 ring-primary"
                : "border-y-border border-r-border bg-card hover:bg-accent"
            }`}
            style={{
              borderLeftColor: `rgba(37, 99, 235, ${0.15 + 0.85 * (c.message_count / maxCount)})`,
            }}
          >
            <span className="font-medium">{c.chat_name}</span>
            <span className="text-xs text-muted-foreground ml-2">
              {c.message_count.toLocaleString()}
            </span>
          </button>
        ))}
      </div>

      {error && (
        <div className="bg-red-50 border border-red-200 rounded-lg p-3 mb-4 text-sm text-red-700">
          {error}
        </div>
      )}

      {/* 生成进度 */}
      {generating && progress && progress.running && (
        <div className="bg-blue-50 border border-blue-200 rounded-lg p-4 mb-4">
          <div className="flex items-center gap-2 mb-2">
            <Loader2 size={16} className="text-blue-600 animate-spin" />
            <span className="font-medium text-blue-800 text-sm">
              正在生成摘要 {progress.total > 0 && `(${progress.completed}/${progress.total})`}
              {progress.queued > 0 && ` · 排队中 ${progress.queued}`}
            </span>
          </div>
          {progress.total > 0 && (
            <div className="w-full bg-blue-100 rounded-full h-2 mb-3">
              <div
                className="bg-blue-500 h-2 rounded-full transition-all duration-500"
                style={{ width: `${(progress.completed / progress.total) * 100}%` }}
              />
            </div>
          )}
          {progress.chat_details && Object.keys(progress.chat_details).length > 0 && (
            <div className="space-y-2">
              {Object.entries(progress.chat_details).map(([name, d]) => {
                const isMap = d.stage === "map";
                const pct = isMap
                  ? (d.map_total > 0 ? (d.map_done / d.map_total) * 100 : 0)
                  : 100;
                const label = isMap
                  ? `分析片段 ${d.map_done}/${d.map_total}`
                  : "合并报告";
                return (
                  <div key={name} className="flex items-center gap-2">
                    <span className="text-xs text-blue-700 w-36 truncate shrink-0" title={name}>{name}</span>
                    <span className={`text-[10px] px-1.5 py-0.5 rounded shrink-0 ${
                      isMap ? "bg-blue-100 text-blue-700" : "bg-purple-100 text-purple-700"
                    }`}>{label}</span>
                    <div className="flex-1 bg-blue-100 rounded-full h-1.5">
                      <div
                        className={`h-full rounded-full transition-all duration-300 ${
                          isMap ? "bg-blue-500" : "bg-purple-500 animate-pulse"
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

      {/* 过期提醒 */}
      {summaries.some((s) => s.stale) && (
        <div className="bg-amber-50 border border-amber-200 rounded-lg p-3 mb-4 text-sm text-amber-800 flex items-center justify-between">
          <span>有新数据导入，当前摘要可能已过期。建议重新生成。</span>
          <button
            onClick={() => handleGenerate(true)}
            disabled={generating}
            className="text-amber-800 underline hover:no-underline text-sm font-medium"
          >
            重新生成
          </button>
        </div>
      )}

      {summaries.length === 0 && !generating ? (
        <div className="text-center py-20 text-muted-foreground">
          <FileText size={48} className="mx-auto mb-4 opacity-50" />
          <p>选择一个群聊并点击「生成摘要」开始分析</p>
        </div>
      ) : (
        <>
          {/* 标签页 */}
          <div className="flex gap-1 border-b border-border mb-4">
            {CATEGORIES.map((cat) => {
              const hasSummary = summaries.some((s) => s.category === cat.key);
              return (
                <button
                  key={cat.key}
                  onClick={() => setActiveTab(cat.key)}
                  className={`px-3 py-2 text-sm border-b-2 transition-colors ${
                    activeTab === cat.key
                      ? "border-primary text-primary font-medium"
                      : "border-transparent text-muted-foreground hover:text-foreground"
                  } ${!hasSummary ? "opacity-40" : ""}`}
                >
                  {cat.label}
                </button>
              );
            })}
          </div>

          {/* 内容 */}
          {activeSummary ? (
            <div className="bg-card border border-border rounded-lg p-6 prose prose-sm max-w-none">
              <Markdown>{activeSummary.content}</Markdown>
            </div>
          ) : (
            <div className="text-center py-12 text-muted-foreground">
              <p>该分类暂无摘要内容</p>
            </div>
          )}
        </>
      )}
    </div>
  );
}
