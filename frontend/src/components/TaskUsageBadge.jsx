import { Coins } from "lucide-react";

/**
 * 任务完成后显示的 token 用量 + 费用统计。
 * taskUsage: {
 *   prompt_tokens, completion_tokens, total_tokens, cached_tokens,
 *   sub_agent_prompt_tokens, sub_agent_completion_tokens, sub_agent_cached_tokens,
 *   estimated_cost_yuan, model
 * }
 */
export default function TaskUsageBadge({ taskUsage, className = "" }) {
  if (!taskUsage) return null;

  const {
    prompt_tokens = 0,
    completion_tokens = 0,
    total_tokens = 0,
    cached_tokens = 0,
    sub_agent_prompt_tokens = 0,
    sub_agent_completion_tokens = 0,
    estimated_cost_yuan = 0,
    model,
  } = taskUsage;

  const hasSub = sub_agent_prompt_tokens > 0 || sub_agent_completion_tokens > 0;
  const costText = estimated_cost_yuan > 0
    ? estimated_cost_yuan < 0.01
      ? `<0.01`
      : estimated_cost_yuan.toFixed(estimated_cost_yuan < 0.1 ? 4 : 2)
    : "0";

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
      </span>

      <span className="tabular-nums">out <b>{fmt(completion_tokens)}</b></span>
      <span className="tabular-nums">total <b>{fmt(total_tokens)}</b></span>

      {hasSub && (
        <span className="opacity-70 border-l border-current/20 pl-2 ml-0.5">
          sub-agent: in {fmt(sub_agent_prompt_tokens)} / out {fmt(sub_agent_completion_tokens)}
        </span>
      )}

      <span className="border-l border-current/20 pl-2 ml-0.5 text-amber-600 dark:text-amber-400 font-semibold">
        ~{costText} CNY
      </span>

      {model && (
        <span className="text-[10px] opacity-60 border-l border-current/20 pl-1.5 ml-0.5 normal-case font-sans">
          {model}
        </span>
      )}
    </div>
  );
}

function fmt(n) {
  if (!n && n !== 0) return "0";
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(2) + "M";
  if (n >= 1000) return (n / 1000).toFixed(1) + "k";
  return String(n);
}
