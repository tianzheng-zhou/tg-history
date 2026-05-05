import { Coins, AlertTriangle } from "lucide-react";

/**
 * 任务完成后显示的 token 用量 + 费用统计。
 *
 * 新版 taskUsage 结构（P0.4 之后）：
 *   {
 *     main: { model, prompt_tokens, completion_tokens, total_tokens,
 *             cached_tokens, cache_creation_tokens, cost_yuan },
 *     sub:  { ... } | undefined,            // 仅当有 research 调用时存在
 *     // 汇总字段（兼容老前端）：
 *     prompt_tokens, completion_tokens, total_tokens, cached_tokens,
 *     cache_creation_tokens, estimated_cost_yuan, model,
 *     // 老兼容字段（已废弃，仅老 session 用）：
 *     sub_agent_prompt_tokens, sub_agent_completion_tokens, sub_agent_cached_tokens
 *   }
 *
 * 老 taskUsage 结构（向后兼容）：没有 main/sub 字段，只有顶层汇总
 */
export default function TaskUsageBadge({ taskUsage, className = "" }) {
  if (!taskUsage) return null;

  const {
    main,
    sub,
    prompt_tokens = 0,
    completion_tokens = 0,
    total_tokens = 0,
    cached_tokens = 0,
    cache_creation_tokens = 0,
    sub_agent_prompt_tokens = 0,
    sub_agent_completion_tokens = 0,
    estimated_cost_yuan = 0,
    model,
  } = taskUsage;

  // 新格式：用 main/sub 双行布局
  if (main) {
    const forced = taskUsage.forced_summary;
    const steps = taskUsage.steps;
    const maxSteps = taskUsage.max_steps;
    return (
      <div
        className={`inline-flex flex-col gap-1 px-2.5 py-1.5 rounded-md border text-xs font-mono
          bg-muted/40 border-border text-muted-foreground ${className}`}
      >
        <UsageRow label="主" data={main} />
        {sub && <UsageRow label="子" data={sub} />}
        <div className="flex items-center gap-2 pt-0.5 border-t border-current/15">
          <span className="opacity-70">合计</span>
          <span className="text-amber-600 dark:text-amber-400 font-semibold">
            ~{fmtCost(estimated_cost_yuan)} CNY
          </span>
          {steps != null && maxSteps != null && (
            <span className="opacity-60 ml-1">
              {steps}/{maxSteps} 步
            </span>
          )}
          {forced && (
            <span className="inline-flex items-center gap-1 text-orange-600 dark:text-orange-400 ml-1"
                  title="已达最大步数，Agent 被强制总结">
              <AlertTriangle size={12} className="shrink-0" />
              被截断
            </span>
          )}
        </div>
      </div>
    );
  }

  // 老格式（兼容老 session）：单行
  const hasSub = sub_agent_prompt_tokens > 0 || sub_agent_completion_tokens > 0;
  return (
    <div
      className={`inline-flex flex-wrap items-center gap-x-3 gap-y-1 px-2.5 py-1.5 rounded-md border text-xs font-mono
        bg-muted/40 border-border text-muted-foreground ${className}`}
    >
      <span className="inline-flex items-center gap-1">
        <Coins size={12} className="shrink-0" />
        <span className="font-medium">usage</span>
      </span>

      <span className="tabular-nums">
        in <b>{fmt(prompt_tokens)}</b>
        {cached_tokens > 0 && (
          <span className="text-emerald-600 dark:text-emerald-400"> (cached {fmt(cached_tokens)})</span>
        )}
        {cache_creation_tokens > 0 && (
          <span className="text-sky-600 dark:text-sky-400"> (new {fmt(cache_creation_tokens)})</span>
        )}
      </span>

      <span className="tabular-nums">out <b>{fmt(completion_tokens)}</b></span>
      <span className="tabular-nums">total <b>{fmt(total_tokens)}</b></span>

      {hasSub && (
        <span className="opacity-70 border-l border-current/20 pl-2 ml-0.5">
          sub-agent: in {fmt(sub_agent_prompt_tokens)} / out {fmt(sub_agent_completion_tokens)}
        </span>
      )}

      <span className="border-l border-current/20 pl-2 ml-0.5 text-amber-600 dark:text-amber-400 font-semibold">
        ~{fmtCost(estimated_cost_yuan)} CNY
      </span>

      {model && (
        <span className="text-[10px] opacity-60 border-l border-current/20 pl-1.5 ml-0.5 normal-case font-sans">
          {model}
        </span>
      )}
    </div>
  );
}

function UsageRow({ label, data }) {
  const {
    model: m,
    prompt_tokens = 0,
    completion_tokens = 0,
    cached_tokens = 0,
    cache_creation_tokens = 0,
    cost_yuan = 0,
  } = data || {};
  return (
    <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1">
      <span className="inline-flex items-center gap-1">
        <Coins size={12} className="shrink-0" />
        <span className="font-medium">{label}</span>
      </span>
      <span className="text-[10px] opacity-70 normal-case font-sans">[{m}]</span>
      <span className="tabular-nums">
        in <b>{fmt(prompt_tokens)}</b>
        {cached_tokens > 0 && (
          <span className="text-emerald-600 dark:text-emerald-400"> (cached {fmt(cached_tokens)})</span>
        )}
        {cache_creation_tokens > 0 && (
          <span className="text-sky-600 dark:text-sky-400"> (new {fmt(cache_creation_tokens)})</span>
        )}
      </span>
      <span className="tabular-nums">out <b>{fmt(completion_tokens)}</b></span>
      <span className="text-amber-600 dark:text-amber-400 font-semibold">
        ~{fmtCost(cost_yuan)}
      </span>
    </div>
  );
}

function fmt(n) {
  if (!n && n !== 0) return "0";
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(2) + "M";
  if (n >= 1000) return (n / 1000).toFixed(1) + "k";
  return String(n);
}

function fmtCost(n) {
  if (!n || n <= 0) return "0";
  if (n < 0.01) return "<0.01";
  return n.toFixed(n < 0.1 ? 4 : 2);
}
