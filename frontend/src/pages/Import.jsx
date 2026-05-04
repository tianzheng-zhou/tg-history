import { useCallback, useEffect, useRef, useState } from "react";
import {
  Upload,
  CheckCircle2,
  AlertCircle,
  FileJson,
  Loader2,
  Database,
  FolderPlus,
  FolderSearch,
  Folder,
  RefreshCw,
  Trash2,
  Send,
} from "lucide-react";
import {
  importChat,
  getChats,
  getIndexProgress,
  getImportProgress,
  validateFolder,
  listFolders,
  addFolder,
  deleteFolder,
  scanFolder,
} from "@/lib/api";
import TelegramSync from "@/components/TelegramSync";

export default function Import() {
  const [activeTab, setActiveTab] = useState("upload"); // "upload" | "folder" | "telegram"
  const [file, setFile] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [recentImports, setRecentImports] = useState([]);
  const [indexProgress, setIndexProgress] = useState(null);
  const [importProgress, setImportProgress] = useState(null);
  const pollRef = useRef(null);
  const importPollRef = useRef(null);

  // 绑定目录相关
  const [folders, setFolders] = useState([]);
  const [pathInput, setPathInput] = useState("");
  const [aliasInput, setAliasInput] = useState("");
  const [validating, setValidating] = useState(false);
  const [validateResult, setValidateResult] = useState(null);
  const [adding, setAdding] = useState(false);
  const [folderError, setFolderError] = useState(null);
  const [scanningId, setScanningId] = useState(null);

  const loadImports = useCallback(() => {
    getChats().then(setRecentImports).catch(() => {});
  }, []);

  const loadFolders = useCallback(() => {
    listFolders().then(setFolders).catch(() => {});
  }, []);

  useEffect(() => {
    loadImports();
    loadFolders();
  }, [loadImports, loadFolders]);

  const handleDrop = useCallback((e) => {
    e.preventDefault();
    const f = e.dataTransfer?.files?.[0];
    if (f && f.name.endsWith(".json")) {
      setFile(f);
      setError(null);
    }
  }, []);

  const handleFileSelect = (e) => {
    const f = e.target.files?.[0];
    if (f) {
      setFile(f);
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

  // 文件 / 目录扫描的导入进度轮询：与索引构建独立。
  // import 流程：parsing → importing → done | error，
  // 完成后再触发 startPolling() 跟进向量索引阶段。
  const startImportPolling = useCallback(() => {
    if (importPollRef.current) clearInterval(importPollRef.current);
    importPollRef.current = setInterval(async () => {
      try {
        const prog = await getImportProgress();
        setImportProgress(prog);
        if (!prog.running) {
          clearInterval(importPollRef.current);
          importPollRef.current = null;
          loadImports();
          loadFolders();
          // 导入完成后，索引构建一般会被 _enqueue_index 触发 → 顺便启动索引轮询
          getIndexProgress().then((ip) => {
            if (ip.running) {
              setIndexProgress(ip);
              startPolling();
            }
          }).catch(() => {});
        }
      } catch {
        clearInterval(importPollRef.current);
        importPollRef.current = null;
      }
    }, 800);
  }, [loadImports, loadFolders, startPolling]);

  useEffect(() => {
    // 页面加载时同时检查导入任务和索引构建是否在跑
    getImportProgress().then((prog) => {
      if (prog.running) {
        setImportProgress(prog);
        startImportPolling();
      } else if (prog.stage && (prog.stage === "done" || prog.stage === "error")) {
        // 上一次任务已结束，展示一次结果
        setImportProgress(prog);
      }
    }).catch(() => {});
    getIndexProgress().then((prog) => {
      if (prog.running || prog.completed > 0) {
        setIndexProgress(prog);
        if (prog.running) startPolling();
      }
    }).catch(() => {});
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
      if (importPollRef.current) clearInterval(importPollRef.current);
    };
  }, [startPolling, startImportPolling]);

  const handleImport = async () => {
    if (!file) return;
    setLoading(true);
    setError(null);
    setImportProgress(null);
    try {
      // 后端立即返回 task_id，真正进度通过轮询拿；不再阻塞等几十分钟
      await importChat(file);
      setFile(null);
      // 立刻拉一次并启动轮询，避免空白 1 秒
      try {
        const prog = await getImportProgress();
        setImportProgress(prog);
      } catch {}
      startImportPolling();
    } catch (err) {
      setError(err.response?.data?.detail || "导入失败，请检查文件格式");
    } finally {
      setLoading(false);
    }
  };

  const handleValidate = async () => {
    const p = pathInput.trim();
    if (!p) return;
    setValidating(true);
    setFolderError(null);
    setValidateResult(null);
    try {
      const data = await validateFolder(p);
      setValidateResult(data);
    } catch (err) {
      setFolderError(err.response?.data?.detail || "路径校验失败");
    } finally {
      setValidating(false);
    }
  };

  const handleAddFolder = async () => {
    const p = pathInput.trim();
    if (!p) return;
    setAdding(true);
    setFolderError(null);
    try {
      await addFolder(p, aliasInput.trim() || null);
      setPathInput("");
      setAliasInput("");
      setValidateResult(null);
      loadFolders();
    } catch (err) {
      setFolderError(err.response?.data?.detail || "添加失败");
    } finally {
      setAdding(false);
    }
  };

  const handleDeleteFolder = async (folderId, folderPath) => {
    if (!window.confirm(`确定要解除绑定该目录吗？\n${folderPath}\n（已导入的群聊数据不会被删除）`)) {
      return;
    }
    try {
      await deleteFolder(folderId);
      loadFolders();
    } catch (err) {
      setFolderError(err.response?.data?.detail || "删除失败");
    }
  };

  const handleScanFolder = async (folderId) => {
    setScanningId(folderId);
    setFolderError(null);
    setImportProgress(null);
    try {
      // scanFolder 后端改成异步任务，立即返回 task_id；进度通过轮询拿
      await scanFolder(folderId);
      try {
        const prog = await getImportProgress();
        setImportProgress(prog);
      } catch {}
      startImportPolling();
    } catch (err) {
      setFolderError(err.response?.data?.detail || "扫描失败");
    } finally {
      setScanningId(null);
    }
  };

  const TABS = [
    { key: "upload", label: "文件上传", icon: Upload },
    { key: "folder", label: "扫描目录", icon: FolderSearch },
    { key: "telegram", label: "Telegram 直连", icon: Send },
  ];

  return (
    <div>
      <h1 className="text-2xl font-bold mb-4">数据导入</h1>

      {/* Tabs */}
      <div className="flex items-center gap-1 border-b border-border mb-6">
        {TABS.map((t) => {
          const Icon = t.icon;
          const active = activeTab === t.key;
          return (
            <button
              key={t.key}
              onClick={() => setActiveTab(t.key)}
              className={`inline-flex items-center gap-2 px-4 py-2.5 text-sm font-medium border-b-2 transition-colors -mb-px ${
                active
                  ? "border-primary text-primary"
                  : "border-transparent text-muted-foreground hover:text-foreground hover:border-border"
              }`}
            >
              <Icon size={14} />
              {t.label}
            </button>
          );
        })}
      </div>

      {/* Tab: 文件上传 */}
      {activeTab === "upload" && (
        <>
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

          {error && (
            <div className="mt-4 bg-red-50 border border-red-200 rounded-lg p-4 flex items-start gap-3">
              <AlertCircle size={20} className="text-red-600 mt-0.5" />
              <div>
                <p className="font-medium text-red-800">导入失败</p>
                <p className="text-sm text-red-700 mt-1">{error}</p>
              </div>
            </div>
          )}
        </>
      )}

      {/* Tab: 扫描目录 */}
      {activeTab === "folder" && (
      <div>
        <p className="text-xs text-muted-foreground mb-3">
          绑定一个本地目录（服务端可访问的绝对路径），点击"立即扫描"会递归查找其中所有 <code className="px-1 py-0.5 bg-secondary rounded">result.json</code> 并自动导入。已扫描过且未修改的文件会被跳过。
        </p>

        {/* 输入区 */}
        <div className="bg-card border border-border rounded-lg p-4 space-y-3">
          <div className="flex gap-2">
            <input
              type="text"
              value={pathInput}
              onChange={(e) => {
                setPathInput(e.target.value);
                setValidateResult(null);
                setFolderError(null);
              }}
              placeholder="例：D:\TG_Exports 或 /home/user/tg_exports"
              className="flex-1 border border-border rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring font-mono"
              onKeyDown={(e) => {
                if (e.key === "Enter" && pathInput.trim()) handleValidate();
              }}
            />
            <input
              type="text"
              value={aliasInput}
              onChange={(e) => setAliasInput(e.target.value)}
              placeholder="别名（可选）"
              className="w-40 border border-border rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
            />
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={handleValidate}
              disabled={validating || !pathInput.trim()}
              className="inline-flex items-center gap-1.5 border border-border px-3 py-1.5 rounded-md text-sm hover:bg-secondary transition-colors disabled:opacity-50"
            >
              {validating ? (
                <Loader2 size={14} className="animate-spin" />
              ) : (
                <FolderSearch size={14} />
              )}
              校验路径
            </button>
            <button
              onClick={handleAddFolder}
              disabled={
                adding ||
                !pathInput.trim() ||
                !validateResult ||
                !validateResult.valid
              }
              className="inline-flex items-center gap-1.5 bg-primary text-primary-foreground px-3 py-1.5 rounded-md text-sm hover:opacity-90 transition-opacity disabled:opacity-50"
              title={
                !validateResult
                  ? "请先校验路径"
                  : !validateResult.valid
                  ? "路径无效，无法添加"
                  : ""
              }
            >
              {adding ? (
                <Loader2 size={14} className="animate-spin" />
              ) : (
                <FolderPlus size={14} />
              )}
              添加绑定
            </button>

            {validateResult && validateResult.valid && (
              <span className="text-xs text-green-700 inline-flex items-center gap-1">
                <CheckCircle2 size={14} />
                找到 {validateResult.result_json_count} 个 result.json
                {validateResult.sample_paths?.length > 0 && (
                  <span
                    className="text-muted-foreground"
                    title={validateResult.sample_paths.join("\n")}
                  >
                    （前 {validateResult.sample_paths.length} 个：
                    {validateResult.sample_paths.slice(0, 2).join("、")}
                    {validateResult.sample_paths.length > 2 ? " ..." : ""}）
                  </span>
                )}
              </span>
            )}
            {validateResult && !validateResult.valid && (
              <span className="text-xs text-red-600 inline-flex items-center gap-1">
                <AlertCircle size={14} />
                {validateResult.reason || "路径无效"}
              </span>
            )}
          </div>
          {folderError && (
            <div className="text-xs text-red-600 inline-flex items-center gap-1">
              <AlertCircle size={14} />
              {folderError}
            </div>
          )}
        </div>

        {/* 已绑定列表 */}
        {folders.length > 0 && (
          <div className="mt-3 border border-border rounded-lg overflow-hidden">
            {folders.map((f) => (
              <div
                key={f.id}
                className="flex items-center justify-between px-4 py-3 border-b border-border last:border-b-0 bg-card"
              >
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <Folder size={14} className="text-muted-foreground shrink-0" />
                    <span className="text-sm font-medium truncate" title={f.path}>
                      {f.alias || f.path}
                    </span>
                  </div>
                  {f.alias && (
                    <p
                      className="text-xs text-muted-foreground font-mono truncate mt-0.5"
                      title={f.path}
                    >
                      {f.path}
                    </p>
                  )}
                  <p className="text-xs text-muted-foreground mt-0.5">
                    {f.last_scan_at ? (
                      <>
                        上次扫描：
                        {new Date(f.last_scan_at).toLocaleString("zh-CN")}
                        {" · "}共 {f.last_scan_total} · 新导入{" "}
                        <span className="text-green-700">
                          {f.last_scan_imported}
                        </span>
                        {" · 跳过 "}
                        {f.last_scan_skipped}
                        {f.last_scan_failed > 0 && (
                          <>
                            {" · "}
                            <span className="text-red-600">
                              失败 {f.last_scan_failed}
                            </span>
                          </>
                        )}
                      </>
                    ) : (
                      "尚未扫描"
                    )}
                  </p>
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  <button
                    onClick={() => handleScanFolder(f.id)}
                    disabled={scanningId !== null}
                    className="inline-flex items-center gap-1.5 bg-primary text-primary-foreground px-3 py-1.5 rounded-md text-xs hover:opacity-90 transition-opacity disabled:opacity-50"
                  >
                    {scanningId === f.id ? (
                      <Loader2 size={12} className="animate-spin" />
                    ) : (
                      <RefreshCw size={12} />
                    )}
                    立即扫描
                  </button>
                  <button
                    onClick={() => handleDeleteFolder(f.id, f.path)}
                    disabled={scanningId !== null}
                    className="inline-flex items-center gap-1 border border-border text-muted-foreground px-2 py-1.5 rounded-md text-xs hover:bg-secondary hover:text-red-600 transition-colors disabled:opacity-50"
                    title="解除绑定"
                  >
                    <Trash2 size={12} />
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}

      </div>
      )}

      {/* Tab: Telegram 直连 */}
      {activeTab === "telegram" && (
        <TelegramSync onImported={() => { loadImports(); startPolling(); }} />
      )}

      {/* 导入进度（文件上传 / 目录扫描共用） */}
      {importProgress && importProgress.stage && (
        <div
          className={`mt-4 border rounded-lg p-4 ${
            importProgress.stage === "error"
              ? "bg-red-50 border-red-200"
              : importProgress.stage === "done"
              ? "bg-green-50 border-green-200"
              : "bg-blue-50 border-blue-200"
          }`}
        >
          <div className="flex items-center gap-2 mb-2">
            {importProgress.running ? (
              <Loader2 size={18} className="text-blue-600 animate-spin" />
            ) : importProgress.stage === "error" ? (
              <AlertCircle size={18} className="text-red-600" />
            ) : (
              <CheckCircle2 size={18} className="text-green-600" />
            )}
            <span
              className={`font-medium ${
                importProgress.stage === "error"
                  ? "text-red-800"
                  : importProgress.stage === "done"
                  ? "text-green-800"
                  : "text-blue-800"
              }`}
            >
              {importProgress.running
                ? importProgress.stage === "parsing"
                  ? "正在解析 JSON 文件..."
                  : `正在导入 ${
                      importProgress.current_chat_name || ""
                    } (${importProgress.chats_done}/${
                      importProgress.chats_total || "?"
                    })`
                : importProgress.stage === "error"
                ? `导入失败：${importProgress.error || "未知错误"}`
                : `导入完成 · 共 ${importProgress.chats_done} 个群聊`}
            </span>
          </div>

          {/* 当前 chat 内部进度（消息条数） */}
          {importProgress.running &&
            importProgress.stage === "importing" &&
            importProgress.current_chat_total > 0 && (
              <>
                <div className="w-full bg-blue-100 rounded-full h-2 mb-1">
                  <div
                    className="bg-blue-500 h-2 rounded-full transition-all duration-300"
                    style={{
                      width: `${
                        (importProgress.current_chat_done /
                          importProgress.current_chat_total) *
                        100
                      }%`,
                    }}
                  />
                </div>
                <p className="text-xs text-blue-700">
                  {importProgress.current_chat_done.toLocaleString()} /{" "}
                  {importProgress.current_chat_total.toLocaleString()} 条消息
                </p>
              </>
            )}

          {/* 目录扫描的文件级进度 */}
          {importProgress.kind === "folder_scan" &&
            importProgress.files_total > 0 && (
              <p className="text-xs text-blue-700 mt-1">
                文件 {importProgress.files_done} / {importProgress.files_total}
              </p>
            )}

          {/* 完成后的结果列表 */}
          {!importProgress.running &&
            importProgress.results?.length > 0 && (
              <div className="mt-2 max-h-48 overflow-y-auto space-y-0.5 text-xs">
                {importProgress.results.map((r, i) => (
                  <p
                    key={i}
                    className={
                      r.status === "ok" ? "text-green-700" : "text-red-600"
                    }
                  >
                    {r.status === "ok" ? "✓" : "✗"} {r.chat_name}
                    {r.status === "ok" && (
                      <span className="text-muted-foreground">
                        {" "}
                        · 新增 {r.message_count} 条 · {r.date_range}
                      </span>
                    )}
                    {r.status === "error" && r.error && (
                      <span> · {r.error}</span>
                    )}
                  </p>
                ))}
              </div>
            )}
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
