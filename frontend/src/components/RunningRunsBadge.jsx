import { useState, useEffect, useRef } from "react";
import { Loader2 } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { useRuns } from "@/lib/runsStore";

/** 顶栏后台运行任务指示器。 */
export default function RunningRunsBadge() {
  const { runs } = useRuns();
  const [open, setOpen] = useState(false);
  const navigate = useNavigate();
  const ref = useRef(null);

  // 点外面关闭
  useEffect(() => {
    if (!open) return;
    const handler = (e) => {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    };
    window.addEventListener("mousedown", handler);
    return () => window.removeEventListener("mousedown", handler);
  }, [open]);

  const active = Object.values(runs).filter(
    (r) => r.status === "running" || r.status === "pending"
  );

  if (active.length === 0) return null;

  return (
    <div className="relative" ref={ref}>
      <button
        onClick={() => setOpen((v) => !v)}
        className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md border border-primary/30 bg-primary/5 text-primary text-xs font-medium hover:bg-primary/10 transition-colors"
        title="后台运行的任务"
      >
        <Loader2 size={13} className="animate-spin" />
        <span>{active.length} 个任务运行中</span>
      </button>

      {open && (
        <div className="absolute right-0 top-full mt-1 w-80 bg-card border border-border rounded-md shadow-lg z-50 p-1.5 max-h-96 overflow-auto">
          <div className="text-[10px] uppercase tracking-wider text-muted-foreground px-2 py-1">
            进行中的会话
          </div>
          {active.map((r) => (
            <button
              key={r.run_id}
              onClick={() => {
                setOpen(false);
                navigate(`/qa/${r.session_id}`);
              }}
              className="w-full text-left px-2 py-2 rounded hover:bg-accent/40 transition-colors group"
            >
              <div className="flex items-center gap-1.5">
                <span className="text-[10px] uppercase font-bold text-primary px-1 py-px rounded bg-primary/10">
                  {r.mode}
                </span>
                <Loader2 size={10} className="animate-spin text-primary shrink-0" />
                <span className="text-xs text-muted-foreground tabular-nums">
                  {elapsedSeconds(r.started_at)}s
                </span>
              </div>
              <div className="text-sm mt-1 line-clamp-2 group-hover:text-primary">
                {r.question || "(空问题)"}
              </div>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function elapsedSeconds(startedAt) {
  if (!startedAt) return "?";
  const start = new Date(startedAt).getTime();
  const now = Date.now();
  return Math.max(0, Math.round((now - start) / 1000));
}
