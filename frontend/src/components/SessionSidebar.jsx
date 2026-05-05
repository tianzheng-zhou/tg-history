import { useState, useEffect, useRef, useCallback } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  Plus,
  Search,
  Pin,
  PinOff,
  Archive,
  ArchiveRestore,
  Trash2,
  Pencil,
  ChevronDown,
  ChevronRight,
  MoreVertical,
  Download,
  FileJson,
  FileText,
  Loader2,
  MessageSquare,
} from "lucide-react";
import {
  listSessions,
  patchSession,
  deleteSession,
  exportSessionUrl,
} from "@/lib/api";
import { useRuns } from "@/lib/runsStore";

/**
 * 会话侧栏。
 * @param {object} props - { onSessionSelected: (id) => void, refreshKey: any }
 */
export default function SessionSidebar({ refreshKey }) {
  const navigate = useNavigate();
  const { sessionId } = useParams();
  const { runs } = useRuns();

  const [active, setActive] = useState([]); // archived=false, pinned 一并
  const [archived, setArchived] = useState([]);
  const [archivedOpen, setArchivedOpen] = useState(false);
  const [archivedTotal, setArchivedTotal] = useState(0);
  const [searchInput, setSearchInput] = useState("");
  const [q, setQ] = useState("");
  const [loading, setLoading] = useState(false);
  const debounceRef = useRef(null);

  // 节流搜索
  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => setQ(searchInput.trim()), 300);
    return () => clearTimeout(debounceRef.current);
  }, [searchInput]);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      // 两次 listSessions 互不依赖 → 并行发，省一次 RTT。
      // archivedOpen=false 时第二次仍发 limit=1 仅为拿 total 计数（用来显示"已归档 (N)"按钮），
      // 服务端 ~25ms，比再设一个独立的 count endpoint 简单。
      const [r1, r2] = await Promise.all([
        listSessions({ archived: false, q: q || undefined, limit: 100 }),
        listSessions({
          archived: true,
          q: q || undefined,
          limit: archivedOpen ? 100 : 1,
        }),
      ]);
      setActive(r1.sessions || []);
      if (archivedOpen) {
        setArchived(r2.sessions || []);
      }
      setArchivedTotal(r2.total || 0);
    } catch (e) {
      console.error("listSessions failed", e);
    } finally {
      setLoading(false);
    }
  }, [q, archivedOpen]);

  // 当某个 run 状态变化（结束 / 新增）时，列表里的 last_preview 会更新 —— 跟 reload 触发条件
  // 合并到同一个 useEffect，避免一次 mount 同时跑两个 effect（StrictMode 下被放大成 4 次 reload，
  // 浏览器并发 8+ 个 listSessions 请求，让用户看到明显的"加载一下"）
  const runsHash = Object.values(runs)
    .map((r) => `${r.session_id}:${r.status}:${r.maxSeq}`)
    .join("|");
  useEffect(() => {
    reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reload, refreshKey, runsHash]);

  const handleNew = () => navigate("/qa");

  const handlePin = async (s) => {
    await patchSession(s.id, { pinned: !s.pinned });
    reload();
  };
  const handleArchive = async (s) => {
    await patchSession(s.id, { archived: !s.archived });
    reload();
  };
  const handleDelete = async (s) => {
    if (!confirm(`删除会话 "${s.title}"？此操作不可撤销。`)) return;
    await deleteSession(s.id);
    if (sessionId === s.id) navigate("/qa");
    reload();
  };
  const handleRename = async (s, newTitle) => {
    if (!newTitle || newTitle === s.title) return;
    await patchSession(s.id, { title: newTitle });
    reload();
  };

  const pinned = active.filter((s) => s.pinned);
  const recent = active.filter((s) => !s.pinned);

  return (
    <aside className="w-64 border-r border-border bg-card/30 flex flex-col h-full">
      <div className="p-3 border-b border-border space-y-2">
        <button
          onClick={handleNew}
          className="w-full inline-flex items-center justify-center gap-1.5 bg-primary text-primary-foreground rounded-md px-3 py-2 text-sm font-medium hover:opacity-90 transition-opacity"
        >
          <Plus size={14} />
          新建对话
        </button>
        <div className="relative">
          <Search size={12} className="absolute left-2 top-1/2 -translate-y-1/2 text-muted-foreground" />
          <input
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
            placeholder="搜索会话..."
            className="w-full text-xs pl-7 pr-2 py-1.5 rounded-md border border-border bg-background focus:outline-none focus:ring-1 focus:ring-ring"
          />
        </div>
      </div>

      <div className="flex-1 overflow-auto p-2 space-y-1">
        {loading && active.length === 0 && (
          <div className="flex items-center justify-center py-6 text-muted-foreground">
            <Loader2 size={14} className="animate-spin" />
          </div>
        )}

        {pinned.length > 0 && (
          <SectionHeader>置顶</SectionHeader>
        )}
        {pinned.map((s) => (
          <SessionItem
            key={s.id}
            session={s}
            isActive={sessionId === s.id}
            isRunning={isSessionRunning(runs, s.id)}
            onClick={() => navigate(`/qa/${s.id}`)}
            onPin={() => handlePin(s)}
            onArchive={() => handleArchive(s)}
            onDelete={() => handleDelete(s)}
            onRename={(t) => handleRename(s, t)}
          />
        ))}

        {recent.length > 0 && pinned.length > 0 && (
          <SectionHeader>最近</SectionHeader>
        )}
        {recent.map((s) => (
          <SessionItem
            key={s.id}
            session={s}
            isActive={sessionId === s.id}
            isRunning={isSessionRunning(runs, s.id)}
            onClick={() => navigate(`/qa/${s.id}`)}
            onPin={() => handlePin(s)}
            onArchive={() => handleArchive(s)}
            onDelete={() => handleDelete(s)}
            onRename={(t) => handleRename(s, t)}
          />
        ))}

        {active.length === 0 && !loading && (
          <p className="text-xs text-muted-foreground text-center py-4">暂无会话</p>
        )}
      </div>

      {archivedTotal > 0 && (
        <div className="border-t border-border">
          <button
            onClick={() => setArchivedOpen((v) => !v)}
            className="w-full px-3 py-2 text-xs text-muted-foreground hover:bg-accent/30 flex items-center gap-1.5"
          >
            {archivedOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
            <Archive size={12} />
            <span>已归档 ({archivedTotal})</span>
          </button>
          {archivedOpen && (
            <div className="px-2 pb-2 space-y-1 max-h-48 overflow-auto">
              {archived.map((s) => (
                <SessionItem
                  key={s.id}
                  session={s}
                  isActive={sessionId === s.id}
                  isRunning={isSessionRunning(runs, s.id)}
                  onClick={() => navigate(`/qa/${s.id}`)}
                  onPin={() => handlePin(s)}
                  onArchive={() => handleArchive(s)}
                  onDelete={() => handleDelete(s)}
                  onRename={(t) => handleRename(s, t)}
                  archived
                />
              ))}
            </div>
          )}
        </div>
      )}
    </aside>
  );
}

