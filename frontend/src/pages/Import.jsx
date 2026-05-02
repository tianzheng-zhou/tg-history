import { useCallback, useState } from "react";
import { Upload, CheckCircle2, AlertCircle, FileJson } from "lucide-react";
import { importChat, getChats } from "@/lib/api";

export default function Import() {
  const [file, setFile] = useState(null);
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [recentImports, setRecentImports] = useState([]);

  const loadImports = useCallback(() => {
    getChats().then(setRecentImports).catch(() => {});
  }, []);

  useState(() => {
    loadImports();
  });

  const handleDrop = useCallback((e) => {
    e.preventDefault();
    const f = e.dataTransfer?.files?.[0];
    if (f && f.name.endsWith(".json")) {
      setFile(f);
      setResult(null);
      setError(null);
    }
  }, []);

  const handleFileSelect = (e) => {
    const f = e.target.files?.[0];
    if (f) {
      setFile(f);
      setResult(null);
      setError(null);
    }
  };

  const handleImport = async () => {
    if (!file) return;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const data = await importChat(file);
      setResult(data);
      setFile(null);
      loadImports();
    } catch (err) {
      setError(err.response?.data?.detail || "导入失败，请检查文件格式");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div>
      <h1 className="text-2xl font-bold mb-6">数据导入</h1>

      {/* 拖拽上传区域 */}
      <div
        onDrop={handleDrop}
        onDragOver={(e) => e.preventDefault()}
        className="border-2 border-dashed border-border rounded-lg p-12 text-center hover:border-primary/50 transition-colors cursor-pointer"
        onClick={() => document.getElementById("file-input").click()}
      >
        <input
          id="file-input"
          type="file"
          accept=".json"
          className="hidden"
          onChange={handleFileSelect}
        />
        <Upload size={40} className="mx-auto mb-4 text-muted-foreground" />
        <p className="text-lg font-medium mb-1">
          拖拽或点击上传 Telegram 导出的 JSON 文件
        </p>
        <p className="text-sm text-muted-foreground">
          支持 Telegram Desktop 导出的 result.json 格式
        </p>
      </div>

      {/* 已选文件 */}
      {file && (
        <div className="mt-4 flex items-center justify-between bg-card border border-border rounded-lg p-4">
          <div className="flex items-center gap-3">
            <FileJson size={20} className="text-primary" />
            <div>
              <p className="text-sm font-medium">{file.name}</p>
              <p className="text-xs text-muted-foreground">
                {(file.size / 1024 / 1024).toFixed(2)} MB
              </p>
            </div>
          </div>
          <button
            onClick={handleImport}
            disabled={loading}
            className="bg-primary text-primary-foreground px-4 py-2 rounded-md text-sm hover:opacity-90 transition-opacity disabled:opacity-50"
          >
            {loading ? "导入中..." : "开始导入"}
          </button>
        </div>
      )}

      {/* 导入结果 */}
      {result && (
        <div className="mt-4 bg-green-50 border border-green-200 rounded-lg p-4 flex items-start gap-3">
          <CheckCircle2 size={20} className="text-green-600 mt-0.5" />
          <div>
            <p className="font-medium text-green-800">导入成功!</p>
            <p className="text-sm text-green-700 mt-1">
              群聊: {result.chat_name} · 新增 {result.message_count} 条消息 ·
              时间范围: {result.date_range}
            </p>
          </div>
        </div>
      )}

      {/* 错误提示 */}
      {error && (
        <div className="mt-4 bg-red-50 border border-red-200 rounded-lg p-4 flex items-start gap-3">
          <AlertCircle size={20} className="text-red-600 mt-0.5" />
          <div>
            <p className="font-medium text-red-800">导入失败</p>
            <p className="text-sm text-red-700 mt-1">{error}</p>
          </div>
        </div>
      )}

      {/* 已导入列表 */}
      {recentImports.length > 0 && (
        <div className="mt-8">
          <h2 className="text-lg font-semibold mb-3">已导入的群聊</h2>
          <div className="border border-border rounded-lg overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-secondary">
                <tr>
                  <th className="text-left px-4 py-2 font-medium">群聊名称</th>
                  <th className="text-left px-4 py-2 font-medium">消息数</th>
                  <th className="text-left px-4 py-2 font-medium">时间范围</th>
                  <th className="text-left px-4 py-2 font-medium">导入时间</th>
                </tr>
              </thead>
              <tbody>
                {recentImports.map((imp) => (
                  <tr key={imp.chat_id} className="border-t border-border">
                    <td className="px-4 py-2.5 font-medium">{imp.chat_name}</td>
                    <td className="px-4 py-2.5 text-muted-foreground">
                      {imp.message_count.toLocaleString()}
                    </td>
                    <td className="px-4 py-2.5 text-muted-foreground">
                      {imp.date_range}
                    </td>
                    <td className="px-4 py-2.5 text-muted-foreground">
                      {new Date(imp.imported_at).toLocaleString("zh-CN")}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
