import { MessageSquare } from "lucide-react";

export default function SourceCard({ source }) {
  return (
    <div className="border border-border rounded-md p-3 bg-card hover:bg-accent/50 transition-colors">
      <div className="flex items-center gap-2 mb-1.5">
        <MessageSquare size={14} className="text-primary" />
        <span className="text-sm font-medium">{source.sender || "未知"}</span>
        {source.date && (
          <span className="text-xs text-muted-foreground">{source.date}</span>
        )}
      </div>
      <p className="text-sm text-muted-foreground line-clamp-3">
        {source.preview}
      </p>
    </div>
  );
}
