import { useCallback, useEffect, useRef, useState } from "react";
import { Upload, CheckCircle2, AlertCircle, FileJson, Loader2, Database } from "lucide-react";
import { importChat, getChats, getIndexProgress } from "@/lib/api";

export default function Import() {
  const [file, setFile] = useState(null);
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [recentImports, setRecentImports] = useState([]);
  const [indexProgress, setIndexProgress] = useState(null);
  const pollRef = useRef(null);

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

  const startPolling = useCallback(() => {
    if (pollRef.current) clearInterval(pollRef.current);
    pollRef.current = setInterval(async () => {
      try {
        const prog = await getIndexProgress();
        setIndexProgress(prog);
        if (!prog.running) {
          clearInterval(pollRef.current);
          pollRef.current = null;
          loadImports();
        }
      } catch {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
    }, 1500);
  }, [loadImports]);

  useEffect(() => {
    // 页面加载时检查是否有正在进行的索引构建
    getIndexProgress().then((prog) => {
      if (prog.running || prog.completed > 0) {
        setIndexProgress(prog);
        if (prog.running) startPolling();
      }
    }).catch(() => {});
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
  }, [startPolling]);

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
      startPolling();
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
      {result && result.length > 0 && (
        <div className="mt-4 bg-green-50 border border-green-200 rounded-lg p-4 flex items-start gap-3">
          <CheckCircle2 size={20} className="text-green-600 mt-0.5" />
          <div>
            <p className="font-medium text-green-800">
              导入成功! 共 {result.length} 个群聊
            </p>
            {result.map((r, i) => (
              <p key={i} className="text-sm text-green-700 mt-1">
                {r.chat_name} · 新增 {r.message_count} 条消息 · {r.date_range}
              </p>
            ))}
          </div>
        </div>
      )}

      {/* 索引构建进度 */}
      {indexProgress && (indexProgress.running || indexProgress.completed > 0) && (
        <div className="mt-4 bg-blue-50 border border-blue-200 rounded-lg p-4">
          <div className="flex items-center gap-2 mb-2">
            {indexProgress.running ? (
              <Loader2 size={18} className="text-blue-600 animate-spin" />
            ) : (
              <Database size={18} className="text-blue-600" />
            )}
            <span className="font-medium text-blue-800">
              {indexProgress.running
                ? `正在构建向量索引 (${indexProgress.completed}/${indexProgress.total})`
                : `向量索引构建完成 (${indexProgress.completed}/${indexProgress.total})`}
            </span>
          </div>
          {/* 进度条 */}
          <div className="w-full bg-blue-100 rounded-full h-2 mb-2">
            <div
              className="bg-blue-500 h-2 rounded-full transition-all duration-500"
              style={{ width: indexProgress.total ? `${(indexProgress.completed / indexProgress.total) * 100}%` : '0%' }}
            />
          </div>
          {indexProgress.running && indexProgress.current_chat && (
            <p className="text-xs text-blue-600">当前: {indexProgress.current_chat}</p>
          )}
          {!indexProgress.running && indexProgress.results?.length > 0 && (
            <div className="mt-2 space-y-0.5">
              {indexProgress.results.map((r, i) => (
                <p key={i} className={`text-xs ${r.status === 'ok' ? 'text-green-700' : 'text-red-600'}`}>
                  {r.status === 'ok' ? '✓' : '✗'} {r.chat_name}
                  {r.status === 'ok' && r.topics != null && ` · ${r.topics} 个话题已索引`}
                  {r.status === 'error' && ` · ${r.error}`}
                </p>
              ))}
            </div>
          )}
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
                  <th className="text-left px-4 py-2 font-medium">索引状态</th>
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
                    <td className="px-4 py-2.5">
                      {imp.index_built ? (
                        <span className="inline-flex items-center gap-1 text-xs text-green-700 bg-green-50 px-2 py-0.5 rounded-full">
                          <span className="w-1.5 h-1.5 rounded-full bg-green-500" />
                          已索引
                        </span>
                      ) : (
                        <span className="inline-flex items-center gap-1 text-xs text-amber-700 bg-amber-50 px-2 py-0.5 rounded-full">
                          <span className="w-1.5 h-1.5 rounded-full bg-amber-500" />
                          待索引
                        </span>
                      )}
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
