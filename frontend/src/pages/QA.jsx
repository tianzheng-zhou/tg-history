import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import { Send, Loader2, History, Filter } from "lucide-react";
import { askQuestion, getChats, getQAHistory } from "@/lib/api";
import SourceCard from "@/components/SourceCard";

export default function QA() {
  const [chats, setChats] = useState([]);
  const [selectedChats, setSelectedChats] = useState([]);
  const [question, setQuestion] = useState("");
  const [conversation, setConversation] = useState([]);
  const [loading, setLoading] = useState(false);
  const [showFilters, setShowFilters] = useState(false);
  const [showHistory, setShowHistory] = useState(false);
  const [history, setHistory] = useState([]);
  const bottomRef = useRef(null);

  useEffect(() => {
    getChats().then(setChats);
    getQAHistory().then(setHistory).catch(() => {});
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [conversation]);

  const handleAsk = async () => {
    const q = question.trim();
    if (!q || loading) return;

    setConversation((prev) => [...prev, { role: "user", content: q }]);
    setQuestion("");
    setLoading(true);

    try {
      const res = await askQuestion(q, {
        chatIds: selectedChats.length > 0 ? selectedChats : null,
      });
      setConversation((prev) => [
        ...prev,
        {
          role: "assistant",
          content: res.answer,
          sources: res.sources,
          confidence: res.confidence,
        },
      ]);
      getQAHistory().then(setHistory).catch(() => {});
    } catch (err) {
      setConversation((prev) => [
        ...prev,
        {
          role: "assistant",
          content: "抱歉，处理问题时出现错误，请稍后重试。",
          sources: [],
        },
      ]);
    } finally {
      setLoading(false);
    }
  };

  const handleKeyDown = (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleAsk();
    }
  };

  return (
    <div className="flex h-[calc(100vh-3rem)] gap-4">
      {/* 主聊天区 */}
      <div className="flex-1 flex flex-col">
        <div className="flex items-center justify-between mb-4">
          <h1 className="text-2xl font-bold">智能问答</h1>
          <div className="flex gap-2">
            <button
              onClick={() => setShowFilters(!showFilters)}
              className={`inline-flex items-center gap-1 px-3 py-1.5 rounded-md text-sm border ${
                showFilters
                  ? "border-primary text-primary"
                  : "border-border text-muted-foreground"
              }`}
            >
              <Filter size={14} />
              筛选
            </button>
            <button
              onClick={() => setShowHistory(!showHistory)}
              className={`inline-flex items-center gap-1 px-3 py-1.5 rounded-md text-sm border ${
                showHistory
                  ? "border-primary text-primary"
                  : "border-border text-muted-foreground"
              }`}
            >
              <History size={14} />
              历史
            </button>
          </div>
        </div>

        {/* 筛选器 */}
        {showFilters && (
          <div className="bg-card border border-border rounded-lg p-3 mb-3">
            <p className="text-xs text-muted-foreground mb-2">选择群聊范围</p>
            <div className="flex flex-wrap gap-2">
              {chats.map((c) => (
                <label
                  key={c.chat_id}
                  className="inline-flex items-center gap-1.5 text-sm"
                >
                  <input
                    type="checkbox"
                    checked={selectedChats.includes(c.chat_id)}
                    onChange={(e) => {
                      if (e.target.checked) {
                        setSelectedChats((p) => [...p, c.chat_id]);
                      } else {
                        setSelectedChats((p) =>
                          p.filter((id) => id !== c.chat_id)
                        );
                      }
                    }}
                    className="rounded"
                  />
                  {c.chat_name}
                </label>
              ))}
            </div>
          </div>
        )}

        {/* 对话区域 */}
        <div className="flex-1 overflow-auto border border-border rounded-lg p-4 mb-3 bg-card/50">
          {conversation.length === 0 ? (
            <div className="flex flex-col items-center justify-center h-full text-muted-foreground gap-3">
              <p className="text-lg">开始提问吧</p>
              <p className="text-sm">
                你可以问任何关于群聊记录的问题，例如：
              </p>
              <div className="flex flex-wrap gap-2 justify-center max-w-lg">
                {[
                  "群里讨论过哪些技术方案？",
                  "有人分享过有用的链接吗？",
                  "关于XX项目有什么讨论？",
                ].map((q) => (
                  <button
                    key={q}
                    onClick={() => setQuestion(q)}
                    className="text-sm border border-border rounded-full px-3 py-1 hover:bg-accent transition-colors"
                  >
                    {q}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <div className="space-y-4">
              {conversation.map((msg, i) => (
                <div key={i}>
                  <div
                    className={`flex ${
                      msg.role === "user" ? "justify-end" : "justify-start"
                    }`}
                  >
                    <div
                      className={`max-w-[80%] rounded-lg px-4 py-3 ${
                        msg.role === "user"
                          ? "bg-primary text-primary-foreground"
                          : "bg-card border border-border"
                      }`}
                    >
                      {msg.role === "assistant" ? (
                        <div className="prose prose-sm max-w-none">
                          <ReactMarkdown>{msg.content}</ReactMarkdown>
                        </div>
                      ) : (
                        <p className="text-sm">{msg.content}</p>
                      )}
                    </div>
                  </div>
                  {/* 来源引用 */}
                  {msg.sources && msg.sources.length > 0 && (
                    <div className="mt-2 ml-2 space-y-2">
                      <p className="text-xs text-muted-foreground">
                        📎 来源引用 ({msg.sources.length})
                      </p>
                      {msg.sources.map((src, j) => (
                        <SourceCard key={j} source={src} />
                      ))}
                    </div>
                  )}
                </div>
              ))}
              {loading && (
                <div className="flex justify-start">
                  <div className="bg-card border border-border rounded-lg px-4 py-3">
                    <Loader2 size={16} className="animate-spin text-primary" />
                  </div>
                </div>
              )}
              <div ref={bottomRef} />
            </div>
          )}
        </div>

        {/* 输入框 */}
        <div className="flex gap-2">
          <textarea
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="输入你的问题..."
            rows={1}
            className="flex-1 border border-border rounded-lg px-4 py-2.5 text-sm resize-none focus:outline-none focus:ring-2 focus:ring-ring"
          />
          <button
            onClick={handleAsk}
            disabled={!question.trim() || loading}
            className="bg-primary text-primary-foreground p-2.5 rounded-lg hover:opacity-90 disabled:opacity-50"
          >
            <Send size={18} />
          </button>
        </div>
      </div>

      {/* 历史侧边栏 */}
      {showHistory && (
        <aside className="w-72 border border-border rounded-lg bg-card p-3 overflow-auto">
          <h3 className="text-sm font-semibold mb-3">历史问答</h3>
          {history.length === 0 ? (
            <p className="text-xs text-muted-foreground">暂无历史记录</p>
          ) : (
            <div className="space-y-2">
              {history.map((h) => (
                <button
                  key={h.id}
                  onClick={() => setQuestion(h.question)}
                  className="w-full text-left border border-border rounded-md p-2 hover:bg-accent/50 transition-colors"
                >
                  <p className="text-sm font-medium line-clamp-2">
                    {h.question}
                  </p>
                  <p className="text-xs text-muted-foreground mt-1">
                    {new Date(h.created_at).toLocaleString("zh-CN")}
                  </p>
                </button>
              ))}
            </div>
          )}
        </aside>
      )}
    </div>
  );
}
