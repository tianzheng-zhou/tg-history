import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import {
  abortRun as apiAbortRun,
  listActiveRuns,
  startAgentRun,
  startRagRun,
  streamRunEvents,
} from "./api";

/**
 * 全局 Run 管理。挂在 App 顶层，路由切换不 unmount。
 *
 * runs[runId] = {
 *   run_id, session_id, mode, question, status,
 *   agentSteps: [{step, thinking, reasoning, toolCalls, done, isFinalStep}],
 *   ragEvents: [{kind, ...}],          // status / search_result / rerank / context
 *   currentStage,                       // RAG 当前 stage
 *   answer, sources,
 *   usage: {prompt_tokens, completion_tokens, total_tokens, max_context, percent, model},
 *   error,
 *   maxSeq,                             // 最近收到的 seq（用于断线重连）
 * }
 */

const RunsContext = createContext(null);

export function useRuns() {
  const ctx = useContext(RunsContext);
  if (!ctx) throw new Error("useRuns must be used inside <RunsProvider>");
  return ctx;
}

const TERMINAL = new Set(["completed", "aborted", "failed", "lost"]);

function emptyRun(meta) {
  return {
    run_id: meta.run_id,
    session_id: meta.session_id,
    mode: meta.mode || "agent",
    question: meta.question || "",
    status: meta.status || "pending",
    started_at: meta.started_at,
    completed_at: meta.completed_at || null,
    agentSteps: [],
    ragEvents: [],
    currentStage: null,
    answer: "",
    sources: [],
    usage: null,
    taskUsage: null,
    error: null,
    maxSeq: -1,
  };
}

function applyEvent(run, ev) {
  const out = { ...run };
  const t = ev.type;
  const seq = typeof ev.seq === "number" ? ev.seq : null;
  if (seq !== null && seq > out.maxSeq) out.maxSeq = seq;

  if (t === "__end__") {
    out.status = ev.status || "completed";
    return out;
  }
  if (out.status === "pending") out.status = "running";

  if (t === "error") {
    out.error = ev.error || "未知错误";
    return out;
  }

  // ---- Agent 模式事件 ----
  if (t === "step_start") {
    out.agentSteps = [
      ...out.agentSteps,
      { step: ev.step, thinking: "", reasoning: "", toolCalls: [], done: false, isFinalStep: false },
    ];
    return out;
  }
  if (t === "thinking_delta") {
    const steps = [...out.agentSteps];
    const idx = steps.findIndex((s) => s.step === ev.step);
    if (idx !== -1) {
      steps[idx] = { ...steps[idx], thinking: (steps[idx].thinking || "") + (ev.text || "") };
    }
    out.agentSteps = steps;
    return out;
  }
  if (t === "reasoning_delta") {
    const steps = [...out.agentSteps];
    const idx = steps.findIndex((s) => s.step === ev.step);
    if (idx !== -1) {
      steps[idx] = { ...steps[idx], reasoning: (steps[idx].reasoning || "") + (ev.text || "") };
    }
    out.agentSteps = steps;
    return out;
  }
  if (t === "tool_call") {
    const steps = [...out.agentSteps];
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
    out.agentSteps = steps;
    return out;
  }
  if (t === "tool_result") {
    const steps = [...out.agentSteps];
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
    out.agentSteps = steps;
    return out;
  }
  if (t === "sub_agent_event") {
    // 子 Agent 进度事件 — 挂到当前 step 的 subEvents 数组
    const steps = [...out.agentSteps];
    const idx = steps.findIndex((s) => s.step === ev.step);
    if (idx !== -1) {
      const sub = steps[idx].subEvents || [];
      steps[idx] = { ...steps[idx], subEvents: [...sub, ev] };
    }
    out.agentSteps = steps;
    return out;
  }
  if (t === "step_done") {
    const steps = [...out.agentSteps];
    const idx = steps.findIndex((s) => s.step === ev.step);
    if (idx !== -1) {
      steps[idx] = {
        ...steps[idx],
        done: true,
        isFinalStep: !ev.had_tool_calls,
      };
    }
    out.agentSteps = steps;
    return out;
  }
  if (t === "final_answer") {
    out.answer = ev.answer || out.answer;
    out.sources = ev.sources || [];
    out.taskUsage = ev.task_usage || null;
    return out;
  }

  // ---- RAG 模式事件 ----
  if (t === "status") {
    out.currentStage = ev.stage || out.currentStage;
    out.ragEvents = [...out.ragEvents, { kind: "status", stage: ev.stage, message: ev.message }];
    return out;
  }
  if (t === "search_result") {
    out.ragEvents = [
      ...out.ragEvents,
      { kind: "search_result", searchKind: ev.kind, count: ev.count, preview: ev.preview },
    ];
    return out;
  }
  if (t === "rerank") {
    out.ragEvents = [
      ...out.ragEvents,
      { kind: "rerank", before: ev.before, after: ev.after, top_scores: ev.top_scores },
    ];
    return out;
  }
  if (t === "context") {
    out.ragEvents = [
      ...out.ragEvents,
      { kind: "context", count: ev.count, preview: ev.preview },
    ];
    return out;
  }
  if (t === "token") {
    out.answer = (out.answer || "") + (ev.text || "");
    return out;
  }
  if (t === "done") {
    // RAG 模式的最终 done
    out.answer = ev.answer || out.answer;
    out.sources = ev.sources || [];
    out.confidence = ev.confidence;
    out.currentStage = null;
    return out;
  }

  // ---- usage（agent + rag 共享） ----
  if (t === "usage") {
    out.usage = {
      prompt_tokens: ev.prompt_tokens || 0,
      completion_tokens: ev.completion_tokens || 0,
      total_tokens: ev.total_tokens || 0,
      max_context: ev.max_context || 0,
      percent: ev.percent || 0,
      model: ev.model || null,
    };
    return out;
  }

  return out;
}

