import { NavLink, Outlet, useLocation } from "react-router-dom";
import {
  LayoutDashboard,
  Upload,
  Database,
  MessageCircleQuestion,
  FileText,
  Settings,
} from "lucide-react";
import RunningRunsBadge from "./RunningRunsBadge";

const navItems = [
  { to: "/", icon: LayoutDashboard, label: "仪表盘" },
  { to: "/import", icon: Upload, label: "数据导入" },
  { to: "/index", icon: Database, label: "索引管理" },
  { to: "/qa", icon: MessageCircleQuestion, label: "智能问答" },
  { to: "/articles", icon: FileText, label: "文章库" },
  { to: "/settings", icon: Settings, label: "设置" },
];

export default function Layout() {
  const location = useLocation();
  // QA 与文章库使用全宽布局（两栏 / 滑出面板都受益于更宽视野）
  const isFullWidth =
    location.pathname.startsWith("/qa") ||
    location.pathname.startsWith("/articles");

  return (
    <div className="flex h-screen bg-background">
      {/* Sidebar */}
      <aside className="w-56 border-r border-border bg-card flex flex-col">
        <div className="p-4 border-b border-border">
          <h1 className="text-lg font-bold text-primary">TG 群聊分析</h1>
          <p className="text-xs text-muted-foreground mt-1">
            Telegram Chat Analyzer
          </p>
        </div>
        <nav className="flex-1 p-2 space-y-1">
          {navItems.map(({ to, icon: Icon, label }) => (
            <NavLink
              key={to}
              to={to}
              end={to === "/"}
              className={({ isActive }) =>
                `flex items-center gap-3 px-3 py-2 rounded-md text-sm transition-colors ${
                  isActive
                    ? "bg-primary text-primary-foreground"
                    : "text-muted-foreground hover:bg-accent hover:text-accent-foreground"
                }`
              }
            >
              <Icon size={18} />
              {label}
            </NavLink>
          ))}
        </nav>
      </aside>

      {/* Main */}
      <main className="flex-1 flex flex-col overflow-hidden">
        {/* Header */}
        <header className="h-10 border-b border-border bg-card/40 flex items-center justify-end px-4 gap-2 shrink-0">
          <RunningRunsBadge />
        </header>
        <div className={`flex-1 overflow-auto ${isFullWidth ? "" : ""}`}>
          {isFullWidth ? (
            <Outlet />
          ) : (
            <div className="max-w-6xl mx-auto p-6">
              <Outlet />
            </div>
          )}
        </div>
      </main>
    </div>
  );
}
