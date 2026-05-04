import axios from "axios";

const api = axios.create({
  baseURL: "/api",
  timeout: 120000,
});

// ---------- Import ----------

/**
 * 上传文件触发后台导入。
 *
 * 后端改成异步任务：
 * - 立即返回 {status: "started", task_id}
 * - 真正进度由 getImportProgress() 轮询
 *
 * 上传本身（multipart 大文件）依然要等服务端把文件写到临时目录才会返回，
 * 所以保留较大的 timeout（10 分钟）以应对几百 MB 的 export.json。
 */
export async function importChat(file) {
  const formData = new FormData();
  formData.append("file", file);
  const { data } = await api.post("/import", formData, {
    headers: { "Content-Type": "multipart/form-data" },
    timeout: 600000,
  });
  return data;
}

export async function getImportProgress() {
  const { data } = await api.get("/import-progress");
  return data;
}

export async function getIndexProgress() {
  const { data } = await api.get("/index-progress");
  return data;
}

export async function rebuildIndex(chatId, force = false) {
  const { data } = await api.post(`/rebuild-index/${chatId}?force=${force}`);
  return data;
}

export async function rebuildAllIndex(force = false) {
  const { data } = await api.post(`/rebuild-index-all?force=${force}`);
  return data;
}

export async function getChats() {
  const { data } = await api.get("/chats");
  return data;
}

// ---------- Watched Folders ----------

export async function validateFolder(path) {
  const { data } = await api.post("/folders/validate", { path });
  return data;
}

export async function listFolders() {
  const { data } = await api.get("/folders");
  return data;
}

export async function addFolder(path, alias) {
  const { data } = await api.post("/folders", { path, alias: alias || null });
  return data;
}

export async function deleteFolder(folderId) {
  const { data } = await api.delete(`/folders/${folderId}`);
  return data;
}

export async function scanFolder(folderId) {
  const { data } = await api.post(`/folders/${folderId}/scan`, null, {
    timeout: 0, // 大目录扫描可能很久，禁用超时
  });
  return data;
}

export async function getChatStats(chatId) {
  const { data } = await api.get(`/chats/${chatId}/stats`);
  return data;
}

// ---------- Messages ----------

export async function getMessages(params) {
  const { data } = await api.get("/messages", { params });
  return data;
}

// ---------- QA: 启动 Run ----------

/**
 * 启动一个 Agent 模式 run，立刻返回 {run_id, session_id, title, already_running}。
 */
export async function startAgentRun(question, options = {}) {
  const { data } = await api.post("/ask/agent", {
    question,
    session_id: options.sessionId || null,
    mode: "agent",
    chat_ids: options.chatIds || null,
    date_range: options.dateRange || null,
    sender: options.sender || null,
  });
  return data;
}

/**
 * 启动一个 RAG 模式 run。
 */
export async function startRagRun(question, options = {}) {
  const { data } = await api.post("/ask/stream", {
    question,
    session_id: options.sessionId || null,
    mode: "rag",
    chat_ids: options.chatIds || null,
    date_range: options.dateRange || null,
    sender: options.sender || null,
  });
  return data;
}

/**
 * 订阅一个 run 的 SSE 事件流。
 *
 * @param {string} runId
 * @param {object} options - { lastEventId?, signal?, onEvent? }
 *   - lastEventId: 从此 seq 之后续播（默认 -1 = 从头开始）
 *   - signal: AbortSignal
 *   - onEvent(ev): 每收到一个事件回调
 *
 * 收到 `{type: "__end__", status}` 表示流结束。
 */
export async function streamRunEvents(runId, options = {}) {
  const lastEventId = options.lastEventId ?? -1;
  const url = `/api/runs/${encodeURIComponent(runId)}/events?last_event_id=${lastEventId}`;

  const resp = await fetch(url, {
    method: "GET",
    headers: { Accept: "text/event-stream" },
    signal: options.signal,
  });

  if (!resp.ok) {
    const errText = await resp.text().catch(() => "");
    const err = new Error(`HTTP ${resp.status}: ${errText}`);
    err.status = resp.status;
    throw err;
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let idx;
    while ((idx = buffer.indexOf("\n\n")) !== -1) {
      const rawEvent = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);

      // 取出 data: 部分（可能多行）
      const dataLines = rawEvent
        .split("\n")
        .filter((l) => l.startsWith("data:"))
        .map((l) => l.slice(5).trimStart());
      if (dataLines.length === 0) continue;
      const dataStr = dataLines.join("\n");
      if (!dataStr) continue;

      try {
        const ev = JSON.parse(dataStr);
        options.onEvent?.(ev);
        // 收到 __end__ 后服务端会主动关闭流，下一轮 read 返回 done
      } catch (e) {
        console.warn("SSE parse error:", e, dataStr);
      }
    }
  }
}

export async function abortRun(runId) {
  const { data } = await api.post(`/runs/${encodeURIComponent(runId)}/abort`);
  return data;
}

export async function listActiveRuns() {
  const { data } = await api.get("/runs/active");
  return data;
}

export async function getSessionActiveRun(sessionId) {
  try {
    const { data } = await api.get(`/sessions/${encodeURIComponent(sessionId)}/active-run`);
    return data;
  } catch (err) {
    if (err.response?.status === 404) return null;
    throw err;
  }
}

// ---------- Sessions ----------

export async function createSession(payload = {}) {
  const { data } = await api.post("/sessions", {
    title: payload.title || null,
    mode: payload.mode || "agent",
    chat_ids: payload.chatIds || null,
  });
  return data;
}

