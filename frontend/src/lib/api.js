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
