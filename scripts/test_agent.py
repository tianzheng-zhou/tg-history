"""手动测试 QA agent 流式输出"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.models.database import SessionLocal
from backend.services.qa_agent import run_agent


async def main(question: str):
    db = SessionLocal()
    try:
        async for ev in run_agent(db, question):
            t = ev.get("type")
            if t == "thinking_delta":
                print(ev["text"], end="", flush=True)
            elif t == "tool_call":
                print(f"\n>>> TOOL_CALL: {ev['name']}({ev['args']})")
            elif t == "tool_result":
                err = " [ERROR]" if ev.get("error") else ""
                print(f"<<< TOOL_RESULT: {ev['name']} ({ev['duration_ms']}ms){err}")
                print(f"    preview: {ev['output_preview']}")
            elif t == "step_start":
                print(f"\n--- step {ev['step']} ---")
            elif t == "step_done":
                print(f"\n--- step {ev['step']} done (had_tool_calls={ev.get('had_tool_calls')}) ---")
            elif t == "final_answer":
                print(f"\n\n===== FINAL ANSWER =====\n{ev['answer']}\n")
                print(f"sources: {len(ev['sources'])}")
            elif t == "error":
                print(f"\n!! ERROR: {ev['error']}")
            elif t == "status":
                print(f"[status] {ev['message']}")
    finally:
        db.close()


if __name__ == "__main__":
    q = sys.argv[1] if len(sys.argv) > 1 else "群里讨论过哪些 GPU 租赁方案？"
    asyncio.run(main(q))
