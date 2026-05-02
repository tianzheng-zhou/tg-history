import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import { FileText, Loader2, RefreshCw } from "lucide-react";
import { getChats, getSummaries, triggerSummarize } from "@/lib/api";

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
  const [error, setError] = useState(null);

  useEffect(() => {
    getChats().then((data) => {
      setChats(data);
      if (data.length > 0) setSelectedChat(data[0].chat_id);
    });
  }, []);

  useEffect(() => {
    if (selectedChat) {
      getSummaries(selectedChat).then(setSummaries).catch(() => setSummaries([]));
    }
  }, [selectedChat]);

  const handleGenerate = async (force = false) => {
    if (!selectedChat) return;
    setGenerating(true);
    setError(null);
    try {
      await triggerSummarize(selectedChat, force);
      const data = await getSummaries(selectedChat);
      setSummaries(data);
    } catch (err) {
      setError(err.response?.data?.detail || "摘要生成失败");
    } finally {
      setGenerating(false);
    }
  };

  const activeSummary = summaries.find((s) => s.category === activeTab);

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold">摘要报告</h1>
        <div className="flex items-center gap-3">
          <select
            className="border border-border rounded-md px-3 py-1.5 bg-card text-sm"
            value={selectedChat || ""}
            onChange={(e) => setSelectedChat(e.target.value)}
          >
            {chats.map((c) => (
              <option key={c.chat_id} value={c.chat_id}>
                {c.chat_name}
              </option>
            ))}
          </select>
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
            {generating ? "生成中..." : "生成摘要"}
          </button>
          {summaries.length > 0 && (
            <button
              onClick={() => handleGenerate(true)}
              disabled={generating}
              className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
            >
              <RefreshCw size={14} />
              重新生成
            </button>
          )}
        </div>
      </div>

      {error && (
        <div className="bg-red-50 border border-red-200 rounded-lg p-3 mb-4 text-sm text-red-700">
          {error}
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
              <ReactMarkdown>{activeSummary.content}</ReactMarkdown>
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
