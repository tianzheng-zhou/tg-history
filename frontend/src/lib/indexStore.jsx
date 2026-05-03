import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
} from "react";
import { getChats, getIndexProgress } from "./api";

/**
 * IndexStoreProvider
 *
 * 全局缓存「索引管理」页面的数据：chats 列表 + 构建进度。
 *
 * 目的：用户切走再切回 index 页时，状态**立即显示**，不再有空窗期。
 *  - 启动时拉一次 chats + progress
 *  - progress.running 时持续后台轮询（独立于组件挂载）
 *  - 组件 mount 时会 soft-refresh 一次，补上切走期间的变化
 */

const POLL_MS = 1500;
const IndexStoreContext = createContext(null);

export function IndexStoreProvider({ children }) {
  const [chats, setChats] = useState([]);
  const [chatsLoading, setChatsLoading] = useState(true);
  const [progress, setProgress] = useState(null);
  const pollRef = useRef(null);

  const refreshChats = useCallback(async () => {
    try {
      const data = await getChats();
      setChats(data);
    } catch {
      // 静默失败（比如后端重启中），保留旧数据
    } finally {
      setChatsLoading(false);
    }
  }, []);

  const stopPolling = useCallback(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  const startPolling = useCallback(() => {
    if (pollRef.current) return; // 已在轮询
    pollRef.current = setInterval(async () => {
      try {
        const prog = await getIndexProgress();
        setProgress(prog);
        if (!prog.running) {
          stopPolling();
          // 构建完成 → 刷新 chats 列表（反映 index_built 变化）
          refreshChats();
        }
      } catch {
        stopPolling();
      }
    }, POLL_MS);
  }, [stopPolling, refreshChats]);

  const refreshProgress = useCallback(async () => {
    try {
      const prog = await getIndexProgress();
      setProgress(prog);
      if (prog.running) startPolling();
      return prog;
    } catch {
      return null;
    }
  }, [startPolling]);

  // 初始加载：拉 chats + progress；若正在构建就开始轮询
  useEffect(() => {
    refreshChats();
    refreshProgress();
    return () => stopPolling();
  }, [refreshChats, refreshProgress, stopPolling]);

  const value = {
    chats,
    chatsLoading,
    progress,
    rebuilding: !!progress?.running,
    refreshChats,
    refreshProgress,
    startPolling,
  };

  return (
    <IndexStoreContext.Provider value={value}>
      {children}
    </IndexStoreContext.Provider>
  );
}

export function useIndexStore() {
  const ctx = useContext(IndexStoreContext);
  if (!ctx) {
    throw new Error("useIndexStore must be used within IndexStoreProvider");
  }
  return ctx;
}
