import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Send, Loader2, History, Filter, Search, Database, FileSearch, Brain, CheckCircle2, AlertCircle, Square, Wrench, Sparkles, ChevronDown, ChevronRight } from "lucide-react";
import { askQuestionStream, askAgentStream, getChats, getQAHistory } from "@/lib/api";
import SourceCard from "@/components/SourceCard";

// RAG 阶段图标
const STAGE_ICONS = {
  semantic_search: Database,
  keyword_search: Search,
  context_expand: FileSearch,
  rerank: FileSearch,
  rerank_skip: AlertCircle,
  generating: Brain,
};

const STAGE_LABELS = {
  semantic_search: "向量语义检索",
  keyword_search: "FTS5 关键词搜索",
  context_expand: "话题上下文扩展",
  rerank: "Rerank 重排序",
  rerank_skip: "跳过 Rerank",
  generating: "生成回答",
};

function RAGTimeline({ events, streaming, currentStage }) {
  const [expanded, setExpanded] = useState(true);

  // 把 events 按状态阶段分组：每个 status 事件后跟随的 search/rerank/context
  const groups = [];
  let curGroup = null;
  for (const ev of events) {
    if (ev.kind === "status") {
      curGroup = { stage: ev.stage, message: ev.message, items: [] };
      groups.push(curGroup);
    } else if (curGroup) {
      curGroup.items.push(ev);
    }
  }

  return (
    <div className="ml-2 mb-2 max-w-[80%]">
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground transition-colors"
      >
        <span className={`transition-transform ${expanded ? "rotate-90" : ""}`}>▶</span>
        <span>RAG 检索过程 ({groups.length} 步)</span>
        {streaming && <Loader2 size={11} className="animate-spin text-primary" />}
      </button>

      {expanded && (
        <div className="mt-2 border-l-2 border-border pl-3 space-y-2">
          {groups.map((g, idx) => {
            const Icon = STAGE_ICONS[g.stage] || Loader2;
            const isActive = streaming && g.stage === currentStage;
            const isDone = !isActive;
            return (
              <div key={idx} className="text-xs">
                <div className="flex items-center gap-1.5 font-medium text-foreground/80">
                  {isActive ? (
                    <Loader2 size={12} className="animate-spin text-primary shrink-0" />
                  ) : isDone ? (
                    <CheckCircle2 size={12} className="text-green-600 shrink-0" />
                  ) : (
                    <Icon size={12} className="shrink-0" />
                  )}
                  <span>{STAGE_LABELS[g.stage] || g.stage}</span>
                  <span className="text-muted-foreground font-normal">— {g.message}</span>
                </div>

                {/* 子事件：检索结果、rerank、context */}
                {g.items.length > 0 && (
                  <div className="mt-1.5 ml-5 space-y-1.5">
                    {g.items.map((item, j) => (
                      <RAGEventDetail key={j} item={item} />
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function RAGEventDetail({ item }) {
  if (item.kind === "search_result") {
    const kindLabel = item.searchKind === "semantic" ? "向量结果" : "关键词结果";
    return (
      <div className="bg-muted/40 rounded p-1.5">
        <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
          <span className="font-medium">{kindLabel}</span>
          <span className="bg-primary/10 text-primary px-1 rounded">{item.count} 条</span>
        </div>
        {item.preview && item.preview.length > 0 && (
          <ul className="mt-1 space-y-0.5">
            {item.preview.map((p, k) => (
              <li key={k} className="text-[11px] text-foreground/70 truncate">
                · {p.sender ? `${p.sender}: ` : ""}{p.snippet}
                {p.distance !== undefined && (
                  <span className="text-muted-foreground ml-1">
                    (dist: {p.distance?.toFixed(3)})
                  </span>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    );
  }
  if (item.kind === "rerank") {
    return (
      <div className="bg-muted/40 rounded p-1.5 text-[11px]">
        <span className="text-muted-foreground">
          {item.before} → {item.after} 条
        </span>
        {item.top_scores && item.top_scores.length > 0 && (
          <span className="ml-2 text-muted-foreground">
            top scores: {item.top_scores.join(", ")}
          </span>
        )}
      </div>
    );
  }
  if (item.kind === "context") {
    return (
      <div className="bg-muted/40 rounded p-1.5">
        <div className="text-[11px] text-muted-foreground">
          最终上下文: <span className="text-primary">{item.count}</span> 条消息
        </div>
        {item.preview && item.preview.length > 0 && (
          <ul className="mt-1 space-y-0.5">
            {item.preview.map((p, k) => (
              <li key={k} className="text-[11px] text-foreground/70 truncate">
                · [{p.date}] {p.sender}: {p.snippet}
              </li>
            ))}
          </ul>
        )}
      </div>
    );
  }
  return null;
}

// ---------- Agent 模式的步骤时间线 ----------

function AgentTimeline({ steps, streaming }) {
  // 过滤掉"最终答案 step"（其内容会渲染在主气泡里，避免重复展示）
  const visibleSteps = steps.filter((s) => !s.isFinalStep);
  if (visibleSteps.length === 0) return null;
  return (
    <div className="ml-2 mb-2 max-w-[80%] space-y-2">
      {visibleSteps.map((step, i) => (
        <AgentStep
          key={step.step}
          step={step}
          streaming={streaming && i === visibleSteps.length - 1}
        />
      ))}
    </div>
  );
}

function AgentStep({ step, streaming }) {
  const [expanded, setExpanded] = useState(true);
  const isActive = streaming && !step.done;
  const hasToolCalls = step.toolCalls && step.toolCalls.length > 0;

  return (
    <div className="border-l-2 border-primary/30 pl-3">
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-1.5 text-xs font-medium hover:text-primary transition-colors"
      >
        {expanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
        <span className="bg-primary/10 text-primary px-1.5 py-0.5 rounded text-[10px]">
          步骤 {step.step}
        </span>
        {isActive ? (
          <Loader2 size={12} className="animate-spin text-primary" />
        ) : (
          <CheckCircle2 size={12} className="text-green-600" />
        )}
        <span className="text-muted-foreground font-normal">
          {hasToolCalls
            ? `调用 ${step.toolCalls.length} 个工具`
            : step.thinking
              ? "思考/回答"
              : "..."}
        </span>
      </button>

      {expanded && (
        <div className="mt-1.5 space-y-2">
          {/* Kimi 深度思考链 */}
          {step.reasoning && (
            <details className="text-xs text-foreground/60 bg-purple-50 rounded p-2">
              <summary className="cursor-pointer flex items-center gap-1 text-[10px] text-purple-600 font-medium">
                <Sparkles size={10} />
                <span>深度思考</span>
                <span className="text-[9px] text-muted-foreground ml-1">({step.reasoning.length} 字符)</span>
              </summary>
              <div className="mt-1 whitespace-pre-wrap text-foreground/70 max-h-48 overflow-auto">
                {step.reasoning}
              </div>
            </details>
          )}

          {/* 思考/回答文本 */}
          {step.thinking && (
            <div className="text-xs text-foreground/80 bg-muted/30 rounded p-2 whitespace-pre-wrap">
              <div className="flex items-center gap-1 text-[10px] text-muted-foreground mb-1">
                <Brain size={10} />
                <span>thinking</span>
              </div>
              <div className="prose prose-xs max-w-none">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{step.thinking}</ReactMarkdown>
              </div>
            </div>
          )}

          {/* 工具调用 */}
          {step.toolCalls?.map((tc, j) => (
            <ToolCallCard key={j} toolCall={tc} />
          ))}
        </div>
      )}
    </div>
  );
}

function ToolCallCard({ toolCall }) {
  const [expanded, setExpanded] = useState(false);
  const done = toolCall.resultReady;
  const err = toolCall.error;

  return (
    <div className="bg-card border border-border rounded p-2">
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-1.5 text-xs font-medium w-full text-left hover:text-primary transition-colors"
      >
        {expanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
        <Wrench size={12} className={err ? "text-red-500" : done ? "text-green-600" : "text-amber-500"} />
        <code className="bg-primary/10 text-primary px-1.5 py-0.5 rounded text-[10px]">
          {toolCall.name}
        </code>
        {done && !err && (
          <span className="text-[10px] text-muted-foreground">
            {toolCall.durationMs}ms
            {toolCall.preview?.count !== undefined && ` · ${toolCall.preview.count} 条`}
          </span>
        )}
        {err && <span className="text-[10px] text-red-500">失败</span>}
        {!done && <Loader2 size={11} className="animate-spin text-amber-500" />}
      </button>

      {expanded && (
        <div className="mt-2 space-y-2 text-[11px]">
          {/* 参数 */}
          <div>
            <div className="text-muted-foreground mb-1">参数：</div>
            <pre className="bg-muted/40 rounded p-1.5 overflow-x-auto text-[10px]">
              {JSON.stringify(toolCall.args, null, 2)}
            </pre>
          </div>

          {/* 结果预览 */}
          {done && toolCall.preview && (
            <div>
              <div className="text-muted-foreground mb-1">
                结果预览{toolCall.preview.summary && ` · ${toolCall.preview.summary}`}：
              </div>
              {toolCall.preview.error ? (
                <div className="bg-red-50 text-red-700 rounded p-1.5">
                  ❌ {toolCall.preview.error}
                </div>
              ) : toolCall.preview.items && toolCall.preview.items.length > 0 ? (
                <ul className="bg-muted/40 rounded p-1.5 space-y-1">
                  {toolCall.preview.items.map((it, k) => (
                    <li key={k} className="text-foreground/70">
                      <span className="text-muted-foreground">
                        {it.sender && `[${it.sender}]`}
                        {it.date && ` ${it.date}`}
                        {it.distance !== undefined && ` (dist: ${it.distance?.toFixed?.(3)})`}
                      </span>{" "}
                      {it.text}
                    </li>
                  ))}
                </ul>
              ) : (
                <div className="text-muted-foreground italic">无结果</div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default function QA() {
  const [chats, setChats] = useState([]);
  const [selectedChats, setSelectedChats] = useState([]);
  const [question, setQuestion] = useState("");
  const [conversation, setConversation] = useState([]);
  const [loading, setLoading] = useState(false);
  const [showFilters, setShowFilters] = useState(false);
  const [showHistory, setShowHistory] = useState(false);
  const [history, setHistory] = useState([]);
  const [mode, setMode] = useState("agent"); // "rag" | "agent"
  const bottomRef = useRef(null);

  useEffect(() => {
    getChats().then(setChats);
    getQAHistory().then(setHistory).catch(() => {});
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [conversation]);

  const abortRef = useRef(null);

  const handleAsk = async () => {
    const q = question.trim();
    if (!q || loading) return;

    // 添加用户消息 + 占位 assistant 消息
    setConversation((prev) => [
      ...prev,
      { role: "user", content: q },
      {
        role: "assistant",
        mode,
        content: "",
        sources: [],
        events: [],        // RAG 模式用
        agentSteps: [],    // Agent 模式用
        currentStage: null,
        streaming: true,
      },
    ]);
    setQuestion("");
    setLoading(true);

    const controller = new AbortController();
    abortRef.current = controller;

    const updateLast = (updater) => {
      setConversation((prev) => {
        const next = [...prev];
        const last = next[next.length - 1];
        if (last && last.role === "assistant") {
          next[next.length - 1] = updater(last);
        }
        return next;
      });
    };

    try {
      if (mode === "agent") {
        await runAgentMode(q, controller.signal, updateLast);
      } else {
        await runRagMode(q, controller.signal, updateLast);
      }
      getQAHistory().then(setHistory).catch(() => {});
    } catch (err) {
      if (err.name === "AbortError") {
        updateLast((m) => ({
          ...m,
          content: m.content + "\n\n_(已中止)_",
          streaming: false,
          currentStage: null,
        }));
      } else {
        updateLast((m) => ({
          ...m,
          content: `抱歉，处理问题时出现错误：${err.message}`,
          streaming: false,
          currentStage: null,
        }));
      }
    } finally {
      setLoading(false);
      abortRef.current = null;
    }
  };

  // -------- RAG 模式 --------
  const runRagMode = async (q, signal, updateLast) => {
    await askQuestionStream(q, {
      chatIds: selectedChats.length > 0 ? selectedChats : null,
      signal,
      onEvent: (ev) => {
        if (ev.type === "status") {
          updateLast((m) => ({
            ...m,
            currentStage: ev.stage,
            events: [...m.events, { kind: "status", stage: ev.stage, message: ev.message }],
          }));
        } else if (ev.type === "search_result") {
          updateLast((m) => ({
            ...m,
            events: [...m.events, {
              kind: "search_result",
              searchKind: ev.kind,
              count: ev.count,
              preview: ev.preview,
            }],
          }));
        } else if (ev.type === "rerank") {
          updateLast((m) => ({
            ...m,
            events: [...m.events, {
              kind: "rerank", before: ev.before, after: ev.after, top_scores: ev.top_scores,
            }],
          }));
        } else if (ev.type === "context") {
          updateLast((m) => ({
            ...m,
            events: [...m.events, { kind: "context", count: ev.count, preview: ev.preview }],
          }));
        } else if (ev.type === "token") {
          updateLast((m) => ({ ...m, content: m.content + ev.text }));
        } else if (ev.type === "done") {
          updateLast((m) => ({
            ...m,
            content: ev.answer || m.content,
            sources: ev.sources || [],
            confidence: ev.confidence,
            streaming: false,
            currentStage: null,
          }));
        } else if (ev.type === "error") {
          updateLast((m) => ({
            ...m,
            content: m.content + `\n\n❌ 错误: ${ev.error}`,
            streaming: false,
            currentStage: null,
          }));
        }
      },
    });
  };

  // -------- Agent 模式 --------
  const runAgentMode = async (q, signal, updateLast) => {
    // conversation 此时还是旧状态（setConversation 是异步的），直接取全部 user/assistant
    const historyMsgs = conversation
      .filter((m) => (m.role === "user" || m.role === "assistant") && m.content)
      .map((m) => ({ role: m.role, content: m.content }));

    await askAgentStream(q, {
      chatIds: selectedChats.length > 0 ? selectedChats : null,
      history: historyMsgs.length > 0 ? historyMsgs : null,
      signal,
      onEvent: (ev) => {
        if (ev.type === "status") {
          // 顶部状态栏，不需要存
        } else if (ev.type === "step_start") {
          updateLast((m) => ({
            ...m,
            agentSteps: [
              ...m.agentSteps,
              { step: ev.step, thinking: "", reasoning: "", toolCalls: [], done: false },
            ],
          }));
        } else if (ev.type === "thinking_delta") {
          updateLast((m) => {
            const steps = [...m.agentSteps];
            const idx = steps.findIndex((s) => s.step === ev.step);
            if (idx !== -1) {
              steps[idx] = { ...steps[idx], thinking: (steps[idx].thinking || "") + ev.text };
            }
            return { ...m, agentSteps: steps };
          });
        } else if (ev.type === "reasoning_delta") {
          // Kimi 思考链
          updateLast((m) => {
            const steps = [...m.agentSteps];
            const idx = steps.findIndex((s) => s.step === ev.step);
            if (idx !== -1) {
              steps[idx] = { ...steps[idx], reasoning: (steps[idx].reasoning || "") + ev.text };
            }
            return { ...m, agentSteps: steps };
          });
        } else if (ev.type === "tool_call") {
          updateLast((m) => {
            const steps = [...m.agentSteps];
            const idx = steps.findIndex((s) => s.step === ev.step);
            if (idx !== -1) {
              steps[idx] = {
                ...steps[idx],
                toolCalls: [
                  ...steps[idx].toolCalls,
                  {
                    id: ev.id,
                    name: ev.name,
                    args: ev.args,
                    resultReady: false,
                    error: false,
                  },
                ],
              };
            }
            return { ...m, agentSteps: steps };
          });
        } else if (ev.type === "tool_result") {
          updateLast((m) => {
            const steps = [...m.agentSteps];
            const idx = steps.findIndex((s) => s.step === ev.step);
            if (idx !== -1) {
              const calls = steps[idx].toolCalls.map((c) =>
                c.id === ev.id
                  ? {
                      ...c,
                      resultReady: true,
                      error: !!ev.error,
                      durationMs: ev.duration_ms,
                      preview: ev.output_preview,
                    }
                  : c
              );
              steps[idx] = { ...steps[idx], toolCalls: calls };
            }
            return { ...m, agentSteps: steps };
          });
        } else if (ev.type === "step_done") {
          updateLast((m) => {
            const steps = [...m.agentSteps];
            const idx = steps.findIndex((s) => s.step === ev.step);
            if (idx !== -1) {
              steps[idx] = {
                ...steps[idx],
                done: true,
                // 没调用工具的 step = 最终答案 step：内容会在主气泡显示，这里标记隐藏
                isFinalStep: !ev.had_tool_calls,
              };
            }
            return { ...m, agentSteps: steps };
          });
        } else if (ev.type === "final_answer") {
          updateLast((m) => ({
            ...m,
            content: ev.answer || m.content,
            sources: ev.sources || [],
            streaming: false,
          }));
        } else if (ev.type === "error") {
          updateLast((m) => ({
            ...m,
            content: m.content + `\n\n❌ 错误: ${ev.error}`,
            streaming: false,
          }));
        }
      },
    });
  };

  const handleStop = () => {
    abortRef.current?.abort();
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
          <div className="flex gap-2 items-center">
            {/* 模式切换 */}
            <div className="inline-flex border border-border rounded-md overflow-hidden text-sm" title="问答模式">
              <button
                onClick={() => setMode("agent")}
                disabled={loading}
                className={`flex items-center gap-1 px-2.5 py-1.5 ${
                  mode === "agent"
                    ? "bg-primary text-primary-foreground"
                    : "text-muted-foreground hover:bg-accent"
                } disabled:opacity-50`}
                title="Agent 模式：LLM 自主调用工具多轮推理"
              >
                <Sparkles size={14} />
                Agent
              </button>
              <button
                onClick={() => setMode("rag")}
                disabled={loading}
                className={`flex items-center gap-1 px-2.5 py-1.5 border-l border-border ${
                  mode === "rag"
                    ? "bg-primary text-primary-foreground"
                    : "text-muted-foreground hover:bg-accent"
                } disabled:opacity-50`}
                title="RAG 模式：固定流程的语义检索 + 一次回答"
              >
                <Database size={14} />
                RAG
              </button>
            </div>
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
                  {/* RAG 检索事件时间线（RAG 模式） */}
                  {msg.role === "assistant" && msg.mode !== "agent" && msg.events && msg.events.length > 0 && (
                    <RAGTimeline events={msg.events} streaming={msg.streaming} currentStage={msg.currentStage} />
                  )}
                  {/* Agent 步骤时间线（Agent 模式） */}
                  {msg.role === "assistant" && msg.mode === "agent" && msg.agentSteps && msg.agentSteps.length > 0 && (
                    <AgentTimeline steps={msg.agentSteps} streaming={msg.streaming} />
                  )}
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
                          {msg.content ? (
                            <ReactMarkdown remarkPlugins={[remarkGfm]}>{msg.content}</ReactMarkdown>
                          ) : (
                            msg.streaming && (
                              <p className="text-sm text-muted-foreground italic">
                                {msg.currentStage === "generating"
                                  ? "正在生成..."
                                  : "准备中..."}
                              </p>
                            )
                          )}
                          {msg.streaming && msg.content && (
                            <span className="inline-block w-2 h-4 bg-primary/60 animate-pulse ml-0.5 align-middle" />
                          )}
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
          {loading ? (
            <button
              onClick={handleStop}
              className="bg-red-500 text-white p-2.5 rounded-lg hover:bg-red-600 transition-colors"
              title="中止"
            >
              <Square size={18} fill="currentColor" />
            </button>
          ) : (
            <button
              onClick={handleAsk}
              disabled={!question.trim()}
              className="bg-primary text-primary-foreground p-2.5 rounded-lg hover:opacity-90 disabled:opacity-50"
            >
              <Send size={18} />
            </button>
          )}
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