export async function listSessions(params = {}) {
  const { data } = await api.get("/sessions", {
    params: {
      archived: params.archived ?? false,
      pinned: params.pinned,
      q: params.q || undefined,
      limit: params.limit ?? 30,
      offset: params.offset ?? 0,
    },
  });
  return data;
}

export async function getSession(sessionId) {
  const { data } = await api.get(`/sessions/${encodeURIComponent(sessionId)}`);
  return data;
}

export async function patchSession(sessionId, fields) {
  const { data } = await api.patch(`/sessions/${encodeURIComponent(sessionId)}`, fields);
  return data;
}

export async function deleteSession(sessionId) {
  const { data } = await api.delete(`/sessions/${encodeURIComponent(sessionId)}`);
  return data;
}

export async function autotitleSession(sessionId) {
  const { data } = await api.post(`/sessions/${encodeURIComponent(sessionId)}/autotitle`);
  return data;
}

export function exportSessionUrl(sessionId, format = "md") {
  return `/api/sessions/${encodeURIComponent(sessionId)}/export?format=${format}`;
}

// ---------- Artifacts ----------

export async function listArtifacts(sessionId) {
  const { data } = await api.get(
    `/sessions/${encodeURIComponent(sessionId)}/artifacts`
  );
  return data;
}

export async function getArtifact(sessionId, artifactKey, version = null) {
  const params = version != null ? { version } : {};
  const { data } = await api.get(
    `/sessions/${encodeURIComponent(sessionId)}/artifacts/${encodeURIComponent(artifactKey)}`,
    { params }
  );
  return data;
}

export async function listArtifactVersions(sessionId, artifactKey) {
  const { data } = await api.get(
    `/sessions/${encodeURIComponent(sessionId)}/artifacts/${encodeURIComponent(artifactKey)}/versions`
  );
  return data;
}

export async function deleteArtifact(sessionId, artifactKey) {
  const { data } = await api.delete(
    `/sessions/${encodeURIComponent(sessionId)}/artifacts/${encodeURIComponent(artifactKey)}`
  );
  return data;
}

export function exportArtifactUrl(sessionId, artifactKey, version = null) {
  const base = `/api/sessions/${encodeURIComponent(sessionId)}/artifacts/${encodeURIComponent(artifactKey)}/export`;
  return version != null ? `${base}?version=${version}` : base;
}

// ---------- Articles (Published Library) ----------

/** 跨 session 草稿总览：所有 artifact + publication_count。 */
export async function listDrafts() {
  const { data } = await api.get("/articles/drafts");
  return data;
}

/** 跨 session 已发布文章列表（按生成时间倒序）。 */
export async function listArticles() {
  const { data } = await api.get("/articles");
  return data;
}

/** 取一篇已发布文章的完整内容。 */
export async function getArticle(articleId) {
  const { data } = await api.get(`/articles/${encodeURIComponent(articleId)}`);
  return data;
}

/** 从文章库撤回一篇文章（不影响源 artifact）。 */
export async function deleteArticle(articleId) {
  const { data } = await api.delete(
    `/articles/${encodeURIComponent(articleId)}`
  );
  return data;
}

/** 导出单篇文章为 .md 下载链接。 */
export function exportArticleUrl(articleId) {
  return `/api/articles/${encodeURIComponent(articleId)}/export`;
}

/** 查询 artifact 已发布过哪些文章（PublishDialog 决定追加/覆盖时用）。 */
export async function getArtifactPublications(sessionId, artifactKey) {
  const { data } = await api.get(
    `/sessions/${encodeURIComponent(sessionId)}/artifacts/${encodeURIComponent(artifactKey)}/publications`
  );
  return data;
}

/**
 * 发布 artifact 到文章库。
 * @param {object} opts
 * @param {"append"|"overwrite"} opts.mode 默认 append
 * @param {string|null} opts.targetArticleId overwrite 时必填
 */
export async function publishArtifact(sessionId, artifactKey, { mode = "append", targetArticleId = null } = {}) {
  const { data } = await api.post(
    `/sessions/${encodeURIComponent(sessionId)}/artifacts/${encodeURIComponent(artifactKey)}/publish`,
    { mode, target_article_id: targetArticleId }
  );
  return data;
}

// ---------- Settings ----------

export async function getSettings() {
  const { data } = await api.get("/settings");
  return data;
}

export async function updateSettings(settings) {
  const { data } = await api.put("/settings", settings);
  return data;
}

// ---------- Telegram 直连同步 ----------

export async function getTelegramAccount() {
  const { data } = await api.get("/telegram/account");
  return data;
}

export async function configureTelegramAccount({ apiId, apiHash, phone }) {
  const { data } = await api.post("/telegram/account", {
    api_id: Number(apiId),
    api_hash: apiHash,
    phone,
  });
  return data;
}

export async function deleteTelegramAccount() {
  const { data } = await api.delete("/telegram/account");
  return data;
}

export async function telegramSendCode() {
  const { data } = await api.post("/telegram/login/send-code");
  return data;
}

export async function telegramVerifyCode(code, password = null) {
  const { data } = await api.post("/telegram/login/verify", { code, password });
  return data;
}

export async function listTelegramDialogs() {
  const { data } = await api.get("/telegram/dialogs", { timeout: 0 });
  return data;
}

export async function startTelegramSync(chatIds) {
  const { data } = await api.post("/telegram/sync", { chat_ids: chatIds });
  return data;
}

export async function getTelegramSyncProgress() {
  const { data } = await api.get("/telegram/sync/progress");
  return data;
}

export async function abortTelegramSync() {
  const { data } = await api.post("/telegram/sync/abort");
  return data;
}

export default api;
