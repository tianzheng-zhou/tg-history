import { Cpu } from "lucide-react";

/** 紧凑显示当前上下文占比，类 opencode 风格。
 *  usage: { prompt_tokens, completion_tokens, total_tokens, max_context, percent, model }
 *  defaultMax: 无 usage 数据时显示的占位最大窗口（应来自 settings.qa_context_window）
 *  defaultModel: 无 usage 数据时显示的占位模型名
 */
export default function ContextBadge({ usage, defaultMax, defaultModel, className = "" }) {
  const hasUsage = usage && usage.max_context > 0;

  // 默认值（无数据时占位）—— max 来自 settings 而非硬编码 1M
  const fallbackMax = defaultMax && defaultMax > 0 ? defaultMax : 131_072;
  const prompt = hasUsage ? usage.prompt_tokens : 0;
  const max = hasUsage ? usage.max_context : fallbackMax;
  const completion = hasUsage ? usage.completion_tokens : 0;
  const total = hasUsage ? usage.total_tokens : 0;
  const percent = hasUsage ? Math.min(usage.percent || 0, 1) : 0;
  const pctText = (percent * 100).toFixed(1);
  const modelName = (hasUsage && usage.model) || defaultModel || null;

  // 颜色梯度
  let fg, bg, bar;
  if (percent < 0.6) {
    fg = "text-emerald-700 dark:text-emerald-400";
    bg = "bg-emerald-50 dark:bg-emerald-900/30 border-emerald-200 dark:border-emerald-800";
    bar = "bg-emerald-500";
  } else if (percent < 0.85) {
    fg = "text-amber-700 dark:text-amber-400";
    bg = "bg-amber-50 dark:bg-amber-900/30 border-amber-200 dark:border-amber-800";
    bar = "bg-amber-500";
  } else {
    fg = "text-red-700 dark:text-red-400";
    bg = "bg-red-50 dark:bg-red-900/30 border-red-200 dark:border-red-800";
    bar = "bg-red-500";
  }

  if (!hasUsage) {
    fg = "text-muted-foreground";
    bg = "bg-muted/40 border-border";
    bar = "bg-muted-foreground/40";
  }

  const tooltip = hasUsage
    ? `prompt ${formatNum(prompt)} · completion ${formatNum(completion)} · total ${formatNum(total)}` +
      (modelName ? ` · ${modelName}` : "")
    : modelName
      ? `尚无对话 · 模型 ${modelName} · 上下文 ${formatNum(max)}`
      : "尚无对话";

  return (
    <div
      title={tooltip}
      className={`inline-flex items-center gap-2 px-2 py-1 rounded-md border text-xs font-mono ${bg} ${fg} ${className}`}
    >
      <Cpu size={12} className="shrink-0" />
      <span className="font-medium">ctx</span>
      <div className="w-16 h-1.5 bg-foreground/10 rounded-full overflow-hidden">
        <div
          className={`h-full ${bar} transition-all duration-500`}
          style={{ width: `${Math.max(percent * 100, hasUsage ? 1 : 0)}%` }}
        />
      </div>
      <span className="tabular-nums">
        {hasUsage ? formatNum(prompt) : "—"} / {formatNum(max)}
      </span>
      <span className="tabular-nums opacity-80">· {hasUsage ? `${pctText}%` : "—"}</span>
      {modelName && (
        <span className="text-[10px] opacity-60 border-l border-current/20 pl-1.5 ml-0.5 normal-case font-sans">
          {modelName}
        </span>
      )}
    </div>
  );
}

function formatNum(n) {
  if (!n && n !== 0) return "—";
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(2) + "M";
  if (n >= 1000) return (n / 1000).toFixed(1) + "k";
  return String(n);
}