export function RunsProvider({ children }) {
  const [runs, setRuns] = useState({}); // run_id → run state
  const subsRef = useRef({});           // run_id → AbortController
  const retryRef = useRef({});          // run_id → 重试次数
  const initializedRef = useRef(false);

  const updateRun = useCallback((runId, updater) => {
    setRuns((prev) => {
      const cur = prev[runId];
      if (!cur) return prev;
      const next = updater(cur);
      if (next === cur) return prev;
      return { ...prev, [runId]: next };
    });
  }, []);

  const subscribe = useCallback((runId, fromSeq = -1) => {
    // 已有订阅则跳过
    if (subsRef.current[runId]) return;

    const ctl = new AbortController();
    subsRef.current[runId] = ctl;

    (async () => {
      try {
        await streamRunEvents(runId, {
          lastEventId: fromSeq,
          signal: ctl.signal,
          onEvent: (ev) => updateRun(runId, (r) => applyEvent(r, ev)),
        });
        // 流正常结束（服务端发了 __end__ 后关流）
        retryRef.current[runId] = 0;
      } catch (err) {
        if (err.name === "AbortError" || ctl.signal.aborted) return;

        if (err.status === 404) {
          // run 已被清理
          updateRun(runId, (r) => ({ ...r, status: "lost" }));
          return;
        }

        // 网络错误：指数退避重连
        const cur = (retryRef.current[runId] || 0) + 1;
        retryRef.current[runId] = cur;
        if (cur > 3) {
          updateRun(runId, (r) => (TERMINAL.has(r.status) ? r : { ...r, status: "lost", error: "断线重连失败" }));
          return;
        }
        const wait = 1000 * Math.pow(2, cur - 1); // 1s / 2s / 4s
        setTimeout(() => {
          delete subsRef.current[runId];
          // 用上次见到的最大 seq 续播
          const last = runs[runId]?.maxSeq ?? -1;
          subscribe(runId, last);
        }, wait);
      } finally {
        delete subsRef.current[runId];
      }
    })();
  }, [updateRun, runs]);

  /** 启动一个新 run，立刻 subscribe */
  const startRun = useCallback(async ({ question, sessionId, mode = "agent", chatIds, dateRange, sender }) => {
    const startFn = mode === "rag" ? startRagRun : startAgentRun;
    const resp = await startFn(question, { sessionId, chatIds, dateRange, sender });
    const meta = {
      run_id: resp.run_id,
      session_id: resp.session_id,
      mode,
      question,
      status: resp.already_running ? "running" : "pending",
      started_at: new Date().toISOString(),
    };
    setRuns((prev) => {
      // 若已存在则保留旧 state（已订阅事件）
      if (prev[resp.run_id]) return prev;
      return { ...prev, [resp.run_id]: emptyRun(meta) };
    });
    subscribe(resp.run_id, -1);
    return resp;
  }, [subscribe]);

  const abortRun = useCallback(async (runId) => {
    try {
      await apiAbortRun(runId);
    } catch (e) {
      console.warn("abort failed", e);
    }
  }, []);

  /** 把某 run 从前端 state 中清除（不影响后端） */
  const dropRun = useCallback((runId) => {
    const ctl = subsRef.current[runId];
    if (ctl) ctl.abort();
    delete subsRef.current[runId];
    delete retryRef.current[runId];
    setRuns((prev) => {
      if (!prev[runId]) return prev;
      const next = { ...prev };
      delete next[runId];
      return next;
    });
  }, []);

  // 启动时恢复所有 active runs（刷新页面/重开浏览器后接上）
  useEffect(() => {
    if (initializedRef.current) return;
    initializedRef.current = true;
    (async () => {
      try {
        const list = await listActiveRuns();
        for (const r of list || []) {
          setRuns((prev) => {
            if (prev[r.run_id]) return prev;
            return { ...prev, [r.run_id]: emptyRun(r) };
          });
          subscribe(r.run_id, -1);
        }
      } catch (e) {
        console.warn("listActiveRuns failed", e);
      }
    })();
  }, [subscribe]);

  const value = {
    runs,
    startRun,
    abortRun,
    dropRun,
    subscribe,
  };

  return <RunsContext.Provider value={value}>{children}</RunsContext.Provider>;
}

/** 帮助方法：找到某 session 的当前 active run（status running/pending） */
export function findActiveRunForSession(runs, sessionId) {
  for (const r of Object.values(runs)) {
    if (r.session_id === sessionId && (r.status === "pending" || r.status === "running")) {
      return r;
    }
  }
  return null;
}

/** 帮助方法：找到某 session 最新的 run（含完成的） */
export function findLatestRunForSession(runs, sessionId) {
  let latest = null;
  for (const r of Object.values(runs)) {
    if (r.session_id !== sessionId) continue;
    if (!latest) {
      latest = r;
      continue;
    }
    const a = new Date(r.started_at || 0).getTime();
    const b = new Date(latest.started_at || 0).getTime();
    if (a > b) latest = r;
  }
  return latest;
}
