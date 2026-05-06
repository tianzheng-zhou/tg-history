r"""验证多轮对话完整工具结果回放的核心数据流（不调真实 LLM）。

测试用例：
  1. Run.tool_outputs 通过 _emit 正确收集
  2. _build_trajectory 把 tool_outputs 合并到 tool_calls[i].output
  3. _has_full_tool_outputs 判断逻辑
  4. _replay_assistant_turn_full 还原成 OpenAI messages 序列（顺序 + tool_call_id 配对）
  5. get_history_messages 端到端路径
  6. feature flag 关闭时退回旧行为

跑法：venv\Scripts\python scripts\_test_full_replay.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.config import settings
from backend.services import run_registry, session_service


# ---------- 1. tool_outputs 收集 ----------

def test_emit_collects_tool_outputs():
    run = run_registry.Run(
        id="run-1", session_id="sess-1", mode="agent", question="q",
    )
    run_registry._emit(run, {"type": "step_start", "step": 1})
    run_registry._emit(run, {"type": "thinking_delta", "step": 1, "text": "let me search"})
    run_registry._emit(run, {
        "type": "tool_call", "step": 1,
        "id": "call_abc", "name": "keyword_search",
        "args": {"keywords": ["EFunCard"], "limit": 30},
    })
    run_registry._emit(run, {
        "type": "tool_result", "step": 1,
        "id": "call_abc", "name": "keyword_search",
        "output_preview": {"count": 3, "items": []},
        "output_full": '{"results":[{"message_id":1234,"text":"hi"}],"count":3}',
        "duration_ms": 42, "error": False,
    })
    run_registry._emit(run, {"type": "step_done", "step": 1, "had_tool_calls": True})

    assert run.tool_outputs == {
        "call_abc": '{"results":[{"message_id":1234,"text":"hi"}],"count":3}'
    }, f"unexpected: {run.tool_outputs}"

    # output_full 应该已经从 events 里 pop 走了
    tr_events = [e for e in run.events if e.get("type") == "tool_result"]
    assert len(tr_events) == 1
    assert "output_full" not in tr_events[0], "output_full 不应该泄漏到 run.events"
    assert tr_events[0]["output_preview"] == {"count": 3, "items": []}
    print("[OK] _emit collects tool_outputs and strips output_full from events")


# ---------- 2. _build_trajectory 合并 ----------

def test_build_trajectory_merges_outputs():
    run = run_registry.Run(id="run-2", session_id="sess-2", mode="agent", question="q")
    run_registry._emit(run, {"type": "step_start", "step": 1})
    run_registry._emit(run, {
        "type": "tool_call", "step": 1, "id": "c1", "name": "list_chats", "args": {},
    })
    run_registry._emit(run, {
        "type": "tool_result", "step": 1, "id": "c1", "name": "list_chats",
        "output_preview": {"count": 2}, "output_full": '{"chats":[1,2]}',
        "duration_ms": 5, "error": False,
    })
    run_registry._emit(run, {"type": "step_done", "step": 1, "had_tool_calls": True})
    run_registry._emit(run, {"type": "step_start", "step": 2})
    run_registry._emit(run, {"type": "thinking_delta", "step": 2, "text": "found 2 chats"})
    run_registry._emit(run, {"type": "step_done", "step": 2, "had_tool_calls": False})

    traj = run_registry._build_trajectory(run)
    assert "steps" in traj and len(traj["steps"]) == 2

    s1 = traj["steps"][0]
    assert s1["had_tool_calls"] is True
    assert len(s1["tool_calls"]) == 1
    tc1 = s1["tool_calls"][0]
    assert tc1["id"] == "c1"
    assert tc1["name"] == "list_chats"
    assert tc1["output"] == '{"chats":[1,2]}', f"output mismatch: {tc1.get('output')}"
    assert tc1.get("preview") == {"count": 2}

    s2 = traj["steps"][1]
    assert s2["had_tool_calls"] is False
    assert s2["thinking"] == "found 2 chats"
    print("[OK] _build_trajectory merges tool_outputs into tool_calls[i].output")


# ---------- 3. _has_full_tool_outputs ----------

def test_has_full_tool_outputs():
    assert session_service._has_full_tool_outputs(None) is False
    assert session_service._has_full_tool_outputs({}) is False
    assert session_service._has_full_tool_outputs({"steps": []}) is False
    assert session_service._has_full_tool_outputs({"steps": [{"tool_calls": []}]}) is False
    # 老 trajectory（只有 preview，没 output）
    old = {"steps": [{"tool_calls": [{"id": "x", "preview": {"k": 1}}]}]}
    assert session_service._has_full_tool_outputs(old) is False
    # 新 trajectory（有 output）
    new = {"steps": [{"tool_calls": [{"id": "x", "preview": {}, "output": '{"a":1}'}]}]}
    assert session_service._has_full_tool_outputs(new) is True
    print("[OK] _has_full_tool_outputs differentiates old vs new trajectory")


# ---------- 4. _replay_assistant_turn_full ----------

class _FakeTurn:
    """模拟 ChatTurn ORM 对象（避免起 DB session）"""
    def __init__(self, role, content, trajectory=None, meta=None, turn_id=99):
        self.id = turn_id
        self.role = role
        self.content = content
        self.trajectory = json.dumps(trajectory) if trajectory else None
        self.meta = json.dumps(meta) if meta else None


def test_replay_full_two_steps():
    """两步：第 1 步调工具，第 2 步给最终答案"""
    trajectory = {
        "steps": [
            {
                "step": 1,
                "thinking": "let me search EFunCard",
                "reasoning": "",
                "tool_calls": [
                    {
                        "id": "call_abc",
                        "name": "keyword_search",
                        "args": {"keywords": ["EFunCard"]},
                        "preview": {"count": 3},
                        "output": '{"results":[{"message_id":1234,"text":"hi"}],"count":1}',
                    }
                ],
                "had_tool_calls": True,
            },
            {
                "step": 2,
                "thinking": "draft answer in step 2",
                "reasoning": "",
                "tool_calls": [],
                "had_tool_calls": False,
            },
        ]
    }
    turn = _FakeTurn(role="assistant",
                      content="Final answer text",
                      trajectory=trajectory)

    msgs = session_service._replay_assistant_turn_full(turn)

    # 期望顺序：assistant(content+tool_calls) → tool → assistant(final answer)
    assert len(msgs) == 3, f"expected 3 messages, got {len(msgs)}: {msgs}"

    # M0: assistant with tool_calls
    assert msgs[0]["role"] == "assistant"
    assert msgs[0]["content"] == "let me search EFunCard"
    assert len(msgs[0]["tool_calls"]) == 1
    tc = msgs[0]["tool_calls"][0]
    assert tc["id"] == "call_abc"
    assert tc["type"] == "function"
    assert tc["function"]["name"] == "keyword_search"
    args_obj = json.loads(tc["function"]["arguments"])
    assert args_obj == {"keywords": ["EFunCard"]}

    # M1: tool result，tool_call_id 必须配对
    assert msgs[1]["role"] == "tool"
    assert msgs[1]["tool_call_id"] == "call_abc"
    assert msgs[1]["content"] == '{"results":[{"message_id":1234,"text":"hi"}],"count":1}'

    # M2: assistant final answer，优先用 turn.content（数据库纯净版）
    assert msgs[2]["role"] == "assistant"
    assert msgs[2]["content"] == "Final answer text"
    assert "tool_calls" not in msgs[2]
    print("[OK] _replay_assistant_turn_full: 2-step trajectory replay correct")


def test_replay_full_with_reasoning():
    """Kimi 思考链 reasoning_content 必须保留"""
    trajectory = {
        "steps": [
            {
                "step": 1,
                "thinking": "",
                "reasoning": "internal thinking content...",
                "tool_calls": [
                    {
                        "id": "c1",
                        "name": "list_chats",
                        "args": {},
                        "preview": {"count": 0},
                        "output": '{"chats":[]}',
                    }
                ],
                "had_tool_calls": True,
            },
        ]
    }
    turn = _FakeTurn(role="assistant", content="No chats found.", trajectory=trajectory)
    msgs = session_service._replay_assistant_turn_full(turn)

    # 只有 1 个 step 但带 tool_calls；最终答案在 turn.content 但不会作为额外 message 出现
    # 只重放 trajectory 的 steps；最终答案的回放靠最后那个 no-tool_calls 的 step
    # 这里 step 1 had_tool_calls=True 且是最后一步 → 只产出 assistant(tool_calls) + tool
    assert len(msgs) == 2
    assert msgs[0]["role"] == "assistant"
    assert msgs[0].get("reasoning_content") == "internal thinking content..."
    assert "tool_calls" in msgs[0]
    assert msgs[1]["role"] == "tool"
    print("[OK] _replay_assistant_turn_full: reasoning_content preserved (Kimi compat)")


def test_replay_old_trajectory_fallback():
    """老 trajectory 没有 output 字段 → tool 消息用 preview 兜底"""
    trajectory = {
        "steps": [
            {
                "step": 1,
                "thinking": "",
                "tool_calls": [
                    {"id": "c1", "name": "X", "args": {}, "preview": {"count": 5}}
                ],
                "had_tool_calls": True,
            }
        ]
    }
    turn = _FakeTurn(role="assistant", content="", trajectory=trajectory)
    msgs = session_service._replay_assistant_turn_full(turn)
    # 这种 turn 一般不会被 get_history_messages 选中走 full replay（因为 _has_full 返回 False），
    # 但 _replay_assistant_turn_full 自身要能 graceful 处理
    assert len(msgs) == 2
    assert msgs[1]["role"] == "tool"
    parsed = json.loads(msgs[1]["content"])
    assert parsed == {"count": 5}
    print("[OK] _replay_assistant_turn_full: fallback to preview when no output")


# ---------- 5. tool_call_id 配对一致性 ----------

def test_tool_call_id_pairing():
    """assistant.tool_calls[i].id 必须和后跟的 tool.tool_call_id 一一对应（OpenAI 严格要求）"""
    trajectory = {
        "steps": [
            {
                "step": 1,
                "thinking": "",
                "tool_calls": [
                    {"id": "a", "name": "T", "args": {}, "output": '{"a":1}'},
                    {"id": "b", "name": "T", "args": {}, "output": '{"b":2}'},
                    {"id": "c", "name": "T", "args": {}, "output": '{"c":3}'},
                ],
                "had_tool_calls": True,
            },
        ]
    }
    turn = _FakeTurn(role="assistant", content="", trajectory=trajectory)
    msgs = session_service._replay_assistant_turn_full(turn)

    asst_ids = [tc["id"] for tc in msgs[0]["tool_calls"]]
    tool_ids = [m["tool_call_id"] for m in msgs[1:] if m["role"] == "tool"]
    assert asst_ids == tool_ids == ["a", "b", "c"], f"id 顺序错位: {asst_ids} vs {tool_ids}"
    print("[OK] tool_call_id pairing strictly preserved (a/b/c -> a/b/c)")


# ---------- 6. feature flag 关闭路径 ----------

def test_feature_flag_off_falls_back_to_legacy():
    """关闭 enable_full_history_replay 时只回放 user/assistant content"""
    original = settings.enable_full_history_replay
    try:
        settings.enable_full_history_replay = False

        # 模拟 turns
        u_turn = _FakeTurn(role="user", content="问题1",
                            meta={"injected_prefix": "[t=now]"}, turn_id=1)
        a_turn = _FakeTurn(
            role="assistant", content="答案1",
            trajectory={"steps": [{"tool_calls": [
                {"id": "x", "name": "T", "args": {}, "output": '{"a":1}'}
            ]}]},
            turn_id=2,
        )

        # mock get_turns
        original_get_turns = session_service.get_turns
        session_service.get_turns = lambda db, sid: [u_turn, a_turn]
        try:
            msgs = session_service.get_history_messages(None, "sess")
        finally:
            session_service.get_turns = original_get_turns

        assert len(msgs) == 2
        assert msgs[0]["role"] == "user"
        assert msgs[0]["content"] == "[t=now]\n\n---\n\n问题1"
        assert msgs[1]["role"] == "assistant"
        assert msgs[1]["content"] == "答案1"
        print("[OK] feature flag OFF -> legacy text-only replay")
    finally:
        settings.enable_full_history_replay = original


# ---------- 7. feature flag 开启端到端 ----------

def test_full_replay_e2e():
    original = settings.enable_full_history_replay
    try:
        settings.enable_full_history_replay = True

        u1 = _FakeTurn(role="user", content="问题1",
                        meta={"injected_prefix": "[t=now]"}, turn_id=1)
        a1 = _FakeTurn(role="assistant", content="答案1", trajectory={
            "steps": [
                {"step": 1, "thinking": "thinking 1",
                 "tool_calls": [{"id": "tc1", "name": "list_chats", "args": {},
                                  "output": '{"chats":[]}', "preview": {"count": 0}}],
                 "had_tool_calls": True},
                {"step": 2, "thinking": "draft", "tool_calls": [],
                 "had_tool_calls": False},
            ]
        }, turn_id=2)
        u2 = _FakeTurn(role="user", content="问题2", turn_id=3)

        original_get_turns = session_service.get_turns
        session_service.get_turns = lambda db, sid: [u1, a1, u2]
        try:
            msgs = session_service.get_history_messages(None, "sess")
        finally:
            session_service.get_turns = original_get_turns

        # 期望：user1, assistant(tool_calls), tool, assistant(final), user2
        assert len(msgs) == 5, f"got {len(msgs)} msgs: {[m['role'] for m in msgs]}"
        roles = [m["role"] for m in msgs]
        assert roles == ["user", "assistant", "tool", "assistant", "user"], roles

        assert "tool_calls" in msgs[1]
        assert msgs[2]["tool_call_id"] == "tc1"
        assert msgs[2]["content"] == '{"chats":[]}'
        assert msgs[3]["content"] == "答案1"  # 用 turn.content
        assert "tool_calls" not in msgs[3]
        print("[OK] full replay E2E: user/assistant/tool/assistant/user sequence")
    finally:
        settings.enable_full_history_replay = original


if __name__ == "__main__":
    test_emit_collects_tool_outputs()
    test_build_trajectory_merges_outputs()
    test_has_full_tool_outputs()
    test_replay_full_two_steps()
    test_replay_full_with_reasoning()
    test_replay_old_trajectory_fallback()
    test_tool_call_id_pairing()
    test_feature_flag_off_falls_back_to_legacy()
    test_full_replay_e2e()
    print("\n[ALL PASS] full replay data flow verified")
