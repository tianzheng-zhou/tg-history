import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  MessageSquare,
  Users,
  Calendar,
  FolderOpen,
  ArrowRight,
} from "lucide-react";
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from "recharts";
import { getChats, getChatStats } from "@/lib/api";

function StatCard({ icon: Icon, label, value, color = "text-primary" }) {
  return (
    <div className="bg-card border border-border rounded-lg p-4">
      <div className="flex items-center gap-3">
        <div className={`p-2 rounded-md bg-primary/10 ${color}`}>
          <Icon size={20} />
        </div>
        <div>
          <p className="text-2xl font-bold">{value}</p>
          <p className="text-sm text-muted-foreground">{label}</p>
        </div>
      </div>
    </div>
  );
}

export default function Dashboard() {
  const [chats, setChats] = useState([]);
  const [stats, setStats] = useState(null);
  const [selectedChat, setSelectedChat] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    getChats()
      .then((data) => {
        setChats(data);
        if (data.length > 0) {
          setSelectedChat(data[0].chat_id);
        }
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    if (selectedChat) {
      getChatStats(selectedChat)
        .then(setStats)
        .catch(() => setStats(null));
    }
  }, [selectedChat]);

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <p className="text-muted-foreground">加载中...</p>
      </div>
    );
  }

  if (chats.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center h-96 gap-4">
        <FolderOpen size={64} className="text-muted-foreground/50" />
        <h2 className="text-xl font-semibold">还没有导入任何群聊</h2>
        <p className="text-muted-foreground">上传 Telegram 导出的 JSON 文件开始分析</p>
        <Link
          to="/import"
          className="inline-flex items-center gap-2 bg-primary text-primary-foreground px-4 py-2 rounded-md hover:opacity-90 transition-opacity"
        >
          导入数据 <ArrowRight size={16} />
        </Link>
      </div>
    );
  }

  return (
    <div>
      <h1 className="text-2xl font-bold mb-6">仪表盘</h1>

      {/* 群聊选择 */}
      <div className="mb-6">
        <label className="text-sm text-muted-foreground mb-2 block">选择群聊</label>
        <select
          className="border border-border rounded-md px-3 py-2 bg-card text-sm w-full max-w-xs"
          value={selectedChat || ""}
          onChange={(e) => setSelectedChat(e.target.value)}
        >
          {chats.map((c) => (
            <option key={c.chat_id} value={c.chat_id}>
              {c.chat_name} ({c.message_count} 条消息)
            </option>
          ))}
        </select>
      </div>

      {stats && (
        <>
          {/* 统计卡片 */}
          <div className="grid grid-cols-1 md:grid-cols-4 gap-4 mb-6">
            <StatCard
              icon={MessageSquare}
              label="总消息数"
              value={stats.message_count.toLocaleString()}
            />
            <StatCard
              icon={Users}
              label="活跃发言人"
              value={stats.top_senders.length}
            />
            <StatCard
              icon={Calendar}
              label="时间跨度"
              value={stats.date_range}
            />
            <StatCard
              icon={FolderOpen}
              label="话题数"
              value={stats.topic_count}
            />
          </div>

          {/* 每日消息图表 */}
          {stats.messages_per_day.length > 0 && (
            <div className="bg-card border border-border rounded-lg p-4 mb-6">
              <h3 className="text-sm font-semibold mb-4">每日消息量</h3>
              <ResponsiveContainer width="100%" height={250}>
                <BarChart data={stats.messages_per_day}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                  <XAxis
                    dataKey="date"
                    tick={{ fontSize: 11 }}
                    interval="preserveStartEnd"
                  />
                  <YAxis tick={{ fontSize: 11 }} />
                  <Tooltip />
                  <Bar dataKey="count" fill="#2563eb" radius={[2, 2, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          )}

          {/* Top 发言人 */}
          <div className="bg-card border border-border rounded-lg p-4">
            <h3 className="text-sm font-semibold mb-3">活跃发言人 Top 10</h3>
            <div className="space-y-2">
              {stats.top_senders.map((s, i) => (
                <div key={i} className="flex items-center gap-3">
                  <span className="text-xs text-muted-foreground w-5">
                    {i + 1}
                  </span>
                  <div className="flex-1">
                    <div className="flex justify-between text-sm">
                      <span>{s.sender}</span>
                      <span className="text-muted-foreground">{s.count}</span>
                    </div>
                    <div className="h-1.5 bg-secondary rounded-full mt-1">
                      <div
                        className="h-full bg-primary rounded-full"
                        style={{
                          width: `${(s.count / stats.top_senders[0].count) * 100}%`,
                        }}
                      />
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
