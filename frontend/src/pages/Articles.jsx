import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  ArrowUpRight,
  Bookmark,
  FileText,
  Loader2,
  RefreshCw,
  Search,
} from "lucide-react";
import ArtifactView from "@/components/ArtifactView";
import ArticleView from "@/components/ArticleView";
import PublishDialog from "@/components/PublishDialog";
import {
  listArticles as apiListArticles,
  listDrafts as apiListDrafts,
} from "@/lib/api";

/**
 * 文章库页：跨 session 总览所有 artifact 草稿 + 已发布的文章。
 *
 * 布局：顶部 Tab 切「草稿 / 已发布」+ 搜索/筛选；下方两栏（列表 + 详情）。
 */
export default function Articles() {
  const [activeTab, setActiveTab] = useState("drafts"); // "drafts" | "published"
  const [drafts, setDrafts] = useState([]); // DraftItem[]
  const [articles, setArticles] = useState([]); // ArticleItem[]
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  // 选中状态（每个 tab 独立）
  const [selectedDraftKey, setSelectedDraftKey] = useState(null); // `${session_id}::${artifact_key}`
  const [selectedArticleId, setSelectedArticleId] = useState(null);

  // 搜索 + 筛选
  const [search, setSearch] = useState("");
  const [sessionFilter, setSessionFilter] = useState("");

  // PublishDialog
  const [publishDialog, setPublishDialog] = useState(null); // { sessionId, artifactKey, title }
  const [refreshArticleId, setRefreshArticleId] = useState(0);

  const navigate = useNavigate();

  const fetchAll = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [d, a] = await Promise.all([apiListDrafts(), apiListArticles()]);
      setDrafts(d);
      setArticles(a);
    } catch (e) {
      setError(`加载失败：${e.response?.data?.detail || e.message}`);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchAll();
  }, [fetchAll]);

  // ---------- 草稿 ----------
  const filteredDrafts = useMemo(() => {
    let list = drafts;
    if (sessionFilter) {
      list = list.filter((d) => d.session_id === sessionFilter);
    }
    if (search.trim()) {
      const q = search.trim().toLowerCase();
      list = list.filter(
        (d) =>
          d.title.toLowerCase().includes(q) ||
          d.session_title.toLowerCase().includes(q) ||
          d.artifact_key.toLowerCase().includes(q)
      );
    }
    return list;
  }, [drafts, sessionFilter, search]);

  // 当前选中的 draft
  const activeDraft = useMemo(() => {
    if (!selectedDraftKey) return null;
    return drafts.find(
      (d) => `${d.session_id}::${d.artifact_key}` === selectedDraftKey
    );
  }, [selectedDraftKey, drafts]);

  // 默认选中：tab 切换 / 列表变化时若没选中就选第一项
  useEffect(() => {
    if (activeTab !== "drafts") return;
    if (filteredDrafts.length === 0) {
      setSelectedDraftKey(null);
      return;
    }
    const stillExists = filteredDrafts.some(
      (d) => `${d.session_id}::${d.artifact_key}` === selectedDraftKey
    );
    if (!stillExists) {
      const first = filteredDrafts[0];
      setSelectedDraftKey(`${first.session_id}::${first.artifact_key}`);
    }
  }, [activeTab, filteredDrafts, selectedDraftKey]);

  // ---------- 已发布 ----------
  const filteredArticles = useMemo(() => {
    let list = articles;
    if (sessionFilter) {
      list = list.filter((a) => a.source_session_id === sessionFilter);
    }
    if (search.trim()) {
      const q = search.trim().toLowerCase();
      list = list.filter(
        (a) =>
          a.title.toLowerCase().includes(q) ||
          a.source_session_title.toLowerCase().includes(q) ||
          a.source_artifact_key.toLowerCase().includes(q)
      );
    }
    return list;
  }, [articles, sessionFilter, search]);

  const activeArticle = useMemo(() => {
    if (!selectedArticleId) return null;
    return articles.find((a) => a.id === selectedArticleId);
  }, [selectedArticleId, articles]);

  useEffect(() => {
    if (activeTab !== "published") return;
    if (filteredArticles.length === 0) {
      setSelectedArticleId(null);
      return;
    }
    const stillExists = filteredArticles.some((a) => a.id === selectedArticleId);
    if (!stillExists) {
      setSelectedArticleId(filteredArticles[0].id);
    }
  }, [activeTab, filteredArticles, selectedArticleId]);

  // ---------- 会话选项（双 tab 共用） ----------
  const sessionOptions = useMemo(() => {
    const map = new Map(); // session_id -> session_title
    for (const d of drafts) {
      if (!map.has(d.session_id)) map.set(d.session_id, d.session_title);
    }
    for (const a of articles) {
      if (a.source_session_id && !map.has(a.source_session_id)) {
        map.set(a.source_session_id, a.source_session_title);
      }
    }
    return Array.from(map.entries()).map(([id, title]) => ({ id, title }));
  }, [drafts, articles]);

  // ---------- 删除回调 ----------
  const handleDraftDeleted = useCallback(async () => {
    setSelectedDraftKey(null);
    await fetchAll();
  }, [fetchAll]);

  const handleArticleDeleted = useCallback(async () => {
    setSelectedArticleId(null);
    await fetchAll();
  }, [fetchAll]);

  // ---------- Publish 成功 ----------
  const handlePublished = useCallback(
    async (newArticle) => {
      await fetchAll();
      setActiveTab("published");
      setSelectedArticleId(newArticle.id);
      setRefreshArticleId((k) => k + 1);
    },
    [fetchAll]
  );

  return (
    <div className="flex flex-col h-full overflow-hidden bg-background">
      {/* 顶部 Tab + 工具栏 */}
      <div className="border-b border-border shrink-0 px-4 pt-3 pb-2 bg-card/40">
        <div className="flex items-center justify-between gap-2 mb-2">
          <div className="flex items-center gap-1">
            <TabButton
              active={activeTab === "drafts"}
              onClick={() => setActiveTab("drafts")}
              icon={<FileText size={14} />}
              label="草稿"
              count={drafts.length}
            />
            <TabButton
              active={activeTab === "published"}
              onClick={() => setActiveTab("published")}
              icon={<Bookmark size={14} />}
              label="已发布"
              count={articles.length}
            />
          </div>
          <div className="flex items-center gap-1">
            {loading && (
              <Loader2
                size={14}
                className="animate-spin text-muted-foreground"
              />
            )}
            <button
              onClick={fetchAll}
              className="p-1.5 rounded hover:bg-accent text-muted-foreground"
              title="刷新"
            >
              <RefreshCw size={14} />
            </button>
          </div>
        </div>

        {/* 搜索 + 会话筛选 */}
        <div className="flex items-center gap-2">
          <div className="relative flex-1 max-w-sm">
            <Search
              size={12}
              className="absolute left-2 top-1/2 -translate-y-1/2 text-muted-foreground"
            />
            <input
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="按标题、会话名或 key 搜索..."
              className="w-full pl-7 pr-2 py-1.5 text-xs rounded border border-border bg-background focus:outline-none focus:ring-1 focus:ring-primary"
            />
          </div>
          <select
            value={sessionFilter}
            onChange={(e) => setSessionFilter(e.target.value)}
            className="text-xs px-2 py-1.5 rounded border border-border bg-background max-w-[200px] truncate"
          >
            <option value="">全部会话</option>
            {sessionOptions.map((s) => (
              <option key={s.id} value={s.id}>
                {s.title}
              </option>
            ))}
          </select>
        </div>
      </div>

      {error && (
        <div className="px-4 py-2 text-xs text-red-600 bg-red-50 border-b border-red-100 shrink-0">
          {error}
        </div>
      )}

      {/* 主区：两栏 */}
      <div className="flex-1 flex overflow-hidden">
        {/* 左栏：列表 */}
        <div className="w-80 border-r border-border overflow-y-auto bg-card/20 shrink-0">
          {activeTab === "drafts" ? (
            <DraftList
              drafts={filteredDrafts}
              selectedKey={selectedDraftKey}
              onSelect={(d) =>
                setSelectedDraftKey(`${d.session_id}::${d.artifact_key}`)
              }
              loading={loading}
              isFiltered={!!search || !!sessionFilter}
            />
          ) : (
            <ArticleList
              articles={filteredArticles}
              selectedId={selectedArticleId}
              onSelect={(a) => setSelectedArticleId(a.id)}
              loading={loading}
              isFiltered={!!search || !!sessionFilter}
            />
          )}
        </div>

        {/* 右栏：详情 */}
        <div className="flex-1 flex flex-col overflow-hidden">
          {activeTab === "drafts" && (
            <DraftDetail
              draft={activeDraft}
              onDeleted={handleDraftDeleted}
              onPublishClick={() =>
                activeDraft &&
                setPublishDialog({
                  sessionId: activeDraft.session_id,
                  artifactKey: activeDraft.artifact_key,
                  title: activeDraft.title,
                })
              }
              onGotoQA={() => {
                if (!activeDraft) return;
                navigate(
                  `/qa/${activeDraft.session_id}?artifact=${encodeURIComponent(
                    activeDraft.artifact_key
                  )}`
                );
              }}
            />
          )}
          {activeTab === "published" && (
            <ArticleView
              articleId={selectedArticleId}
              refreshTrigger={refreshArticleId}
              onDeleted={handleArticleDeleted}
            />
          )}
        </div>
      </div>

      {/* Publish 弹窗 */}
      {publishDialog && (
        <PublishDialog
          isOpen
          sessionId={publishDialog.sessionId}
          artifactKey={publishDialog.artifactKey}
          artifactTitle={publishDialog.title}
          onClose={() => setPublishDialog(null)}
          onPublished={handlePublished}
        />
      )}
    </div>
  );
}

