import { useEffect, useState } from "react";
import { Save, CheckCircle2 } from "lucide-react";
import { getSettings, updateSettings } from "@/lib/api";

const MODEL_OPTIONS = {
  llm: [
    "qwen3.5-flash",
    "qwen3.5-plus",
    "qwen3.6-plus",
  ],
  llm_qa: [
    "qwen3.5-flash",
    "qwen3.5-plus",
    "qwen3.6-plus",
    "kimi-k2.6",
  ],
  embedding: ["text-embedding-v4"],
  rerank: ["qwen3-rerank"],
};

export default function Settings() {
  const [form, setForm] = useState({
    dashscope_api_key: "",
    moonshot_api_key: "",
    llm_model_map: "qwen3.5-plus",
    llm_model_qa: "qwen3.6-plus",
    embedding_model: "text-embedding-v4",
    rerank_model: "qwen3-rerank",
  });
  const [hasKey, setHasKey] = useState(false);
  const [hasMoonshotKey, setHasMoonshotKey] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    getSettings().then((data) => {
      setForm((prev) => ({
        ...prev,
        llm_model_map: data.llm_model_map,
        llm_model_qa: data.llm_model_qa,
        embedding_model: data.embedding_model,
        rerank_model: data.rerank_model,
      }));
      setHasKey(data.has_api_key);
      setHasMoonshotKey(data.has_moonshot_key);
    });
  }, []);

  const handleSave = async () => {
    setSaving(true);
    setSaved(false);
    try {
      const payload = { ...form };
      if (!payload.dashscope_api_key) {
        delete payload.dashscope_api_key;
      }
      if (!payload.moonshot_api_key) {
        delete payload.moonshot_api_key;
      }
      await updateSettings(payload);
      setSaved(true);
      if (form.dashscope_api_key) setHasKey(true);
      if (form.moonshot_api_key) setHasMoonshotKey(true);
      setTimeout(() => setSaved(false), 3000);
    } catch {
      alert("保存失败");
    } finally {
      setSaving(false);
    }
  };

  const update = (key, value) => setForm((p) => ({ ...p, [key]: value }));

  return (
    <div className="max-w-2xl">
      <h1 className="text-2xl font-bold mb-6">设置</h1>

      <div className="space-y-6">
        {/* API Key */}
        <section className="bg-card border border-border rounded-lg p-4">
          <h2 className="text-sm font-semibold mb-3">API 配置</h2>
          <div>
            <label className="block text-sm text-muted-foreground mb-1">
              DashScope API Key
            </label>
            <input
              type="password"
              value={form.dashscope_api_key}
              onChange={(e) => update("dashscope_api_key", e.target.value)}
              placeholder={hasKey ? "已配置（留空保持不变）" : "sk-..."}
              className="w-full border border-border rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
            />
            {hasKey && (
              <p className="text-xs text-green-600 mt-1">✓ API Key 已配置</p>
            )}
          </div>
          <div className="mt-3">
            <label className="block text-sm text-muted-foreground mb-1">
              Moonshot API Key <span className="text-xs">（Kimi K2.6 需要）</span>
            </label>
            <input
              type="password"
              value={form.moonshot_api_key}
              onChange={(e) => update("moonshot_api_key", e.target.value)}
              placeholder={hasMoonshotKey ? "已配置（留空保持不变）" : "sk-..."}
              className="w-full border border-border rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
            />
            {hasMoonshotKey && (
              <p className="text-xs text-green-600 mt-1">✓ Moonshot Key 已配置</p>
            )}
          </div>
        </section>

        {/* LLM 模型 */}
        <section className="bg-card border border-border rounded-lg p-4">
          <h2 className="text-sm font-semibold mb-3">LLM 模型</h2>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <label className="block text-sm text-muted-foreground mb-1">
                话题切分模型
              </label>
              <select
                value={form.llm_model_map}
                onChange={(e) => update("llm_model_map", e.target.value)}
                className="w-full border border-border rounded-md px-3 py-2 text-sm"
              >
                {MODEL_OPTIONS.llm.map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </select>
              <p className="text-xs text-muted-foreground mt-1">
                话题切分大量调用，建议选便宜的
              </p>
            </div>
            <div>
              <label className="block text-sm text-muted-foreground mb-1">
                问答模型
              </label>
              <select
                value={form.llm_model_qa}
                onChange={(e) => update("llm_model_qa", e.target.value)}
                className="w-full border border-border rounded-md px-3 py-2 text-sm"
              >
                {MODEL_OPTIONS.llm_qa.map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </select>
              <p className="text-xs text-muted-foreground mt-1">
                kimi-k2.6 需配置 Moonshot Key
              </p>
            </div>
          </div>
        </section>

        {/* Embedding / Rerank */}
        <section className="bg-card border border-border rounded-lg p-4">
          <h2 className="text-sm font-semibold mb-3">Embedding & Rerank</h2>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <label className="block text-sm text-muted-foreground mb-1">
                Embedding 模型
              </label>
              <select
                value={form.embedding_model}
                onChange={(e) => update("embedding_model", e.target.value)}
                className="w-full border border-border rounded-md px-3 py-2 text-sm"
              >
                {MODEL_OPTIONS.embedding.map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-sm text-muted-foreground mb-1">
                Rerank 模型
              </label>
              <select
                value={form.rerank_model}
                onChange={(e) => update("rerank_model", e.target.value)}
                className="w-full border border-border rounded-md px-3 py-2 text-sm"
              >
                {MODEL_OPTIONS.rerank.map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </select>
            </div>
          </div>
        </section>

        {/* 保存按钮 */}
        <div className="flex items-center gap-3">
          <button
            onClick={handleSave}
            disabled={saving}
            className="inline-flex items-center gap-2 bg-primary text-primary-foreground px-4 py-2 rounded-md text-sm hover:opacity-90 disabled:opacity-50"
          >
            <Save size={14} />
            {saving ? "保存中..." : "保存设置"}
          </button>
          {saved && (
            <span className="inline-flex items-center gap-1 text-sm text-green-600">
              <CheckCircle2 size={14} />
              已保存
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
