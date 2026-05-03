import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
} from "react";
import {
  getTelegramAccount,
  getTelegramSyncProgress,
  listTelegramDialogs,
} from "./api";

/**
 * TelegramStoreProvider
 *
 * 全局缓存 Telegram 账号信息 + 同步进度，避免每次切换页面就触发
 * `/api/telegram/account` 查询（每次走 Telegram 服务器握手 ~2-3s）。
 *
 * 行为约定：
 *  - 启动时拉取一次账号信息
 *  - 同步进度：只要后端 progress.running，就持续轮询；不依赖任何组件挂载
 *  - 子组件可通过 `useTelegramStore()` 拿到 account / progress / 主动 refresh 方法
 */

const TelegramStoreContext = createContext(null);

// 2 分钟轮询一次 → 用户登录态变化最多 2 分钟内被前端感知。
// 后端有 5 分钟 authorized 缓存，所以大部分轮询命中缓存（~26ms），
// 偶尔（每 ~5 分钟一次）触发真握手（~2.7s，单次代价，不阻塞其他 API）。
const ACCOUNT_REFRESH_INTERVAL_MS = 2 * 60 * 1000;
const SYNC_POLL_INTERVAL_MS = 1500;

export function TelegramStoreProvider({ children }) {
  const [account, setAccount] = useState(null);
  const [accountLoading, setAccountLoading] = useState(true);
  const [accountError, setAccountError] = useState(null);
  const [progress, setProgress] = useState(null);

  // 对话列表全局缓存（切页面后保留，避免反复拉 Telegram）
  const [dialogs, setDialogs] = useState([]);
  const [dialogsLoading, setDialogsLoading] = useState(false);
  const [dialogsError, setDialogsError] = useState(null);

  const syncPollRef = useRef(null);
  const accountTimerRef = useRef(null);
  const onSyncFinishedRef = useRef(null); // 同步结束回调（外部注入）

  const refreshAccount = useCallback(async () => {
    try {
      const data = await getTelegramAccount();
      setAccount(data);
      setAccountError(null);
      return data;
    } catch (e) {
      const msg = e?.response?.data?.detail || e?.message || "获取账号状态失败";
      setAccountError(msg);
      return null;
    } finally {
      setAccountLoading(false);
    }
  }, []);

  const stopSyncPolling = useCallback(() => {
    if (syncPollRef.current) {
      clearInterval(syncPollRef.current);
      syncPollRef.current = null;
    }
  }, []);

  const startSyncPolling = useCallback(() => {
    if (syncPollRef.current) return; // 已在轮询
    syncPollRef.current = setInterval(async () => {
      try {
        const prog = await getTelegramSyncProgress();
        setProgress(prog);
        if (!prog.running) {
          stopSyncPolling();
          onSyncFinishedRef.current?.(prog);
        }
      } catch {
        stopSyncPolling();
      }
    }, SYNC_POLL_INTERVAL_MS);
  }, [stopSyncPolling]);

  // 启动：拉一次账号 + 检查是否有正在跑的同步
  useEffect(() => {
    refreshAccount();
    getTelegramSyncProgress()
      .then((prog) => {
        if (prog.running) {
          setProgress(prog);
          startSyncPolling();
        } else if (prog.completed > 0 || prog.results?.length) {
          setProgress(prog);
        }
      })
      .catch(() => {});

    // 定时静默续期账号（避免缓存超过 60s 后第一次访问慢）
    accountTimerRef.current = setInterval(() => {
      refreshAccount();
    }, ACCOUNT_REFRESH_INTERVAL_MS);

    return () => {
      stopSyncPolling();
      if (accountTimerRef.current) clearInterval(accountTimerRef.current);
    };
  }, [refreshAccount, startSyncPolling, stopSyncPolling]);

  const setOnSyncFinished = useCallback((cb) => {
    onSyncFinishedRef.current = cb;
  }, []);

  const refreshDialogs = useCallback(async () => {
    setDialogsLoading(true);
    try {
      const data = await listTelegramDialogs();
      setDialogs(data);
      setDialogsError(null);
      return data;
    } catch (e) {
      const msg =
        e?.response?.data?.detail ||
        "加载对话列表失败（可能 Telegram 限流，请稍后重试）";
      setDialogsError(msg);
      return null;
    } finally {
      setDialogsLoading(false);
    }
  }, []);

  const value = {
    account,
    accountLoading,
    accountError,
    progress,
    dialogs,
    dialogsLoading,
    dialogsError,
    refreshAccount,
    refreshDialogs,
    startSyncPolling,
    stopSyncPolling,
    setProgress,
    setDialogs,
    setOnSyncFinished,
  };

  return (
    <TelegramStoreContext.Provider value={value}>
      {children}
    </TelegramStoreContext.Provider>
  );
}

export function useTelegramStore() {
  const ctx = useContext(TelegramStoreContext);
  if (!ctx) {
    throw new Error("useTelegramStore must be used within TelegramStoreProvider");
  }
  return ctx;
}
