"""Qwen 缓存行为诊断脚本。

目的：摸清 qwen3.5-plus 的隐式/显式缓存实际行为，决定是否接入显式缓存。

Usage:
    python scripts/test_qwen_cache.py
    python scripts/test_qwen_cache.py --model qwen3.6-plus
    python scripts/test_qwen_cache.py --skip D
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI


# ---------- 环境 ----------

def load_env() -> None:
    root = Path(__file__).resolve().parents[1]
    env_file = root / ".env"
    if env_file.exists():
        load_dotenv(env_file)


# ---------- usage 提取 ----------

def _g(obj: Any, *keys: str, default: Any = None) -> Any:
    """支持 dict / 对象 逐级取值"""
    cur = obj
    for k in keys:
        if cur is None:
            return default
        if isinstance(cur, dict):
            cur = cur.get(k)
        else:
            cur = getattr(cur, k, None)
    return cur if cur is not None else default


def extract_usage(usage_obj: Any) -> dict:
    """从 OpenAI SDK 的 usage 对象提取所有感兴趣的字段。"""
    if usage_obj is None:
        return {"raw": None}

    raw: dict = {}
    if hasattr(usage_obj, "model_dump"):
        try:
            raw = usage_obj.model_dump(exclude_none=False)
        except Exception:
            pass

    details = _g(usage_obj, "prompt_tokens_details")
    cache_creation = _g(usage_obj, "cache_creation")

    return {
        "prompt_tokens": _g(usage_obj, "prompt_tokens", default=0) or 0,
        "completion_tokens": _g(usage_obj, "completion_tokens", default=0) or 0,
        "total_tokens": _g(usage_obj, "total_tokens", default=0) or 0,
        "cached_tokens": (_g(details, "cached_tokens", default=0) or 0) if details is not None else 0,
        "cache_creation_input_tokens": (_g(details, "cache_creation_input_tokens", default=0) or 0) if details is not None else 0,
        "ephemeral_5m_input_tokens": (_g(cache_creation, "ephemeral_5m_input_tokens", default=0) or 0) if cache_creation is not None else 0,
        "cache_type": _g(cache_creation, "cache_type") if cache_creation is not None else None,
        "raw": raw,
    }


# ---------- 长 system prompt（~2500 token）----------

def build_long_system() -> str:
    head = (
        "你是一个专业的技术助手，擅长分析 Telegram 群聊记录并提取关键信息。\n\n"
        "以下是一段群聊记录的节选：\n\n"
    )
    body = (
        "【用户A】: 最近在研究 GPU 云服务，有没有推荐的？阿里云和腾讯云的价格差距挺大的，想对比一下。\n"
        "【用户B】: 我们公司用的是 Lambda Labs，H100 每小时 1.89 美元，性价比挺高。\n"
        "【用户C】: 国内的话 AutoDL 不错，按秒计费，3090 3 毛钱一小时。\n"
        "【用户D】: 生产环境还是推荐阿里云 PAI-DLC，稳定性好，但价格贵。\n"
        "【用户A】: 我们算力需求不大，但要长期跑，怎么选？\n"
        "【用户B】: 长期跑可以考虑包月，UCloud 和七牛云都有套餐。\n"
    ) * 30
    tail = (
        "\n请基于以上信息回答用户的问题。回答要求：\n"
        "1. 必须引用具体的消息日期和发言人\n"
        "2. 不要编造内容\n"
        "3. 如果信息不足，明确说明\n"
    )
    return head + body + tail


# ---------- 调用辅助 ----------

def call_streaming(client: OpenAI, model: str, messages: list[dict], max_tokens: int = 50) -> dict:
    stream = client.chat.completions.create(
        model=model,
        messages=messages,
        stream=True,
        stream_options={"include_usage": True},
        max_tokens=max_tokens,
        temperature=0,
    )
    last_usage = None
    parts: list[str] = []
    for chunk in stream:
        u = getattr(chunk, "usage", None)
        if u is not None:
            last_usage = u
        if chunk.choices:
            delta = chunk.choices[0].delta
            if delta and delta.content:
                parts.append(delta.content)
    return {"content": "".join(parts), "usage": extract_usage(last_usage)}


def call_non_streaming(client: OpenAI, model: str, messages: list[dict], max_tokens: int = 50) -> dict:
    resp = client.chat.completions.create(
        model=model,
        messages=messages,
        stream=False,
        max_tokens=max_tokens,
        temperature=0,
    )
    return {
        "content": resp.choices[0].message.content or "",
        "usage": extract_usage(getattr(resp, "usage", None)),
    }


def print_usage_row(idx: int, usage: dict, note: str = "") -> None:
    p = usage.get("prompt_tokens", 0)
    c = usage.get("cached_tokens", 0)
    cr = usage.get("cache_creation_input_tokens", 0)
    eph = usage.get("ephemeral_5m_input_tokens", 0)
    out = usage.get("completion_tokens", 0)
    hit_pct = (c / p * 100) if p else 0.0
    print(
        f"  #{idx}  prompt={p:>6}  cached={c:>6} ({hit_pct:5.1f}%)  "
        f"created={cr:>6}  eph_5m={eph:>6}  out={out:>4}  {note}"
    )


# ---------- 各 Scene ----------

def scenario_a(client: OpenAI, model: str) -> list[dict]:
    """隐式缓存 baseline（流式，3 次相同请求）"""
    print(f"\n=== Scene A: 隐式缓存 baseline (model={model}, stream=True) ===")
    messages = [
        {"role": "system", "content": build_long_system()},
        {"role": "user", "content": "请用一句话总结上文。"},
    ]
    results = []
    for i in range(1, 4):
        r = call_streaming(client, model, messages)
        print_usage_row(i, r["usage"])
        results.append(r["usage"])
        time.sleep(0.5)
    if results and results[0].get("raw"):
        raw_str = json.dumps(results[0]["raw"], ensure_ascii=False, indent=2)
        print("  [raw usage #1]:", raw_str[:800])
    return results


def scenario_b(client: OpenAI, model: str) -> list[dict]:
    """显式 cache_control（流式，3 次相同请求）"""
    print(f"\n=== Scene B: 显式 cache_control (model={model}, stream=True) ===")
    long_sys = build_long_system()
    messages = [
        {
            "role": "system",
            "content": [
                {
                    "type": "text",
                    "text": long_sys,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        },
        {"role": "user", "content": "请用一句话总结上文。"},
    ]
    results = []
    for i in range(1, 4):
        try:
            r = call_streaming(client, model, messages)
            print_usage_row(i, r["usage"])
            results.append(r["usage"])
            if i == 1 and r["usage"].get("raw"):
                raw_str = json.dumps(r["usage"]["raw"], ensure_ascii=False, indent=2)
                print("  [raw usage #1]:", raw_str[:800])
        except Exception as e:
            print(f"  #{i}  ERROR: {e}")
            results.append({"error": str(e)})
        time.sleep(0.5)
    return results


def _scenario_c_once(
    client: OpenAI, model: str, use_cache_control: bool
) -> list[dict]:
    label = "显式" if use_cache_control else "隐式"
    print(f"\n=== Scene C ({label}): 5 轮 Agent 模拟 (model={model}) ===")
    long_sys = build_long_system()
    base: list[dict] = [{"role": "system", "content": long_sys}]
    results = []
    tool_blob = "工具返回：模拟群聊数据片段 " * 40
    for i in range(1, 6):
        round_msgs = base + [{"role": "user", "content": f"步骤 {i}: 继续分析"}]
        if use_cache_control:
            # 找最后一条 assistant（作为"工具前"的锚点）把它 content 升级成 array + cache_control
            for idx in range(len(round_msgs) - 1, -1, -1):
                m = round_msgs[idx]
                if m.get("role") == "assistant" and m.get("content"):
                    text = m["content"] if isinstance(m["content"], str) else ""
                    if text:
                        round_msgs[idx] = {
                            **m,
                            "content": [
                                {
                                    "type": "text",
                                    "text": text,
                                    "cache_control": {"type": "ephemeral"},
                                }
                            ],
                        }
                    break
            # 第 1 轮只有 system，无 assistant 可标 —— 退而求其次给 system 打
            if not any(m.get("role") == "assistant" for m in round_msgs):
                first = round_msgs[0]
                text = first["content"] if isinstance(first["content"], str) else ""
                round_msgs[0] = {
                    **first,
                    "content": [
                        {
                            "type": "text",
                            "text": text,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                }
        try:
            r = call_streaming(client, model, round_msgs)
            print_usage_row(i, r["usage"])
            results.append(r["usage"])
        except Exception as e:
            print(f"  #{i}  ERROR: {e}")
            results.append({"error": str(e)})
            break
        # 追加进 base（下一轮前缀）。为简化，不用 tool role，用 user 模拟返回
        base.append({"role": "user", "content": f"步骤 {i}: 继续分析"})
        base.append(
            {
                "role": "assistant",
                "content": (r["content"] or "ok") + "\n\n[模拟工具结果]\n" + tool_blob,
            }
        )
        time.sleep(0.5)
    return results


def scenario_d(client: OpenAI, model: str) -> list[dict]:
    """非流式对比 baseline"""
    print(f"\n=== Scene D: 隐式缓存 (model={model}, stream=False) ===")
    messages = [
        {"role": "system", "content": build_long_system()},
        {"role": "user", "content": "请用一句话总结上文（非流式）。"},
    ]
    results = []
    for i in range(1, 3):
        try:
            r = call_non_streaming(client, model, messages)
            print_usage_row(i, r["usage"])
            results.append(r["usage"])
            if i == 1 and r["usage"].get("raw"):
                raw_str = json.dumps(r["usage"]["raw"], ensure_ascii=False, indent=2)
                print("  [raw usage #1]:", raw_str[:800])
        except Exception as e:
            print(f"  #{i}  ERROR: {e}")
            results.append({"error": str(e)})
        time.sleep(0.5)
    return results


# ---------- 摘要 ----------

def hit_rate(usage: dict) -> float:
    p = usage.get("prompt_tokens", 0) or 0
    c = usage.get("cached_tokens", 0) or 0
    return (c / p * 100) if p else 0.0


def summarize(results: dict) -> None:
    print("\n" + "=" * 60)
    print("摘要")
    print("=" * 60)

    a = results.get("A", [])
    if a:
        print(
            f"Scene A 隐式命中率: "
            + "  ".join(f"#{i+1}={hit_rate(r):5.1f}%" for i, r in enumerate(a) if "error" not in r)
        )

    b = results.get("B", [])
    if b:
        if any("error" in r for r in b):
            err = next((r["error"] for r in b if "error" in r), "")
            print(f"Scene B 显式 cache_control 失败: {err[:200]}")
        else:
            print(
                f"Scene B 显式命中率: "
                + "  ".join(f"#{i+1}={hit_rate(r):5.1f}%" for i, r in enumerate(b))
            )
            creates = [r.get("cache_creation_input_tokens", 0) for r in b]
            print(f"Scene B 显式创建:  " + "  ".join(f"#{i+1}={v:>6}" for i, v in enumerate(creates)))

    ci = results.get("C_implicit", [])
    ce = results.get("C_explicit", [])
    if ci or ce:
        print("\nScene C 多步对比:")
        n = max(len(ci), len(ce))
        for i in range(n):
            ri = ci[i] if i < len(ci) else {}
            re = ce[i] if i < len(ce) else {}
            if "error" in ri or "error" in re:
                print(f"  第{i+1}轮  含错误")
                continue
            print(
                f"  第{i+1}轮  "
                f"隐式 cached/prompt={ri.get('cached_tokens', 0)}/{ri.get('prompt_tokens', 0)} ({hit_rate(ri):5.1f}%)  "
                f"显式 cached/prompt={re.get('cached_tokens', 0)}/{re.get('prompt_tokens', 0)} ({hit_rate(re):5.1f}%)  "
                f"显式 created={re.get('cache_creation_input_tokens', 0)}"
            )

    d = results.get("D", [])
    if d:
        print(
            f"\nScene D 非流式命中率: "
            + "  ".join(f"#{i+1}={hit_rate(r):5.1f}%" for i, r in enumerate(d) if "error" not in r)
        )

    # 结论建议
    print("\n--- 结论建议 ---")
    if len(a) >= 2 and "error" not in a[1]:
        rate_a2 = hit_rate(a[1])
        if rate_a2 > 60:
            print(f"  隐式 #{2} 命中率 {rate_a2:.1f}% > 60% → 可跳过显式接入")
        elif rate_a2 < 30:
            print(f"  隐式 #{2} 命中率 {rate_a2:.1f}% < 30% → 强烈建议接入显式缓存")
        else:
            print(f"  隐式 #{2} 命中率 {rate_a2:.1f}% 在灰区 → 显式接入可选")
    if b and "error" in b[0]:
        print(f"  显式缓存调用失败 → 可能 qwen 不支持 cache_control 或格式不对")
    elif b and b[0].get("cache_creation_input_tokens", 0) > 0:
        print("  显式 cache_control 工作正常 → 可在 Agent 循环中使用")
    elif b and len(b) >= 2 and b[1].get("cached_tokens", 0) > 0:
        print("  显式命中有数据返回 → cache_control 生效")


def main():
    load_env()
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="qwen3.5-plus")
    parser.add_argument("--skip", nargs="*", default=[], choices=["A", "B", "C", "D"])
    args = parser.parse_args()

    api_key = os.getenv("DASHSCOPE_API_KEY")
    base_url = os.getenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    if not api_key:
        print("ERROR: DASHSCOPE_API_KEY not found in .env")
        sys.exit(1)

    client = OpenAI(api_key=api_key, base_url=base_url)
    print(f"Model:    {args.model}")
    print(f"Base URL: {base_url}")

    results: dict = {}
    if "A" not in args.skip:
        results["A"] = scenario_a(client, args.model)
    if "B" not in args.skip:
        results["B"] = scenario_b(client, args.model)
    if "C" not in args.skip:
        results["C_implicit"] = _scenario_c_once(client, args.model, use_cache_control=False)
        time.sleep(1.0)
        results["C_explicit"] = _scenario_c_once(client, args.model, use_cache_control=True)
    if "D" not in args.skip:
        results["D"] = scenario_d(client, args.model)

    summarize(results)


if __name__ == "__main__":
    main()