// ---------- 子组件 ----------

function TabButton({ active, onClick, icon, label, count }) {
  return (
    <button
      onClick={onClick}
      className={`inline-flex items-center gap-1.5 px-3 py-1.5 rounded text-sm transition-colors ${
        active
          ? "bg-primary/10 text-primary font-medium"
          : "text-muted-foreground hover:bg-accent"
      }`}
    >
      {icon}
      {label}
      <span
        className={`text-[10px] px-1.5 py-0 rounded ${
          active ? "bg-primary/20" : "bg-muted"
        }`}
      >
        {count}
      </span>
    </button>
  );
}

function DraftList({ drafts, selectedKey, onSelect, loading, isFiltered }) {
  if (loading && drafts.length === 0) {
    return (
      <div className="p-6 text-xs text-muted-foreground inline-flex items-center gap-2">
        <Loader2 size={12} className="animate-spin" />
        加载中...
      </div>
    );
  }
  if (drafts.length === 0) {
    return (
      <div className="p-6 text-center text-muted-foreground">
        <FileText size={32} className="mx-auto mb-2 opacity-40" />
        <p className="text-sm">
          {isFiltered ? "没有匹配的草稿" : "还没有任何 artifact"}
        </p>
        {!isFiltered && (
          <p className="text-xs mt-1 leading-relaxed">
            在 QA 中向 Agent 请求"梳理 / 汇总 / 报告"等长篇结构化产出，
            Agent 会主动生成 artifact。
          </p>
        )}
      </div>
    );
  }
  return (
    <ul className="divide-y divide-border">
      {drafts.map((d) => {
        const key = `${d.session_id}::${d.artifact_key}`;
        const isActive = key === selectedKey;
        return (
          <li key={key}>
            <button
              onClick={() => onSelect(d)}
              className={`w-full text-left px-3 py-2.5 hover:bg-accent/50 transition-colors ${
                isActive ? "bg-accent" : ""
              }`}
            >
              <div className="flex items-start justify-between gap-2 mb-1">
                <div className="font-medium text-sm truncate flex-1 min-w-0">
                  {d.title}
                </div>
                <span className="text-[10px] font-mono text-muted-foreground shrink-0">
                  v{d.current_version}
                </span>
              </div>
              <div className="text-xs text-muted-foreground truncate mb-1">
                {d.session_title}
              </div>
              <div className="flex items-center gap-2 text-[10px] text-muted-foreground">
                <span>{relativeTime(d.updated_at)}</span>
                {d.publication_count > 0 && (
                  <span
                    className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded bg-amber-50 text-amber-700 border border-amber-100"
                    title={`已发布 ${d.publication_count} 次`}
                  >
                    <Bookmark size={9} />×{d.publication_count}
                  </span>
                )}
              </div>
              {d.content_preview && (
                <div className="text-[11px] text-muted-foreground line-clamp-2 mt-1.5 leading-relaxed">
                  {d.content_preview}
                </div>
              )}
            </button>
          </li>
        );
      })}
    </ul>
  );
}

