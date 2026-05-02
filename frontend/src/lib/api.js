import axios from "axios";

const api = axios.create({
  baseURL: "/api",
  timeout: 120000,
});

// ---------- Import ----------

export async function importChat(file) {
  const formData = new FormData();
  formData.append("file", file);
  const { data } = await api.post("/import", formData, {
    headers: { "Content-Type": "multipart/form-data" },
  });
  return data;
}

export async function getIndexProgress() {
  const { data } = await api.get("/index-progress");
  return data;
}

export async function rebuildIndex(chatId) {
  const { data } = await api.post(`/rebuild-index/${chatId}`);
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

export async function getChatStats(chatId) {
  const { data } = await api.get(`/chats/${chatId}/stats`);
  return data;
}

// ---------- Messages ----------

export async function getMessages(params) {
  const { data } = await api.get("/messages", { params });
  return data;
}

// ---------- Summary ----------

export async function triggerSummarize(chatId, force = false) {
  const { data } = await api.post("/summarize", { chat_id: chatId, force });
  return data;
}

export async function triggerSummarizeAll(force = false) {
  const { data } = await api.post(`/summarize-all?force=${force}`);
  return data;
}

export async function getSummaries(chatId) {
  const { data } = await api.get(`/summaries/${chatId}`);
  return data;
}

export async function getSummaryProgress() {
  const { data } = await api.get("/summary-progress");
  return data;
}

// ---------- QA ----------

export async function askQuestion(question, options = {}) {
  const { data } = await api.post("/ask", {
    question,
    chat_ids: options.chatIds || null,
    date_range: options.dateRange || null,
    sender: options.sender || null,
  });
  return data;
}

/**
 * 流式 RAG 问答。逐事件推送 RAG 各阶段状态。
 * @param {string} question
 * @param {object} options - { chatIds, dateRange, sender, signal, onEvent }
 *   - onEvent(ev): 每收到一个事件回调，ev 形如 { type, ... }
 */
export async function askQuestionStream(question, options = {}) {
  const body = {
    question,
    chat_ids: options.chatIds || null,
    date_range: options.dateRange || null,
    sender: options.sender || null,
  };

  const resp = await fetch("/api/ask/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: options.signal,
  });

  if (!resp.ok) {
    const errText = await resp.text();
    throw new Error(`HTTP ${resp.status}: ${errText}`);
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // 按 SSE 事件分隔（\n\n）
    let idx;
    while ((idx = buffer.indexOf("\n\n")) !== -1) {
      const rawEvent = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);

      const dataLine = rawEvent
        .split("\n")
        .filter((l) => l.startsWith("data:"))
        .map((l) => l.slice(5).trim())
        .join("");

      if (!dataLine) continue;
      try {
        const ev = JSON.parse(dataLine);
        options.onEvent?.(ev);
      } catch (e) {
        console.warn("SSE parse error:", e, dataLine);
      }
    }
  }
}

/**
 * Agent 式问答：LLM 自主调用工具。事件类型更丰富。
 * @param {string} question
 * @param {object} options - { chatIds, signal, onEvent }
 */
export async function askAgentStream(question, options = {}) {
  const body = {
    question,
    chat_ids: options.chatIds || null,
    history: options.history || null,
  };

  const resp = await fetch("/api/ask/agent", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: options.signal,
  });

  if (!resp.ok) {
    throw new Error(`HTTP ${resp.status}: ${await resp.text()}`);
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
      const dataLine = rawEvent
        .split("\n")
        .filter((l) => l.startsWith("data:"))
        .map((l) => l.slice(5).trim())
        .join("");
      if (!dataLine) continue;
      try {
        const ev = JSON.parse(dataLine);
        options.onEvent?.(ev);
      } catch (e) {
        console.warn("SSE parse error:", e, dataLine);
      }
    }
  }
}

export async function getQAHistory(limit = 50) {
  const { data } = await api.get("/ask/history", { params: { limit } });
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

export default api;
