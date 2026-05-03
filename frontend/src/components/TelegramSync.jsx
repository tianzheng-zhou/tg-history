import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Send,
  ShieldCheck,
  ShieldAlert,
  Loader2,
  LogOut,
  RefreshCw,
  Search,
  CheckCircle2,
  AlertCircle,
  KeyRound,
  Smartphone,
  Hash,
  Users,
  Megaphone,
  User as UserIcon,
  MessageSquare,
  Square,
  CheckSquare,
  X,
  Network,
} from "lucide-react";
import {
  abortTelegramSync,
  configureTelegramAccount,
  deleteTelegramAccount,
  startTelegramSync,
  telegramSendCode,
  telegramVerifyCode,
} from "@/lib/api";
import { useTelegramStore } from "@/lib/telegramStore";

const TYPE_LABEL = {
  group: { label: "群组", icon: Users, color: "text-blue-700 bg-blue-50" },
  supergroup: { label: "超群", icon: Users, color: "text-indigo-700 bg-indigo-50" },
  channel: { label: "频道", icon: Megaphone, color: "text-purple-700 bg-purple-50" },
  private: { label: "私聊", icon: UserIcon, color: "text-gray-700 bg-gray-100" },
  unknown: { label: "未知", icon: MessageSquare, color: "text-gray-700 bg-gray-100" },
};