function ArticleList({ articles, selectedId, onSelect, loading, isFiltered }) {
  if (loading && articles.length === 0) {
    return (
      <div className="p-6 text-xs text-muted-foreground inline-flex items-center gap-2">
        <Loader2 size={12} className="animate-spin" />
        加载中...
      </div>
    );
  }
  if (articles.length === 0) {
    return (
      <div className="p-6 text-center text-muted-foreground">
        <Bookmark size={32} className="mx-auto mb-2 opacity-40" />
        <p className="text-sm">
          {isFiltered ? "没有匹配的文章" : "文章库还是空的"}
        </p>
        {!isFiltered && (
          <p className="text-xs mt-1 leading-relaxed">
            从「草稿」Tab 选一篇 artifact，点「🔖 发布」即可加入文章库。
          </p>
        )}
      </div>
    );
  }
  return (
    <ul className="divide-y divide-border">
      {articles.map((a) => {
        const isActive = a.id === selectedId;
        return (
          <li key={a.id}>
            <button
              onClick={() => onSelect(a)}
              className={`w-full text-left px-3 py-2.5 hover:bg-accent/50 transition-colors ${
                isActive ? "bg-accent" : ""
              }`}
            >
              <div className="flex items-start justify-between gap-2 mb-1">
                <div className="font-medium text-sm truncate flex-1 min-w-0">
                  {a.title}
                </div>
                <span
                  className="text-[10px] font-mono text-muted-foreground shrink-0"
                  title={`基于源 artifact 第 ${a.source_version_number} 版`}
                >
                  v{a.source_version_number}
                </span>
              </div>
              <div
                className={`text-xs truncate mb-1 ${
                  a.source_exists
                    ? "text-muted-foreground"
                    : "text-muted-foreground/60 line-through"
                }`}
                title={a.source_exists ? "" : "源会话或源 artifact 已删除"}
              >
                {a.source_session_title}
              </div>
              <div className="text-[10px] text-muted-foreground">
                生成于 {relativeTime(a.content_created_at)}
              </div>
              {a.content_preview && (
                <div className="text-[11px] text-muted-foreground line-clamp-2 mt-1.5 leading-relaxed">
                  {a.content_preview}
                </div>
              )}
            </button>
          </li>
        );
      })}
    </ul>
  );
}

