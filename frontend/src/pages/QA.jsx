import { useEffect, useMemo, useRef, useState, useCallback } from "react";
import { useNavigate, useParams } from "react-router-dom";
import Markdown from "@/components/Markdown";
import {
  Send,
  Loader2,
  Filter,
  Search,
  Database,
  FileSearch,
  Brain,
  CheckCircle2,
  AlertCircle,
  Square,
  Wrench,
  Sparkles,
  ChevronDown,
  ChevronRight,
  XCircle,
} from "lucide-react";
import {
  getChats,
  getSession,
  getSettings,
  patchSession,
  autotitleSession,
} from "@/lib/api";
import ContextBadge from "@/components/ContextBadge";
import TaskUsageBadge from "@/components/TaskUsageBadge";
import SessionSidebar from "@/components/SessionSidebar";
import { useRuns, findActiveRunForSession } from "@/lib/runsStore";

// ---------- 阶段图标（RAG 模式时间线） ----------

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

  if (groups.length === 0) return null;

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
            return (
              <div key={idx} className="text-xs">
                <div className="flex items-center gap-1.5 font-medium text-foreground/80">
                  {isActive ? (
                    <Loader2 size={12} className="animate-spin text-primary shrink-0" />
                  ) : (
                    <CheckCircle2 size={12} className="text-green-600 shrink-0" />
                  )}
                  <Icon size={12} className="shrink-0" />
                  <span>{STAGE_LABELS[g.stage] || g.stage}</span>
                  <span className="text-muted-foreground font-normal">— {g.message}</span>
                </div>

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
                    (dist: {p.distance?.toFixed?.(3)})
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
        <span className="text-muted-foreground">{item.before} → {item.after} 条</span>
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

// ---------- Agent 模式时间线 ----------

function AgentTimeline({ steps, streaming }) {
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
  // 流式中默认展开，流式结束（或历史 turn）默认折叠
  const [expanded, setExpanded] = useState(streaming);
  const prevStreaming = useRef(streaming);
  useEffect(() => {
    // streaming: true → false 的瞬间自动折叠（用户后续可手动重新展开）
    if (prevStreaming.current && !streaming) setExpanded(false);
    prevStreaming.current = streaming;
  }, [streaming]);
  const isActive = streaming && !step.done;
  const hasToolCalls = step.tool_calls && step.tool_calls.length > 0;

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
            ? `调用 ${step.tool_calls.length} 个工具`
            : step.thinking
              ? "思考/回答"
              : "..."}
        </span>
      </button>

      {expanded && (
        <div className="mt-1.5 space-y-2">
          {step.reasoning && (
            <details className="text-xs text-foreground/60 bg-purple-50 rounded p-2">
              <summary className="cursor-pointer flex items-center gap-1 text-[10px] text-purple-600 font-medium">
                <Sparkles size={10} />
                <span>深度思考</span>
                <span className="text-[9px] text-muted-foreground ml-1">
                  ({step.reasoning.length} 字符)
                </span>
              </summary>
              <div className="mt-1 whitespace-pre-wrap text-foreground/70 max-h-48 overflow-auto">
                {step.reasoning}
              </div>
            </details>
          )}

          {step.thinking && (
            <div className="text-xs text-foreground/80 bg-muted/30 rounded p-2 whitespace-pre-wrap">
              <div className="flex items-center gap-1 text-[10px] text-muted-foreground mb-1">
                <Brain size={10} />
                <span>thinking</span>
              </div>
              <div className="prose prose-xs max-w-none">
                <Markdown>{step.thinking}</Markdown>
              </div>
            </div>
          )}

          {step.tool_calls?.map((tc, j) => (
            <ToolCallCard key={j} toolCall={tc} />
          ))}
        </div>
      )}
    </div>
  );
}