export default function TelegramSync({ onImported }) {
  // 全局账号 + 同步进度 + 对话列表（切换页面不丢）
  const {
    account,
    accountLoading,
    progress,
    dialogs,
    dialogsLoading,
    dialogsError,
    refreshAccount,
    refreshDialogs,
    startSyncPolling,
    setProgress,
    setDialogs,
    setOnSyncFinished,
  } = useTelegramStore();

  const [error, setError] = useState(null);

  // 状态 A：表单
  const [apiId, setApiId] = useState("");
  const [apiHash, setApiHash] = useState("");
  const [phone, setPhone] = useState("");
  const [savingConfig, setSavingConfig] = useState(false);

  // 状态 B：等待验证码
  const [code, setCode] = useState("");
  const [password, setPassword] = useState("");
  const [needsPassword, setNeedsPassword] = useState(false);
  const [sendingCode, setSendingCode] = useState(false);
  const [verifying, setVerifying] = useState(false);

  // 状态 C：对话列表交互 state（selectedIds 仅本页使用）
  const [selectedIds, setSelectedIds] = useState(() => new Set());
  const [search, setSearch] = useState("");
  const [typeFilter, setTypeFilter] = useState("all");

  // stage 派生：根据 account 状态自动决定 UI 阶段
  const stage = useMemo(() => {
    if (!account) return "config";
    if (account.authorized) return "ready";
    if (account.configured) return "code";
    return "config";
  }, [account]);

  // syncing 派生
  const syncing = !!progress?.running;

  // 注册同步完成回调（结束时刷新已导入列表）
  useEffect(() => {
    setOnSyncFinished(() => {
      onImported?.();
    });
    return () => setOnSyncFinished(null);
  }, [onImported, setOnSyncFinished]);

  // ---------- 状态 A：保存配置 + 发码 ----------

  const handleSaveConfigAndSendCode = async () => {
    setError(null);
    if (!apiId || !apiHash || !phone) {
      setError("请填写完整：api_id、api_hash 和手机号");
      return;
    }
    setSavingConfig(true);
    try {
      await configureTelegramAccount({ apiId, apiHash, phone });
      // 立刻发码
      setSendingCode(true);
      await telegramSendCode();
      await refreshAccount();
    } catch (e) {
      setError(e.response?.data?.detail || e.message || "保存或发送验证码失败");
    } finally {
      setSavingConfig(false);
      setSendingCode(false);
    }
  };

  const handleResendCode = async () => {
    setError(null);
    setSendingCode(true);
    try {
      await telegramSendCode();
    } catch (e) {
      setError(e.response?.data?.detail || "重新发送失败");
    } finally {
      setSendingCode(false);
    }
  };

  // ---------- 状态 B：验证码 ----------

  const handleVerifyCode = async () => {
    setError(null);
    if (!code) {
      setError("请输入验证码");
      return;
    }
    setVerifying(true);
    try {
      const data = await telegramVerifyCode(code.trim(), password.trim() || null);
      if (data.needs_password) {
        setNeedsPassword(true);
        setError("此账号开启了二次验证，请输入云密码后再次点击「登录」");
      } else if (data.authorized) {
        setNeedsPassword(false);
        setCode("");
        setPassword("");
        await refreshAccount();
      }
    } catch (e) {
      setError(e.response?.data?.detail || "登录失败");
    } finally {
      setVerifying(false);
    }
  };

  // ---------- 退出登录 ----------

  const handleLogout = async () => {
    if (!window.confirm("确定退出 Telegram 登录吗？\n（已导入的群聊数据不会被删除，仅清除登录态）")) return;
    setError(null);
    try {
      await deleteTelegramAccount();
      setApiId("");
      setApiHash("");
      setPhone("");
      setCode("");
      setPassword("");
      setNeedsPassword(false);
      setSelectedIds(new Set());
      setDialogs([]);
      await refreshAccount();
    } catch (e) {
      setError(e.response?.data?.detail || "退出失败");
    }
  };

  // ---------- 状态 C：对话列表 ----------

  // 进入 ready 阶段且还没拉过 dialogs 时才主动拉一次（之后由用户点「刷新」触发）
  useEffect(() => {
    if (stage === "ready" && dialogs.length === 0 && !dialogsLoading) {
      refreshDialogs();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stage]);

  const loadingDialogs = dialogsLoading;

  const filteredDialogs = useMemo(() => {
    const s = search.trim().toLowerCase();
    return dialogs.filter((d) => {
      if (typeFilter !== "all" && d.type !== typeFilter) return false;
      if (s && !d.name.toLowerCase().includes(s) && !(d.username || "").toLowerCase().includes(s)) return false;
      return true;
    });
  }, [dialogs, search, typeFilter]);

  const allSelected = filteredDialogs.length > 0 && filteredDialogs.every((d) => selectedIds.has(d.chat_id));
  const someSelected = filteredDialogs.some((d) => selectedIds.has(d.chat_id));

  const toggleAll = () => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (allSelected) {
        filteredDialogs.forEach((d) => next.delete(d.chat_id));
      } else {
        filteredDialogs.forEach((d) => next.add(d.chat_id));
      }
      return next;
    });
  };

  const toggleOne = (cid) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(cid)) next.delete(cid); else next.add(cid);
      return next;
    });
  };

  const handleStartSync = async () => {
    if (selectedIds.size === 0) return;
    setError(null);
    setProgress(null);
    try {
      await startTelegramSync(Array.from(selectedIds));
      startSyncPolling();
    } catch (e) {
      setError(e.response?.data?.detail || "启动同步失败");
    }
  };

  const handleAbortSync = async () => {
    try {
      await abortTelegramSync();
    } catch {
      // ignore
    }
  };

  // ---------- 渲染 ----------

  if (accountLoading) {
    return (
      <div className="flex items-center gap-2 text-muted-foreground py-12 justify-center">
        <Loader2 size={16} className="animate-spin" />
        <span className="text-sm">加载中…</span>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* 代理状态条 */}
      {account?.proxy && (
        account.proxy.enabled ? (
          <div className="bg-green-50 border border-green-200 rounded-md px-3 py-2 flex items-center gap-2 text-xs">
            <Network size={14} className="text-green-700" />
            <span className="text-green-800">
              已启用代理 <span className="font-mono">{account.proxy.scheme}://{account.proxy.host}:{account.proxy.port}</span>
              <span className="ml-2 text-green-600">
                （来源：{account.proxy.source === "settings" ? ".env 配置" : "环境变量"}）
              </span>
            </span>
          </div>
        ) : (
          <div className="bg-amber-50 border border-amber-200 rounded-md px-3 py-2 flex items-start gap-2 text-xs">
            <Network size={14} className="text-amber-700 mt-0.5 shrink-0" />
            <div className="text-amber-800 leading-relaxed">
              <p className="font-medium">未配置代理</p>
              <p className="mt-0.5">
                如果在中国大陆，需要在项目根目录的 <code className="px-1 bg-amber-100 rounded">.env</code> 文件中加入：
              </p>
              <pre className="mt-1 mb-1 px-2 py-1 bg-amber-100 rounded font-mono select-all">TELEGRAM_PROXY=socks5://127.0.0.1:7891</pre>
              <p>把端口换成你 Clash / V2Ray 的实际 SOCKS5 端口（也可用 <code className="px-1 bg-amber-100 rounded">http://...</code>），然后<span className="font-medium">重启后端</span>。</p>
            </div>
          </div>
        )
      )}

      {error && (
        <div className="bg-red-50 border border-red-200 rounded-lg p-3 flex items-start gap-2">
          <AlertCircle size={16} className="text-red-600 mt-0.5 shrink-0" />
          <p className="text-sm text-red-700 whitespace-pre-wrap break-words">{error}</p>
        </div>
      )}

      {/* 状态 A：未配置 */}
      {stage === "config" && (
        <div className="bg-card border border-border rounded-lg p-6 space-y-5">
          <div>
            <h3 className="text-base font-semibold mb-1">连接你的 Telegram 账号</h3>
            <p className="text-xs text-muted-foreground">
              通过 MTProto API 直接登录，自动列出所有群聊与频道，无需手动导出 result.json。
            </p>
          </div>

          <details className="text-xs bg-secondary/50 rounded-md p-3 cursor-pointer">
            <summary className="font-medium select-none">如何获取 api_id 和 api_hash？</summary>
            <ol className="mt-2 ml-5 space-y-1 list-decimal text-muted-foreground">
              <li>访问 <a href="https://my.telegram.org/apps" target="_blank" rel="noreferrer" className="text-primary underline">https://my.telegram.org/apps</a> 登录你的 Telegram 账号</li>
              <li>填写 App title（含空格）+ Short name（纯字母数字）+ Platform 选 Desktop</li>
              <li>提交后即可看到 <code className="px-1 bg-secondary rounded">api_id</code>（数字）和 <code className="px-1 bg-secondary rounded">api_hash</code>（32 位 hex）</li>
              <li>⚠️ <span className="font-medium text-amber-700">api_hash 等同于密码</span>，妥善保管，不要分享或上传到 git</li>
            </ol>
          </details>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <label className="block">
              <span className="text-xs font-medium text-muted-foreground flex items-center gap-1.5 mb-1">
                <Hash size={12} /> api_id
              </span>
              <input
                type="text"
                inputMode="numeric"
                value={apiId}
                onChange={(e) => setApiId(e.target.value)}
                placeholder="例：12345678"
                className="w-full border border-border rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring font-mono"
              />
            </label>
            <label className="block">
              <span className="text-xs font-medium text-muted-foreground flex items-center gap-1.5 mb-1">
                <KeyRound size={12} /> api_hash
              </span>
              <input
                type="password"
                value={apiHash}
                onChange={(e) => setApiHash(e.target.value)}
                placeholder="32 位十六进制字符串"
                className="w-full border border-border rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring font-mono"
                autoComplete="off"
              />
            </label>
            <label className="block md:col-span-2">
              <span className="text-xs font-medium text-muted-foreground flex items-center gap-1.5 mb-1">
                <Smartphone size={12} /> 手机号（含国家码）
              </span>
              <input
                type="tel"
                value={phone}
                onChange={(e) => setPhone(e.target.value)}
                placeholder="+8613800138000"
                className="w-full border border-border rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring font-mono"
              />
            </label>
          </div>

          <button
            onClick={handleSaveConfigAndSendCode}
            disabled={savingConfig || sendingCode || !apiId || !apiHash || !phone}
            className="inline-flex items-center gap-2 bg-primary text-primary-foreground px-4 py-2 rounded-md text-sm hover:opacity-90 transition-opacity disabled:opacity-50"
          >
            {(savingConfig || sendingCode) ? (
              <Loader2 size={14} className="animate-spin" />
            ) : (
              <Send size={14} />
            )}
            发送验证码
          </button>
        </div>
      )}

      {/* 状态 B：等待验证码 */}
      {stage === "code" && (
        <div className="bg-card border border-border rounded-lg p-6 space-y-4">
          <div>
            <h3 className="text-base font-semibold mb-1 flex items-center gap-2">
              <ShieldCheck size={16} className="text-primary" />
              输入验证码
            </h3>
            <p className="text-xs text-muted-foreground">
              验证码已发到你的 <span className="font-medium">Telegram 客户端</span>（不是短信）
              {account?.phone && <> — 手机号：<span className="font-mono">{account.phone}</span></>}
            </p>
          </div>

          <label className="block">
            <span className="text-xs font-medium text-muted-foreground mb-1 block">验证码（5–6 位数字）</span>
            <input
              type="text"
              inputMode="numeric"
              value={code}
              onChange={(e) => setCode(e.target.value)}
              placeholder="123456"
              maxLength={6}
              className="w-44 border border-border rounded-md px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-ring font-mono tracking-widest"
              autoFocus
              onKeyDown={(e) => { if (e.key === "Enter" && code.trim()) handleVerifyCode(); }}
            />
          </label>

          {needsPassword && (
            <label className="block">
              <span className="text-xs font-medium text-muted-foreground mb-1 block flex items-center gap-1.5">
                <ShieldAlert size={12} className="text-amber-600" />
                二次验证密码（云密码，不是 Telegram 登录密码）
              </span>
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="w-full max-w-sm border border-border rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                autoComplete="off"
              />
            </label>
          )}

          <div className="flex items-center gap-2">
            <button
              onClick={handleVerifyCode}
              disabled={verifying || !code.trim()}
              className="inline-flex items-center gap-2 bg-primary text-primary-foreground px-4 py-2 rounded-md text-sm hover:opacity-90 transition-opacity disabled:opacity-50"
            >
              {verifying ? <Loader2 size={14} className="animate-spin" /> : <ShieldCheck size={14} />}
              登录
            </button>
            <button
              onClick={handleResendCode}
              disabled={sendingCode}
              className="inline-flex items-center gap-1.5 border border-border px-3 py-2 rounded-md text-sm hover:bg-secondary disabled:opacity-50"
            >
              {sendingCode ? <Loader2 size={14} className="animate-spin" /> : <RefreshCw size={14} />}
              重新发送
            </button>
            <button
              onClick={handleLogout}
              className="inline-flex items-center gap-1.5 text-muted-foreground px-3 py-2 rounded-md text-sm hover:text-red-600"
            >
              <X size={14} />
              换号
            </button>
          </div>
        </div>
      )}

      {/* 状态 C：已登录 */}
      {stage === "ready" && account && (
        <>
          {/* 账号卡片 */}
          <div className="bg-card border border-border rounded-lg px-4 py-3 flex items-center justify-between">
            <div className="flex items-center gap-3 min-w-0">
              <div className="w-9 h-9 rounded-full bg-primary/10 text-primary flex items-center justify-center font-semibold text-sm shrink-0">
                {(account.first_name || account.username || "?").slice(0, 1).toUpperCase()}
              </div>
              <div className="min-w-0">
                <p className="text-sm font-medium truncate">
                  {[account.first_name, account.last_name].filter(Boolean).join(" ") || account.username || "Telegram 用户"}
                </p>
                <p className="text-xs text-muted-foreground truncate font-mono">
                  {account.phone}
                  {account.username && <span className="ml-2">@{account.username}</span>}
                </p>
              </div>
            </div>
            <button
              onClick={handleLogout}
              className="inline-flex items-center gap-1.5 border border-border text-muted-foreground px-3 py-1.5 rounded-md text-xs hover:bg-secondary hover:text-red-600 transition-colors"
            >
              <LogOut size={12} />
              退出登录
            </button>
          </div>

          {/* 工具条 */}
          <div className="flex items-center gap-2 flex-wrap">
            <div className="flex items-center gap-2 flex-1 min-w-[200px]">
              <div className="relative flex-1 max-w-sm">
                <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
                <input
                  type="text"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  placeholder="搜索群聊名称 / 用户名"
                  className="w-full pl-9 pr-3 py-1.5 text-sm border border-border rounded-md focus:outline-none focus:ring-2 focus:ring-ring"
                />
              </div>
              <select
                value={typeFilter}
                onChange={(e) => setTypeFilter(e.target.value)}
                className="border border-border rounded-md px-2 py-1.5 text-sm bg-background focus:outline-none focus:ring-2 focus:ring-ring"
              >
                <option value="all">全部类型</option>
                <option value="group">群组</option>
                <option value="supergroup">超级群</option>
                <option value="channel">频道</option>
                <option value="private">私聊</option>
              </select>
            </div>
            <button
              onClick={refreshDialogs}
              disabled={loadingDialogs || syncing}
              className="inline-flex items-center gap-1.5 border border-border px-3 py-1.5 rounded-md text-sm hover:bg-secondary disabled:opacity-50"
              title={syncing ? "同步进行中，无法刷新对话列表" : "重新拉取对话列表"}
            >
              {loadingDialogs ? <Loader2 size={14} className="animate-spin" /> : <RefreshCw size={14} />}
              刷新列表
            </button>
          </div>

          {/* 对话刷新失败提示（仅在已有数据时小条提示，避免与主 error 红条冲突）*/}
          {dialogsError && dialogs.length > 0 && (
            <div className="bg-amber-50 border border-amber-200 rounded-md px-3 py-1.5 flex items-center gap-2 text-xs text-amber-800">
              <AlertCircle size={12} className="text-amber-700 shrink-0" />
              <span>刷新失败（{dialogsError}），显示的是上次成功的列表。</span>
            </div>
          )}

          {/* 对话表 */}
          {loadingDialogs ? (
            <div className="flex items-center justify-center py-12 text-muted-foreground gap-2">
              <Loader2 size={16} className="animate-spin" />
              <span className="text-sm">正在加载对话列表（可能需要数秒）…</span>
            </div>
          ) : filteredDialogs.length === 0 ? (
            <div className="text-center py-12 text-muted-foreground text-sm">
              {dialogs.length === 0
                ? (dialogsError
                    ? <span className="text-red-600">加载失败：{dialogsError}</span>
                    : "暂无对话")
                : "没有匹配的对话，调整筛选条件"}
            </div>
          ) : (
            <div className="border border-border rounded-lg overflow-hidden bg-card">
              <table className="w-full text-sm">
                <thead className="bg-secondary text-xs">
                  <tr>
                    <th className="w-10 px-3 py-2">
                      <button onClick={toggleAll} className="text-muted-foreground hover:text-foreground">
                        {allSelected ? <CheckSquare size={14} /> : <Square size={14} />}
                      </button>
                    </th>
                    <th className="text-left px-3 py-2 font-medium">名称</th>
                    <th className="text-left px-3 py-2 font-medium w-20">类型</th>
                    <th className="text-left px-3 py-2 font-medium w-32">本地状态</th>
                    <th className="text-left px-3 py-2 font-medium w-28">远端最新</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredDialogs.map((d) => {
                    const meta = TYPE_LABEL[d.type] || TYPE_LABEL.unknown;
                    const Icon = meta.icon;
                    const checked = selectedIds.has(d.chat_id);
                    const newCount = (d.last_message_id || 0) - (d.local_max_message_id || 0);
                    return (
                      <tr
                        key={d.chat_id}
                        className={`border-t border-border hover:bg-secondary/40 cursor-pointer ${checked ? "bg-primary/5" : ""}`}
                        onClick={() => toggleOne(d.chat_id)}
                      >
                        <td className="px-3 py-2.5">
                          <button
                            onClick={(e) => { e.stopPropagation(); toggleOne(d.chat_id); }}
                            className={checked ? "text-primary" : "text-muted-foreground hover:text-foreground"}
                          >
                            {checked ? <CheckSquare size={14} /> : <Square size={14} />}
                          </button>
                        </td>
                        <td className="px-3 py-2.5 min-w-0">
                          <div className="font-medium truncate max-w-[360px]" title={d.name}>{d.name}</div>
                          {d.username && (
                            <div className="text-xs text-muted-foreground font-mono">@{d.username}</div>
                          )}
                        </td>
                        <td className="px-3 py-2.5">
                          <span className={`inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded ${meta.color}`}>
                            <Icon size={10} />
                            {meta.label}
                          </span>
                        </td>
                        <td className="px-3 py-2.5 text-xs">
                          {d.imported ? (
                            <div>
                              <div className="text-foreground">已 {d.imported_message_count.toLocaleString()} 条</div>
                              {newCount > 0 && (
                                <div className="text-amber-600">+{newCount.toLocaleString()} 条新</div>
                              )}
                            </div>
                          ) : (
                            <span className="text-muted-foreground">未导入</span>
                          )}
                        </td>
                        <td className="px-3 py-2.5 text-xs text-muted-foreground">
                          {d.last_message_id ? `#${d.last_message_id}` : "—"}
                          {d.last_message_date && (
                            <div>{new Date(d.last_message_date).toLocaleDateString("zh-CN")}</div>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}

          {/* 底部操作栏 */}
          {filteredDialogs.length > 0 && (
            <div className="flex items-center justify-between bg-card border border-border rounded-lg px-4 py-3 sticky bottom-4 shadow-sm">
              <div className="text-sm">
                {someSelected ? (
                  <span>
                    已选 <span className="font-semibold">{selectedIds.size}</span> 个对话
                  </span>
                ) : (
                  <span className="text-muted-foreground">勾选要同步的对话</span>
                )}
              </div>
              <div className="flex gap-2">
                {syncing ? (
                  <button
                    onClick={handleAbortSync}
                    className="inline-flex items-center gap-1.5 border border-border px-4 py-2 rounded-md text-sm hover:bg-secondary"
                    disabled={progress?.aborting}
                  >
                    <X size={14} />
                    {progress?.aborting ? "正在终止…" : "中止同步"}
                  </button>
                ) : (
                  <button
                    onClick={handleStartSync}
                    disabled={selectedIds.size === 0}
                    className="inline-flex items-center gap-2 bg-primary text-primary-foreground px-4 py-2 rounded-md text-sm hover:opacity-90 transition-opacity disabled:opacity-50"
                  >
                    <RefreshCw size={14} />
                    开始同步选中（{selectedIds.size}）
                  </button>
                )}
              </div>
            </div>
          )}

          {/* 同步进度面板 */}
          {progress && (progress.running || progress.completed > 0 || progress.results?.length > 0) && (
            <div className="bg-blue-50 border border-blue-200 rounded-lg p-4">
              <div className="flex items-center gap-2 mb-2">
                {progress.running ? (
                  <Loader2 size={16} className="text-blue-600 animate-spin" />
                ) : (
                  <CheckCircle2 size={16} className="text-blue-600" />
                )}
                <span className="font-medium text-blue-800 text-sm">
                  {progress.running
                    ? `正在拉取 (${progress.completed}/${progress.total})`
                    : `同步完成 (${progress.completed}/${progress.total})`}
                  {progress.aborting && <span className="ml-2 text-amber-700">— 正在终止…</span>}
                </span>
              </div>
              <div className="w-full bg-blue-100 rounded-full h-2 mb-2">
                <div
                  className="bg-blue-500 h-2 rounded-full transition-all duration-500"
                  style={{ width: progress.total ? `${(progress.completed / progress.total) * 100}%` : "0%" }}
                />
              </div>
              {progress.running && progress.current_chat_name && (
                <p className="text-xs text-blue-700">
                  当前：<span className="font-medium">{progress.current_chat_name}</span>
                  {" · 已拉取 "}{(progress.current_fetched || 0).toLocaleString()} 条
                  {" · 已入库 "}{(progress.current_imported || 0).toLocaleString()} 条
                </p>
              )}
              {!progress.running && progress.results?.length > 0 && (
                <div className="mt-2 max-h-48 overflow-y-auto space-y-0.5">
                  {progress.results.map((r, i) => (
                    <p key={i} className={`text-xs ${r.status === "ok" ? "text-green-700" : "text-red-600"}`}>
                      {r.status === "ok" ? "✓" : "✗"} {r.chat_name}
                      {r.status === "ok"
                        ? ` · 新增 ${r.message_count.toLocaleString()} 条`
                        : ` · ${r.error}`}
                    </p>
                  ))}
                </div>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}