/**
 * 草稿详情区：用 ArtifactView，并通过 extraToolbarActions 注入「发布」按钮，
 * headerExtra 注入「去 QA 会话」链接。
 */
function DraftDetail({ draft, onDeleted, onPublishClick, onGotoQA }) {
  if (!draft) {
    return (
      <div className="flex items-center justify-center flex-1 text-muted-foreground text-sm">
        请选择一个 artifact
      </div>
    );
  }
  return (
    <ArtifactView
      sessionId={draft.session_id}
      artifactKey={draft.artifact_key}
      onDeleted={onDeleted}
      headerExtra={
        <button
          onClick={onGotoQA}
          className="inline-flex items-center gap-0.5 text-xs px-2 py-1 rounded border border-border hover:bg-accent text-muted-foreground hover:text-foreground"
          title="跳转到对应 QA 会话"
        >
          去 QA 会话
          <ArrowUpRight size={11} />
        </button>
      }
      extraToolbarActions={
        <button
          onClick={onPublishClick}
          className="inline-flex items-center gap-1 text-xs px-2 py-1 rounded bg-primary/10 text-primary hover:bg-primary/20"
          title="发布到文章库（生成一份冻结快照）"
        >
          <Bookmark size={12} />
          发布
        </button>
      }
    />
  );
}

// ---------- 工具 ----------

function relativeTime(ts) {
  if (!ts) return "";
  const d = new Date(ts).getTime();
  const now = Date.now();
  const diff = (now - d) / 1000;
  if (diff < 60) return "刚刚";
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  if (diff < 86400 * 30) return `${Math.floor(diff / 86400)} 天前`;
  if (diff < 86400 * 365) return `${Math.floor(diff / (86400 * 30))} 个月前`;
  return `${Math.floor(diff / (86400 * 365))} 年前`;
}
