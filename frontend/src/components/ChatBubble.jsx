export default function ChatBubble({ sender, date, text, isAI = false }) {
  return (
    <div
      className={`flex ${isAI ? "justify-start" : "justify-end"} mb-3`}
    >
      <div
        className={`max-w-[75%] rounded-lg px-4 py-2.5 ${
          isAI
            ? "bg-card border border-border text-card-foreground"
            : "bg-primary text-primary-foreground"
        }`}
      >
        {sender && (
          <p className="text-xs font-semibold mb-1 opacity-80">{sender}</p>
        )}
        <p className="text-sm whitespace-pre-wrap">{text}</p>
        {date && (
          <p className="text-[10px] mt-1 opacity-60 text-right">{date}</p>
        )}
      </div>
    </div>
  );
}
