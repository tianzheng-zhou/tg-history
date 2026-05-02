import { NavLink, Outlet } from "react-router-dom";
import {
  LayoutDashboard,
  Upload,
  Database,
  FileText,
  MessageCircleQuestion,
  Settings,
} from "lucide-react";

const navItems = [
  { to: "/", icon: LayoutDashboard, label: "仪表盘" },
  { to: "/import", icon: Upload, label: "数据导入" },
  { to: "/index", icon: Database, label: "索引管理" },
  { to: "/summary", icon: FileText, label: "摘要报告" },
  { to: "/qa", icon: MessageCircleQuestion, label: "智能问答" },
  { to: "/settings", icon: Settings, label: "设置" },
];

export default function Layout() {
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
      <main className="flex-1 overflow-auto">
        <div className="max-w-6xl mx-auto p-6">
          <Outlet />
        </div>
      </main>
    </div>
  );
}