function SectionHeader({ children }) {
  return (
    <div className="text-[10px] uppercase tracking-wider text-muted-foreground px-1 pt-2 pb-1">
      {children}
    </div>
  );
}

function isSessionRunning(runs, sessionId) {
  return Object.values(runs).some(
    (r) => r.session_id === sessionId && (r.status === "running" || r.status === "pending")
  );
}

function SessionItem({ session, isActive, isRunning, onClick, onPin, onArchive, onDelete, onRename, archived }) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editValue, setEditValue] = useState(session.title);
  const itemRef = useRef(null);

  useEffect(() => {
    if (!menuOpen) return;
    const h = (e) => { if (itemRef.current && !itemRef.current.contains(e.target)) setMenuOpen(false); };
    window.addEventListener("mousedown", h);
    return () => window.removeEventListener("mousedown", h);
  }, [menuOpen]);

  const submitRename = () => {
    setEditing(false);
    const t = editValue.trim();
    if (t && t !== session.title) onRename(t);
    else setEditValue(session.title);
  };

  return (
    <div
      ref={itemRef}
      className={`group relative rounded-md px-2 py-1.5 text-sm cursor-pointer transition-colors ${
        isActive ? "bg-primary/10 border border-primary/20" : "hover:bg-accent/40 border border-transparent"
      }`}
      onClick={() => !editing && onClick()}
    >
      <div className="flex items-start gap-1.5">
        {session.pinned && !archived && <Pin size={10} className="mt-1 shrink-0 text-amber-500" />}
        {isRunning && <Loader2 size={10} className="mt-1 shrink-0 animate-spin text-primary" />}
        {!session.pinned && !isRunning && !archived && (
          <MessageSquare size={10} className="mt-1 shrink-0 text-muted-foreground/60" />
        )}
        {archived && <Archive size={10} className="mt-1 shrink-0 text-muted-foreground/60" />}

        <div className="flex-1 min-w-0">
          {editing ? (
            <input
              autoFocus
              value={editValue}
              onChange={(e) => setEditValue(e.target.value)}
              onBlur={submitRename}
              onKeyDown={(e) => {
                if (e.key === "Enter") submitRename();
                if (e.key === "Escape") { setEditing(false); setEditValue(session.title); }
              }}
              onClick={(e) => e.stopPropagation()}
              className="w-full text-sm bg-background border border-primary/40 rounded px-1 py-0.5 focus:outline-none"
            />
          ) : (
            <div
              className="font-medium truncate leading-tight"
              onDoubleClick={(e) => { e.stopPropagation(); setEditing(true); }}
              title={session.title}
            >
              {session.title}
            </div>
          )}
          {session.last_preview && !editing && (
            <div className="text-[11px] text-muted-foreground truncate mt-0.5">
              {session.last_preview}
            </div>
          )}
          {session.artifact_count > 0 && !editing && (
            <div className="inline-flex items-center gap-0.5 mt-0.5 text-[10px] text-primary/80">
              <FileText size={9} />
              <span>{session.artifact_count}</span>
            </div>
          )}
        </div>

        <button
          onClick={(e) => { e.stopPropagation(); setMenuOpen((v) => !v); }}
          className="opacity-0 group-hover:opacity-100 transition-opacity p-0.5 hover:bg-accent rounded"
        >
          <MoreVertical size={12} />
        </button>
      </div>

      {menuOpen && (
        <div
          className="absolute right-1 top-full mt-1 w-44 bg-card border border-border rounded-md shadow-lg z-50 py-1 text-sm"
          onClick={(e) => e.stopPropagation()}
        >
          <MenuItem icon={Pencil} onClick={() => { setMenuOpen(false); setEditing(true); }}>重命名</MenuItem>
          {!archived && (
            <MenuItem icon={session.pinned ? PinOff : Pin} onClick={() => { setMenuOpen(false); onPin(); }}>
              {session.pinned ? "取消置顶" : "置顶"}
            </MenuItem>
          )}
          <MenuItem icon={archived ? ArchiveRestore : Archive} onClick={() => { setMenuOpen(false); onArchive(); }}>
            {archived ? "恢复" : "归档"}
          </MenuItem>
          <a
            href={exportSessionUrl(session.id, "md")}
            download
            className="flex items-center gap-2 px-2.5 py-1.5 hover:bg-accent/50 cursor-pointer"
            onClick={() => setMenuOpen(false)}
          >
            <Download size={12} />
            导出 Markdown
          </a>
          <a
            href={exportSessionUrl(session.id, "json")}
            download
            className="flex items-center gap-2 px-2.5 py-1.5 hover:bg-accent/50 cursor-pointer"
            onClick={() => setMenuOpen(false)}
          >
            <FileJson size={12} />
            导出 JSON
          </a>
          <div className="border-t border-border my-1" />
          <MenuItem icon={Trash2} danger onClick={() => { setMenuOpen(false); onDelete(); }}>
            删除
          </MenuItem>
        </div>
      )}
    </div>
  );
}

function MenuItem({ icon: Icon, children, danger, onClick }) {
  return (
    <button
      onClick={onClick}
      className={`w-full flex items-center gap-2 px-2.5 py-1.5 text-left hover:bg-accent/50 ${
        danger ? "text-red-600 hover:bg-red-50" : ""
      }`}
    >
      <Icon size={12} />
      {children}
    </button>
  );
}