function ToolCallCard({ toolCall }) {
  const [expanded, setExpanded] = useState(false);
  const done = toolCall.resultReady ?? toolCall.preview !== undefined;
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
            {toolCall.durationMs ?? toolCall.duration_ms}ms
            {toolCall.preview?.count !== undefined && ` · ${toolCall.preview.count} 条`}
            {toolCall.preview?.summary && ` · ${toolCall.preview.summary}`}
          </span>
        )}
        {err && <span className="text-[10px] text-red-500">失败</span>}
        {!done && <Loader2 size={11} className="animate-spin text-amber-500" />}
      </button>

      {expanded && (
        <div className="mt-2 space-y-2 text-[11px]">
          <div>
            <div className="text-muted-foreground mb-1">参数：</div>
            <pre className="bg-muted/40 rounded p-1.5 overflow-x-auto text-[10px]">
              {JSON.stringify(toolCall.args, null, 2)}
            </pre>
          </div>

          {done && toolCall.preview && (
            <div>
              <div className="text-muted-foreground mb-1">
                结果预览{toolCall.preview.summary && !toolCall.preview.report_preview && ` · ${toolCall.preview.summary}`}：
              </div>
              {toolCall.preview.error ? (
                <div className="bg-red-50 text-red-700 rounded p-1.5">
                  {toolCall.preview.error}
                </div>
              ) : toolCall.preview.report_preview ? (
                <div className="bg-muted/40 rounded p-1.5 whitespace-pre-wrap text-foreground/80">
                  {toolCall.preview.report_preview}
                  {toolCall.preview.report_preview.length >= 300 && "..."}
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

// ---------- 把 turn 持久化的 trajectory + agentSteps 形态统一 ----------
// 持久化字段：tool_calls, had_tool_calls
// runStore 字段：toolCalls (动态时用)
// 显示时统一读 step.tool_calls 即可（runStore 也用 toolCalls，需要做 alias）

function normalizeAgentSteps(steps) {
  // steps 来自两个源：runStore（实时）或 trajectory（持久化）。统一字段名。
  return (steps || []).map((s) => {
    const tc = s.tool_calls || s.toolCalls || [];
    const hadTC = s.had_tool_calls ?? s.toolCalls?.length > 0 ?? tc.length > 0;
    return {
      ...s,
      tool_calls: tc,
      had_tool_calls: hadTC,
      isFinalStep: s.isFinalStep ?? !hadTC,
      done: s.done ?? true, // 持久化的 trajectory 都视为 done
    };
  });
}

function normalizeRagEvents(rag_events) {
  // 持久化字段同名，直接返回
  return rag_events || [];
}

// ---------- 主组件 ----------

export default function QA() {
  const navigate = useNavigate();
  const { sessionId: routeSessionId } = useParams();

  const { runs, startRun, abortRun, dropRun } = useRuns();

  const [chats, setChats] = useState([]);
  const [selectedChats, setSelectedChats] = useState([]);
  const [question, setQuestion] = useState("");
  const [showFilters, setShowFilters] = useState(false);
  const [mode, setMode] = useState("agent");

  // 持久化的对话（来自 GET /api/sessions/:id）
  const [persistedTurns, setPersistedTurns] = useState([]);
  const [sessionMeta, setSessionMeta] = useState(null); // 当前 session 的元信息
  const [sessionLoading, setSessionLoading] = useState(false);

  // 当前活跃 run id（前端发起的或从 url 恢复的）
  const [activeRunId, setActiveRunId] = useState(null);
  // 上一次的 usage（即使 run 完成了也保留显示）
  const [stickyUsage, setStickyUsage] = useState(null);
  // 当前 QA 模型 + 其上下文窗口（来自后端 /api/settings，给 ContextBadge 占位用）
  const [qaSettings, setQaSettings] = useState({ model: null, contextWindow: null });

  const bottomRef = useRef(null);

  // 加载群聊列表（用于 filter）+ 当前 QA 模型设置
  useEffect(() => {
    getChats().then(setChats).catch(() => {});
    getSettings()
      .then((s) => setQaSettings({
        model: s.llm_model_qa,
        contextWindow: s.qa_context_window,
      }))
      .catch(() => {});
  }, []);

  // 路由切换 → 加载 session
  useEffect(() => {
    if (!routeSessionId) {
      setPersistedTurns([]);
      setSessionMeta(null);
      setActiveRunId(null);
      setStickyUsage(null);
      return;
    }
    let cancelled = false;
    setSessionLoading(true);
    getSession(routeSessionId)
      .then((data) => {
        if (cancelled) return;
        setSessionMeta(data.session);
        setPersistedTurns(data.turns || []);
        // 恢复 chat_ids 选项
        if (data.session.chat_ids && Array.isArray(data.session.chat_ids)) {
          setSelectedChats(data.session.chat_ids);
        }
        if (data.session.mode) setMode(data.session.mode);
        // 恢复 ContextBadge 的 sticky usage（取最后一条 assistant turn 的 meta.usage）
        const lastAssistant = [...(data.turns || [])].reverse()
          .find((t) => t.role === "assistant" && t.meta?.usage);
        if (lastAssistant?.meta?.usage) setStickyUsage(lastAssistant.meta.usage);
      })
      .catch((err) => {
        console.error("加载 session 失败", err);
        if (!cancelled) navigate("/qa", { replace: true });
      })
      .finally(() => {
        if (!cancelled) setSessionLoading(false);
      });
    return () => { cancelled = true; };
  }, [routeSessionId, navigate]);

  // 自动绑定 session 的活跃 run（切回 QA 页时接上）
  useEffect(() => {
    if (!routeSessionId) {
      if (activeRunId) setActiveRunId(null);
      return;
    }
    const r = findActiveRunForSession(runs, routeSessionId);
    if (r && r.run_id !== activeRunId) {
      setActiveRunId(r.run_id);
    }
  }, [routeSessionId, runs, activeRunId]);

  // 当前活跃 run（来自 store）
  const activeRun = activeRunId ? runs[activeRunId] : null;

  // 同步 sticky usage（每次 run 推 usage 事件就更新）
  useEffect(() => {
    if (activeRun?.usage) setStickyUsage(activeRun.usage);
  }, [activeRun?.usage]);

  // run 终止后自动刷新 session 数据（持久化的 assistant turn 已落库）
  const wasStreaming = useRef(false);
  useEffect(() => {
    const streaming = activeRun && (activeRun.status === "running" || activeRun.status === "pending");
    const justEnded = wasStreaming.current && !streaming && activeRun;
    wasStreaming.current = !!streaming;
    if (justEnded && routeSessionId) {
      // 刷新持久化数据 + 触发自动 title（如果是首轮）
      getSession(routeSessionId).then((data) => {
        setSessionMeta(data.session);
        setPersistedTurns(data.turns || []);
        // 首轮答完 → 异步生成 title
        if ((data.turns || []).length === 2 && (data.session.title === "新对话" || data.session.title?.endsWith("…"))) {
          autotitleSession(routeSessionId).then((updated) => {
            setSessionMeta(updated);
          }).catch(() => {});
        }
      }).catch(() => {});
    }
  }, [activeRun, routeSessionId]);

  // 滚动到底部
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [persistedTurns.length, activeRun?.agentSteps?.length, activeRun?.answer]);

  // 同步 chat_ids 到 session（用户手动改 filter 时）
  const handleToggleChat = useCallback((chatId, on) => {
    setSelectedChats((prev) => {
      const next = on ? [...prev, chatId] : prev.filter((x) => x !== chatId);
      if (routeSessionId) {
        patchSession(routeSessionId, { chat_ids: next }).catch(() => {});
      }
      return next;
    });
  }, [routeSessionId]);

  const handleAsk = async () => {
    const q = question.trim();
    if (!q) return;
    if (activeRun && (activeRun.status === "running" || activeRun.status === "pending")) {
      // 当前 session 已有进行中的 run，禁止再发
      return;
    }
    setQuestion("");
    try {
      const resp = await startRun({
        question: q,
        sessionId: routeSessionId || null,
        mode,
        chatIds: selectedChats.length > 0 ? selectedChats : null,
      });
      setActiveRunId(resp.run_id);
      // 若是新建 session，URL 跳到 /qa/:id
      if (!routeSessionId) {
        navigate(`/qa/${resp.session_id}`, { replace: true });
      } else {
        // 同 session：补充本轮 user turn 立刻显示（后端正在写入）
        setPersistedTurns((prev) => [
          ...prev,
          {
            id: -Date.now(), // 临时 id
            seq: prev.length,
            role: "user",
            content: q,
            created_at: new Date().toISOString(),
          },
        ]);
      }
    } catch (err) {
      console.error("启动 run 失败", err);
      alert(`启动失败: ${err.response?.data?.detail || err.message}`);
    }
  };

  const handleStop = async () => {
    if (activeRunId) await abortRun(activeRunId);
  };

  const handleKeyDown = (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleAsk();
    }
  };

  // 计算实际显示的 conversation：
  // 1. 持久化 turns 为基础（含已落库的 user/assistant）
  // 2. 如果有 activeRun 且当前 session 未有对应的最新 assistant turn，则把 activeRun 作为"流式 assistant"附加
  const isLive = activeRun && (activeRun.status === "running" || activeRun.status === "pending");
  const renderItems = useMemo(() => {
    const items = persistedTurns.map((t) => ({
      kind: "turn",
      turn: t,
    }));
    if (isLive && activeRun) {
      // 检查最后一条 turn 是不是已经覆盖了这次 run 的内容
      // 我们的策略：activeRun 期间，user turn 已 push 到 persistedTurns（startRun 时手动加的）
      // assistant turn 还没落库，渲染流式 activeRun 作为"临时 assistant"
      items.push({ kind: "live", run: activeRun });
    }
    return items;
  }, [persistedTurns, isLive, activeRun]);

  const isEmpty = renderItems.length === 0;

  // 模型/usage 显示来源：当前 run > sticky（最后一次）
  const displayUsage = activeRun?.usage || stickyUsage;

  return (
    <div className="flex h-full">
      {/* 会话侧栏 */}
      <SessionSidebar refreshKey={persistedTurns.length} />

      {/* 主聊天区 */}
      <div className="flex-1 flex flex-col h-full overflow-hidden">
        {/* 顶部条 */}
        <div className="flex items-center justify-between p-3 border-b border-border shrink-0">
          <div className="flex items-center gap-2 min-w-0">
            <h1 className="text-base font-semibold truncate">
              {sessionMeta?.title || "智能问答"}
            </h1>
            {sessionLoading && <Loader2 size={14} className="animate-spin text-muted-foreground" />}
          </div>
          <div className="flex gap-2 items-center">
            <div className="inline-flex border border-border rounded-md overflow-hidden text-sm">
              <button
                onClick={() => setMode("agent")}
                disabled={isLive}
                className={`flex items-center gap-1 px-2.5 py-1 ${
                  mode === "agent"
                    ? "bg-primary text-primary-foreground"
                    : "text-muted-foreground hover:bg-accent"
                } disabled:opacity-50`}
                title="Agent 模式"
              >
                <Sparkles size={14} />
                Agent
              </button>
              <button
                onClick={() => setMode("rag")}
                disabled={isLive}
                className={`flex items-center gap-1 px-2.5 py-1 border-l border-border ${
                  mode === "rag"
                    ? "bg-primary text-primary-foreground"
                    : "text-muted-foreground hover:bg-accent"
                } disabled:opacity-50`}
                title="RAG 模式"
              >
                <Database size={14} />
                RAG
              </button>
            </div>
            <button
              onClick={() => setShowFilters(!showFilters)}
              className={`inline-flex items-center gap-1 px-2.5 py-1 rounded-md text-sm border ${
                showFilters ? "border-primary text-primary" : "border-border text-muted-foreground"
              }`}
            >
              <Filter size={14} />
              筛选
            </button>
          </div>
        </div>

        {/* 筛选器 */}
        {showFilters && (
          <div className="bg-card border-b border-border p-3 shrink-0">
            <p className="text-xs text-muted-foreground mb-2">选择群聊范围</p>
            <div className="flex flex-wrap gap-2">
              {chats.map((c) => (
                <label key={c.chat_id} className="inline-flex items-center gap-1.5 text-sm">
                  <input
                    type="checkbox"
                    checked={selectedChats.includes(c.chat_id)}
                    onChange={(e) => handleToggleChat(c.chat_id, e.target.checked)}
                    className="rounded"
                  />
                  {c.chat_name}
                </label>
              ))}
            </div>
          </div>
        )}

        {/* 对话区 */}
        <div className="flex-1 overflow-auto p-4 bg-card/30">
          {isEmpty && !sessionLoading ? (
            <EmptyState onPickQuestion={(q) => setQuestion(q)} />
          ) : (
            <div className="space-y-4 max-w-4xl mx-auto">
              {renderItems.map((it, idx) => (
                it.kind === "turn"
                  ? <PersistedTurn key={`t-${it.turn.id}`} turn={it.turn} />
                  : <LiveAssistant key={`live-${it.run.run_id}`} run={it.run} />
              ))}
              <div ref={bottomRef} />
            </div>
          )}
        </div>

        {/* 输入区 */}
        <div className="p-3 border-t border-border shrink-0 bg-background">
          <div className="max-w-4xl mx-auto">
            <div className="flex items-center justify-between mb-2 gap-2">
              <ContextBadge
                usage={displayUsage}
                defaultMax={qaSettings.contextWindow}
                defaultModel={qaSettings.model}
              />
              {isLive && activeRun && (
                <span className="text-xs text-muted-foreground inline-flex items-center gap-1">
                  <Loader2 size={11} className="animate-spin" />
                  正在生成... ({activeRun.status})
                </span>
              )}
              {activeRun?.status === "lost" && (
                <span className="text-xs text-amber-600 inline-flex items-center gap-1">
                  <AlertCircle size={11} />
                  会话已过期，请刷新查看最终结果
                </span>
              )}
            </div>
            <div className="flex gap-2">
              <textarea
                value={question}
                onChange={(e) => setQuestion(e.target.value)}
                onKeyDown={handleKeyDown}
                placeholder={isLive ? "正在生成中，请等待或点击停止..." : "输入你的问题..."}
                rows={1}
                disabled={isLive}
                className="flex-1 border border-border rounded-lg px-3 py-2 text-sm resize-none focus:outline-none focus:ring-2 focus:ring-ring disabled:opacity-60"
              />
              {isLive ? (
                <button
                  onClick={handleStop}
                  className="bg-red-500 text-white p-2 rounded-lg hover:bg-red-600 transition-colors"
                  title="停止"
                >
                  <Square size={18} fill="currentColor" />
                </button>
              ) : (
                <button
                  onClick={handleAsk}
                  disabled={!question.trim()}
                  className="bg-primary text-primary-foreground p-2 rounded-lg hover:opacity-90 disabled:opacity-50"
                >
                  <Send size={18} />
                </button>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

// ---------- 子组件：空状态 ----------

function EmptyState({ onPickQuestion }) {
  return (
    <div className="flex flex-col items-center justify-center h-full text-muted-foreground gap-3 py-20">
      <Sparkles size={28} className="text-primary/60" />
      <p className="text-lg">开始提问吧</p>
      <p className="text-sm">你可以问任何关于群聊记录的问题，例如：</p>
      <div className="flex flex-wrap gap-2 justify-center max-w-lg">
        {[
          "群里讨论过哪些技术方案？",
          "有人分享过有用的链接吗？",
          "关于XX项目有什么讨论？",
        ].map((q) => (
          <button
            key={q}
            onClick={() => onPickQuestion(q)}
            className="text-sm border border-border rounded-full px-3 py-1 hover:bg-accent transition-colors"
          >
            {q}
          </button>
        ))}
      </div>
    </div>
  );
}

// ---------- 子组件：持久化 turn ----------

function PersistedTurn({ turn }) {
  if (turn.role === "user") {
    return (
      <div className="flex justify-end">
        <div className="max-w-[80%] rounded-lg px-4 py-3 bg-primary text-primary-foreground">
          <p className="text-sm whitespace-pre-wrap">{turn.content}</p>
        </div>
      </div>
    );
  }

  // assistant turn：trajectory 还原 + content
  const traj = turn.trajectory || {};
  const steps = normalizeAgentSteps(traj.steps);
  const ragEvents = normalizeRagEvents(traj.rag_events);

  const isAborted = turn.meta?.aborted;
  const isFailed = turn.meta?.failed;

  return (
    <div>
      {ragEvents.length > 0 && (
        <RAGTimeline events={ragEvents} streaming={false} currentStage={null} />
      )}
      {steps.length > 0 && <AgentTimeline steps={steps} streaming={false} />}

      <div className="flex justify-start">
        <div className="max-w-[80%] rounded-lg px-4 py-3 bg-card border border-border">
          {(isAborted || isFailed) && (
            <div className="text-xs text-amber-600 mb-2 inline-flex items-center gap-1">
              <XCircle size={11} />
              {isAborted ? "用户中止" : `失败: ${turn.meta?.error || "未知错误"}`}
            </div>
          )}
          <div className="prose prose-sm max-w-none">
            {turn.content ? (
              <Markdown>{turn.content}</Markdown>
            ) : (
              <p className="text-sm text-muted-foreground italic">（无内容）</p>
            )}
          </div>
        </div>
      </div>

      {turn.meta?.task_usage && (
        <div className="mt-2 ml-2">
          <TaskUsageBadge taskUsage={turn.meta.task_usage} />
        </div>
      )}
    </div>
  );
}

// ---------- 子组件：流式 assistant（来自 activeRun） ----------

function LiveAssistant({ run }) {
  const steps = normalizeAgentSteps(run.agentSteps);
  const ragEvents = run.ragEvents || [];
  const streaming = run.status === "running" || run.status === "pending";

  return (
    <div>
      {run.mode === "rag" && ragEvents.length > 0 && (
        <RAGTimeline events={ragEvents} streaming={streaming} currentStage={run.currentStage} />
      )}
      {run.mode === "agent" && steps.length > 0 && (
        <AgentTimeline steps={steps} streaming={streaming} />
      )}

      <div className="flex justify-start">
        <div className="max-w-[80%] rounded-lg px-4 py-3 bg-card border border-border">
          {run.error && (
            <div className="text-xs text-red-600 mb-2 inline-flex items-center gap-1">
              <XCircle size={11} />
              {run.error}
            </div>
          )}
          <div className="prose prose-sm max-w-none">
            {run.answer ? (
              <Markdown>{run.answer}</Markdown>
            ) : streaming ? (
              <p className="text-sm text-muted-foreground italic">
                {run.currentStage === "generating" ? "正在生成..." : "准备中..."}
              </p>
            ) : (
              <p className="text-sm text-muted-foreground italic">（无内容）</p>
            )}
            {streaming && run.answer && (
              <span className="inline-block w-2 h-4 bg-primary/60 animate-pulse ml-0.5 align-middle" />
            )}
          </div>
        </div>
      </div>

      {!streaming && run.taskUsage && (
        <div className="mt-2 ml-2">
          <TaskUsageBadge taskUsage={run.taskUsage} />
        </div>
      )}
    </div>
  );
}
