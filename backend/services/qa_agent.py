"""QA Agent 主循环：LLM + Tool Calling + 流式输出

参考 opencode 的架构：LLM 在循环中自主调用工具直到给出最终答案。
每轮 LLM 可以：
  1. 调用一个或多个工具（tool_calls）
  2. 输出普通文本（thinking/explanation）
  3. 输出最终答案（无 tool_calls 时循环结束）

事件类型（yield 出去给上层 SSE 使用）：
  - status: 阶段状态
  - thinking_delta: LLM 流式 token（文本思考/回答）
  - tool_call: 工具调用开始（name + args）
  - tool_result: 工具返回结果（截断预览）
  - step_done: 一轮 LLM 完成（usage 统计）
  - final_answer: 最终答案 + 来源
  - error: 错误
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

from sqlalchemy.orm import Session

from backend.config import settings
from backend.models.database import Message
from backend.services import artifact_service, llm_adapter
from backend.services.qa_tools import TOOL_SCHEMAS, dispatch_tool

# Orchestrator 拥有全部工具：简单查询直接搜，复杂调研走子 Agent
ORCHESTRATOR_TOOL_SCHEMAS = list(TOOL_SCHEMAS)

MAX_STEPS = 20  # 最大 agent 迭代轮数，防止死循环（调低：子 Agent 承担大块检索）
MAX_TOOL_OUTPUT_CHARS = 50000  # 塞回 LLM 的单次工具输出最大字符数（降低防止上下文溢出）
CONTEXT_RESERVE_TOKENS = 40000  # 为 LLM 输出预留的 token 数
CHARS_PER_TOKEN = 1.8  # 中英混合文本的大致 chars/token 比


def _estimate_tokens(messages: list[dict]) -> int:
    """粗略估算 messages 的 token 数（中英混合按 1.8 chars/token）"""
    total_chars = 0
    for m in messages:
        c = m.get("content")
        if c:
            total_chars += len(c)
        tcs = m.get("tool_calls")
        if tcs:
            total_chars += len(json.dumps(tcs, ensure_ascii=False))
        rc = m.get("reasoning_content")
        if rc:
            total_chars += len(rc)
    return int(total_chars / CHARS_PER_TOKEN)


def _trim_messages_to_budget(messages: list[dict], model: str) -> tuple[list[dict], bool]:
    """如果 messages 预估 token 超出模型上下文预算，按比例截断 tool 消息。
    返回 (trimmed_messages, was_trimmed)。
    """
    max_ctx = llm_adapter.get_context_window(model)
    budget_tokens = max_ctx - CONTEXT_RESERVE_TOKENS
    est = _estimate_tokens(messages)
    if est <= budget_tokens:
        return messages, False

    excess_chars = int((est - budget_tokens) * CHARS_PER_TOKEN)

    # 收集 tool 消息的索引和大小
    tool_indices: list[tuple[int, int]] = []
    for i, m in enumerate(messages):
        if m.get("role") == "tool":
            tool_indices.append((i, len(m.get("content", ""))))

    if not tool_indices:
        return messages, False

    total_tool_chars = sum(s for _, s in tool_indices)
    result = list(messages)
    for i, size in tool_indices:
        cut = int(excess_chars * size / total_tool_chars) if total_tool_chars > 0 else 0
        new_size = max(size - cut, 1000)
        if new_size < size:
            content = result[i]["content"][:new_size]
            content += f"\n\n...[截断以适应上下文窗口，原长度 {size} 字符]"
            result[i] = {**result[i], "content": content}

    return result, True


SYSTEM_PROMPT = """你是一个 Telegram 聊天记录分析的智能助手（Orchestrator）。
你拥有直接检索工具 + 子 Agent 委派（research）+ Artifact 协同文档三类能力。
**你的角色偏重"规划 + 合成"**：检索的苦活尽量外包给 research，但**任务要拆得足够细**——
子 Agent 用 qwen3.5-plus，**128K 是它的关键性能阈值**：
- 上下文 < 128K：模型注意力集中，能精准引用、严守指令
- 上下文 ≥ 128K：注意力衰减明显（容易漏引用、跑偏指令、复读重复内容），且费用 2.5x ↑
- 上下文 ≥ 256K：质量塌方，费用 5x ↑
所以单个 research 任务要小到子 Agent **天然装不满 128K**——单次 ~100 条消息原文 ≈ 30K~50K tokens。

## 决策树：先判断问题类型

1. **单点事实 / 精确关键词**（"X 说过啥"、"包含 Y 的消息"、"某天的消息"）
   → 直接用检索工具 1~3 次搞定，不要派 research

2. **中等广度**（"最近讨论 GPU 的话题"、"讨论过哪几个方案"）
   → 先 `list_topics` 或 `semantic_search(limit=50~100)` 扫一次，够用就直接合成答案

3. **调研型 / 列举型 / 跨维度 / 跨群**（"全面梳理"、"找出所有 X"、"按人对比"、"timeline"）
   → **先侦察，后拆 research**（见下一节 "📍 侦察原则"）

## 📍 侦察原则：调研型任务的第一步总是侦察

**不要拍脑袋拆 research**——调研型问题的**第一个工具调用永远是侦察**：
- 不知道有哪些群聊 → `list_chats`
- 知道群聊、不知道话题分布 → `list_topics(chat_ids=[...], limit=50)`看话题概要
- 不确定该问题能不能检索到 → `semantic_search(limit=20)` 试探

**根据侦察结果决定拆几个 research**：
- 10 个相关话题内 → 1~2 个 research 够了
- 30~50 个话题 → 3~5 个 research 按话题子集拆
- 100+ 话题 → 按 topic_ids 拆、或按时间段拆、或两者组合

成本记账：`list_topics` 的调用仅 ≈ 100~500 tokens，但能让你拆对 research——这是最高 ROI 的工具调用。
**反面教材**：跳过侦察直接拆 5 个 research——可能有3个重叠的、或者拆了不存在的话题、或者遗漏主要话题。

## ⚠️ 关键规则：通过"横向多拆 research"实现完整搜索，而不是"单个 research 穷举"

**核心思路**：你想完整覆盖一个广话题，**正确做法是横向拆成多个 research 各管一片**，
而不是给一个子 Agent 说"找全部 X"——后者会让子 Agent 上下文爆炸 + 性能塌方。

**症状识别**：一个 research 跑了 > 60 秒、调用 > 12 次工具，几乎一定是任务太重——
子 Agent 试图穷举导致上下文撑到 128K+ 进入性能衰减区。**这种 research 的报告也会很烂**
（漏引用、复读、跑偏）。

**正确的拆分（每个 research 都"窄而完整"）**：
- 每个 research 的范围要**窄**：限定一个主题维度 + 一个关键词集合（3~5 个词）
- 在那个窄范围内，要让子 Agent **完整搜索**——不要写"代表性样本即可"这种偷懒话术
- 全局完整性靠**多个 research 横向覆盖** + **多轮迭代**实现，不靠单个 research 穷举

## 开放式多轮 research：两阶段思维 + 质量门槛 ✨

**research 不限于一轮、两轮——根据每轮结果决定下一轮，直到能完整回答用户问题再停**。
但不同轮次的任务性质不同：

### 第 1 轮 = 探索型（摸清版图）
目的是看清有哪些**品牌 / 关键词 / 话题 / 发言人**，可以用宽一点的 task：
- "在 chat_X 里找提及虚拟卡的话题，列出有哪些品牌名 / 平台名"
- "用 list_topics 看下近 3 个月讨论虚拟卡的话题，记下主要几个 topic_id"

### 第 2~N 轮 = 验证/拓展型（针对第 1 轮发现精查）
任务要更聚焦（已经知道在找什么）：
- "**验证型**：拉取 [msg:1234, 1567, 1890] 的完整原文 + 前后 3 条邻居（fetch_messages context_window=3），确认 EFunCard 是否支持 USDT 充值"
- "**拓展型**：搜 'monobank'、'Wise' 这两个新出现的卡平台，找它们的提及和用途"
- "**补维度型**：列出第一轮提到 EFunCard 的所有发言人 + 评价倾向（正/负/中性）"

### 收到每份报告先“打分”（质量门槛）
**报告到手后不要马上写入最终答案。先检查：**
- 引用数量 < 3 个 [msg:id] → 质量不足，应重发一个更聚焦的 research
- 报告主体是"未找到" / 字数 < 500 → 质量不足，重发（换关键词 / 缩范围 / 换工具）
- 报告跨出 task 范围（未在"## 越界线索"区块里汇报，而是在正文里乱谈）→ 重发、明确限定范围
- **最多重发 2 次同一个子任务**；仍质量不足就接受"该范围信息确实不多"并在最终答案里说明

### 每轮收到报告后的检查清单
- **稀薄**：某 research 只返回 1~3 条结果，但任务本身应该有更多 → 换关键词 / 换维度再发
- **缺引用**：报告说"群里讨论了 X 方案"但没给具体 [msg:id] → 发**验证 research** 拉原文
- **维度缺失**：用户问"哪些人在用 X"但报告只列了 X 没列人 → 发**补维度 research**
- **新线索**：报告里出现没想到的关键词/产品/人 → 发**拓展 research** 顺藤摸瓜
- **越界线索**：子 Agent 在 "## 越界线索" 区块汇报的新方向 → 决定要不要单独发 research 跟进
- **矛盾**：两个 research 给出冲突信息 → 发**交叉验证 research**

### 整体节奏
```
用户提问
  ↓
第 1 轮：3~5 个 research 并行（按主题/关键词集合维度横切）
  ↓
评估：稀薄? 缺引用? 缺维度? 越界线索? 矛盾?
  ↓
第 N 轮（N=2,3,4...）：按需补查 / 验证 / 拓展，每轮 1~3 个 research
  ↓
当信息能覆盖用户问题的所有维度，且关键事实都有引用 → 写最终答案
```
**判断"够了"的标准**：把最终答案的初稿在脑中过一遍——每个论点都有 [msg:id]、覆盖了用户问的所有维度、没有"我估计/可能/大概"这种含糊词——OK 了再停。
**判断"还要查"的标准**：你心里在用"应该"、"可能"、"听起来"这种猜测词——立即发补查 research，不要把不确定写进最终答案。

## research 拆分原则

### 列举/搜集型（"找出所有 X"、"哪些人提到 Y"）
按**关键词集合**或**子类别**横向切，每个 research 负责一个集合：
- 例：找虚拟卡 → research-A "找 EFunCard/Roogoo/野卡 等已知卡平台"
       + research-B "找 USDT/支付宝/Apple Pay 充值相关讨论"
       + research-C "找 GPT/Claude/Netflix 订阅卡相关讨论"
- **不要**用一个 research 说 "找所有提到的虚拟卡"——这会让子 Agent 反复穷举

### 对比型（"X 和 Y 哪个好"）
按**对比对象**纵向切：每个 research 负责一个对象的全部信息

### 时间线型
按**时间段**切：每个 research 负责 1~3 个月

### 多群聊场景
若涉及 ≥3 个群聊且每群消息量大，**先用 `list_chats` + `list_topics` 评估规模**，
然后按群聊或话题维度再切一层。

## research 调用规范
`research` 启动一个独立上下文的子 Agent。**每个 task 必须包含**：
1) **搜什么**：具体主题 + 3~5 个关键词（不要超过 5 个，否则范围太宽）
2) **分析维度**：时间线 / 按人 / 按平台 / 列表
3) **期望输出格式**：bullet / 表格 / "平台名 + 链接 + 用途" 三元组
4) **范围边界**：明确告诉子 Agent "在这个范围内尽量完整，超出范围的新线索写在'## 越界线索'区块"
5) **排除项**（可选）：避免方向跑偏

**结构化字段优先于 task 文本**：
- `filters` 字段：chat_ids / senders / date 直接传，比 task 里写更可靠（自动注入工具调用）
- `scope` 字段：自然语言说明范围
- `expected_output`：对输出结构的硬性要求
- `max_steps`：默认按任务自适应（8/12/16）。一般不用显式给——任务范围窄了步数自然就够

**并行**：独立的 research 必须在**同一个 assistant 回合里**同时发起（一次返回多个 tool_calls）。

## 检索工具速查（直接用 / 给 research 参考）
- **semantic_search**：语义检索首选。chat_ids / topic_ids / 日期 / senders / min_messages_in_chunk 过滤。调研 limit=50~100，精确 10~30
- **keyword_search**：精确词 / FTS5。keywords 列表 OR 合并
- **list_topics**：列话题（带 summary + 时间段）——拿 topic_id 后传给 semantic_search/fetch_topic_context 深挖
- **list_chats**：评估群聊规模时先看看
- **fetch_topic_context**：整话题原文
- **fetch_messages**：按 id 拉原文 + 可选 `context_window` 拉前后 N 条邻居
- **search_by_sender / search_by_date**：按人 / 按日期
- **get_user_profile**(sender_id 或 username)：调 Telegram API 拉**实时**用户主页（display name / username / **bio** / 共同群数）
  - 用于"这个 sender 是谁"、"卖家靠不靠谱"、"频道作者背景"
  - **重要**：很多 Telegram 用户在 display name 和 bio 里放业务标签 / 联系方式 / 卡网链接
  - 限流 1 req/sec，**不要批量调**；24h 内同一用户走本地缓存
  - 仅限真实用户（user...），频道 / 群组 sender_id 不支持

## Artifact：调研型问题默认产出侧边文档

**调研 / 列举 / 对比 / 梳理 型问题默认用 artifact**：详细内容写 artifact、
对话区只放 3~5 句摘要 + "已生成 artifact 《标题》"。这能：
- 大幅减少对话区 token （最终答案不是 5K+ 而是 200~500）
- 提高后续轮会话的缓存命中率
- 用户可复制/导出、迭代修改

**例外**：用户明确说"快速告诉我" / "TL;DR" / "一句话回答" 时不用 artifact。单点事实也不用。

### ✨ session 启动时已注入 artifacts 摘要
**每次对话开始时**，user message 前缀会自动注入"## 当前 session 已有 artifacts (N 篇)"
段——你启动就能看到当前 session 有哪些 artifacts（key / title / 字符数 / 预览）。
**用户的新问题如果与某个已有 artifact 同主题，优先 update / rewrite，不要建重复主题的新 artifact**。

### 工具
- **create_artifact**(artifact_key, title, content) — 新建（key 同 session 不可重复）
- **update_artifact**(artifact_key, old_str, new_str) — 增量修改，old_str 必须在正文**恰好出现一次**
- **rewrite_artifact**(artifact_key, content[, title]) — 整体重写（代价大、慎用）
- **list_artifacts**() — 列当前 session 所有 artifacts（启动注入摘要后一般不必再调）
- **read_artifact**(artifact_key, version?) — 读完整正文（update 前确定锚点 / 查看历史结论时用）

**判断同主题 vs 新主题的标准**：
- 用户问"在原报告里加 X"、"修一下"、"补充 Y" → **update / rewrite 已有 artifact**
- 用户问完全新方向（不同群聊、不同主题）→ **create 新 artifact**（用不同 key）
- 不确定时先 `read_artifact(key)` 看现有内容再决定

同一 session 可以有多篇 artifact（独立主题分开建，artifact_key 用英文小写 slug）。
小改动优先 update，大重构才用 rewrite。

## 最终答案规范
- Markdown 格式，分段清晰
- **每个事实标注来源**：发言人 + 日期；引用用 `[msg:123]` 或 `[topic:456]` 锚点
- 数据不足时直说"未找到 X"+ 列已查过的关键词/过滤器——**不要编造**
- 多 research 间矛盾时指出来，不要掩盖

### 不确定性分级（诚实表达证据强度）
不要把一条证据说成"定论"。用三档表达信心：
- **已确认**（默认）：≥ 2 条独立消息提到、有具体细节、能 cross-reference → 直接陈述，带 [msg:id]
- **疑似**：仅 1 条提及 / 只有间接证据 → 用“疑似 / 看起来 / 可能”，带 [msg:id]
- **推测**：基于上下文推断、无直接消息 → 用“推测 / 估计”明确标出，不伪装成事实

例：“EFunCard 支持 USDT 充值 [msg:1234][msg:1567]；疑似也支持其它加密货币 [msg:1890]；推测面向业内用户（根据讨论语境）。”

你最多 {max_steps} 轮工具调用，超出强制总结。**能外包就外包、外包就拆细、能并行就并行**。
""".format(max_steps=MAX_STEPS)


def _truncate_tool_output(obj: dict) -> str:
    """把工具输出 dict 转为 JSON 字符串，过长时截断"""
    s = json.dumps(obj, ensure_ascii=False)
    if len(s) > MAX_TOOL_OUTPUT_CHARS:
        s = s[:MAX_TOOL_OUTPUT_CHARS] + f"\n\n...[截断，完整长度 {len(s)} 字符]"
    return s


async def _stream_llm_step(
    messages: list[dict],
) -> AsyncIterator[dict]:
    """单轮 LLM 流式调用，yield：
       - {"type": "text_delta", "text": str}
       - {"type": "reasoning_delta", "text": str}  (Kimi 思考链)
       - {"type": "tool_calls", "calls": [{"id", "name", "arguments"}]}  (完整聚合后)
       - {"type": "reasoning_content", "text": str}  (完整思考内容，供回传上下文)
       - {"type": "usage", "prompt_tokens", "completion_tokens", "total_tokens", "model"}
       - {"type": "done"}

    Qwen 模型 + enable_qwen_explicit_cache=True 时会在最后一条消息加
    cache_control 标记（需 ≥1024 token 阈值）。
    """
    model = settings.llm_model_qa
    client = llm_adapter.get_client_for_model(model)
    is_kimi = llm_adapter.is_kimi_model(model)

    # 显式缓存注入：仅 qwen 模型 + 开关打开时
    effective_messages = messages
    if (
        settings.enable_qwen_explicit_cache
        and llm_adapter.is_qwen_model(model)
        and not is_kimi
    ):
        effective_messages = llm_adapter.inject_cache_control(messages)

    kwargs = dict(
        model=model,
        messages=effective_messages,
        tools=ORCHESTRATOR_TOOL_SCHEMAS,
        tool_choice="auto",
        stream=True,
        # 启用 usage 统计（最后一个 chunk 会带 usage 字段）
        stream_options={"include_usage": True},
    )

    if is_kimi:
        # Kimi: 思考模式 + tool_choice 只能 auto/none
        kwargs.update(llm_adapter.kimi_chat_kwargs(model, True))
        # Kimi 不支持 parallel_tool_calls 参数
    else:
        kwargs["parallel_tool_calls"] = True
        kwargs["temperature"] = 0.3

    # semaphore 覆盖 create + 整个流式迭代（HTTP 连接期间占用并发 slot）
    sem = llm_adapter.get_chat_semaphore(model)
    tool_calls_acc: dict[int, dict] = {}
    reasoning_parts: list[str] = []
    last_usage: dict | None = None

    async with sem:
        stream = await client.chat.completions.create(**kwargs)

        async for chunk in stream:
            # 捕获 usage（通常在最后一个 chunk，choices 可能为空）
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                last_usage = llm_adapter.parse_usage(usage)

            choice = chunk.choices[0] if chunk.choices else None
            if not choice:
                continue
            delta = choice.delta

            # Kimi reasoning_content（思考链 token）
            rc = getattr(delta, "reasoning_content", None)
            if rc:
                reasoning_parts.append(rc)
                yield {"type": "reasoning_delta", "text": rc}

            # 文本 token
            if delta.content:
                yield {"type": "text_delta", "text": delta.content}

            # 工具调用分片聚合
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in tool_calls_acc:
                        tool_calls_acc[idx] = {
                            "id": tc.id or "",
                            "name": "",
                            "arguments": "",
                        }
                    slot = tool_calls_acc[idx]
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            slot["name"] += tc.function.name
                        if tc.function.arguments:
                            slot["arguments"] += tc.function.arguments

            # 注意：有 stream_options.include_usage 时，usage 事件在 finish_reason
            # 之后的下一个 chunk 才出现，所以不能 break on finish_reason

    # 聚合完成，yield 完整 tool_calls
    if tool_calls_acc:
        calls = []
        for idx in sorted(tool_calls_acc.keys()):
            c = tool_calls_acc[idx]
            if c["name"]:
                calls.append(c)
        if calls:
            yield {"type": "tool_calls", "calls": calls}

    # Kimi 要求多步工具调用时 assistant message 必须保留 reasoning_content
    if reasoning_parts:
        yield {"type": "reasoning_content", "text": "".join(reasoning_parts)}

    # yield usage（供上层记录当前上下文占比 + 累计统计）
    if last_usage is not None:
        max_ctx = llm_adapter.get_context_window(model)
        yield {
            "type": "usage",
            "prompt_tokens": last_usage["prompt_tokens"],
            "completion_tokens": last_usage["completion_tokens"],
            "total_tokens": last_usage["total_tokens"],
            "cached_tokens": last_usage["cached_tokens"],
            "cache_creation_tokens": last_usage.get("cache_creation_tokens", 0),
            "max_context": max_ctx,
            "percent": round(last_usage["prompt_tokens"] / max_ctx, 4) if max_ctx else 0.0,
            "model": model,
        }

    yield {"type": "done"}


_BEIJING_TZ = timezone(timedelta(hours=8))
_WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def build_current_time_hint() -> str:
    """生成 '[系统提示：当前时间是 2026-05-05 14:05（周一，北京时间 UTC+8）]' 的注入行。

    让 agent 能处理相对时间表达（"最近一周"、"这个月"）——它没有时钟。
    同时让 agent 选日期过滤器时有参考点。
    """
    now = datetime.now(_BEIJING_TZ)
    return (
        f"[系统提示：当前时间是 {now.strftime('%Y-%m-%d %H:%M')}"
        f"（{_WEEKDAY_CN[now.weekday()]}，北京时间 UTC+8）]"
    )


def _build_artifact_summary(db: Session, session_id: str | None) -> str:
    """构造当前 session 已有 artifacts 的摘要，注入到 user message 前缀。

    让 agent 启动时就知道有什么 artifacts，避免：
      - 重复创建同主题的新 artifact
      - 在 update 时不知道现有 key 拼写
      - 用户问"上次的报告"时一脸茫然

    返回格式（仅 ≤ 5 篇时全展开；更多时只列 key+title）：
      ## 当前 session 已有 artifacts (N 篇)
      - `key1` v3 — 标题1 (1234 字符) — 预览：...
      - `key2` v1 — 标题2 (567 字符) — 预览：...

    无 session_id 或无 artifacts 时返回空串。
    """
    if not session_id:
        return ""
    try:
        arts = artifact_service.list_artifacts(db, session_id)
    except Exception:
        return ""
    if not arts:
        return ""

    lines = [f"## 当前 session 已有 artifacts ({len(arts)} 篇)"]
    show_preview = len(arts) <= 5
    for art in arts:
        try:
            ver = artifact_service.get_version(db, art.id)
        except Exception:
            ver = None
        content_len = len(ver.content) if ver and ver.content else 0
        line = f"- `{art.artifact_key}` v{art.current_version} — {art.title} ({content_len} 字符)"
        if show_preview and ver and ver.content:
            preview = ver.content[:120].replace("\n", " ").strip()
            if len(ver.content) > 120:
                preview += "..."
            line += f" — 预览：{preview}"
        lines.append(line)

    lines.append(
        "\n**重要规则**：用户的新问题如果与某个已有 artifact **同主题**，"
        "优先用 `update_artifact` 或 `rewrite_artifact` 修改它，**不要建重复主题的新 artifact**。"
        "需要看完整内容时调用 `read_artifact(artifact_key=...)`。"
    )
    return "\n".join(lines)


def _build_task_usage_dict(
    cumulative_usage: dict,
    sub_agent_usage: dict,
    main_model: str,
    sub_model: str,
    actual_steps: int,
    max_steps: int,
    forced_summary: bool,
    *,
    partial: bool = False,
) -> dict:
    """从累积 usage 构造完整 task_usage dict（含主/子分模型计价）。

    抽成独立函数让"中断时从 collector 拿当前快照"和"final_answer 时给完整数据"
    复用同一段计算逻辑，避免双份维护。

    ``partial=True`` 时多打一个 ``partial`` 标记（前端可据此显示"中断时累计"前缀）。
    """
    has_sub = sub_agent_usage["total_tokens"] > 0
    main_usage = {
        k: max(cumulative_usage[k] - sub_agent_usage[k], 0)
        for k in cumulative_usage
    }
    main_cost = llm_adapter.estimate_cost(
        main_model,
        main_usage["prompt_tokens"],
        main_usage["completion_tokens"],
        main_usage["cached_tokens"],
        main_usage["cache_creation_tokens"],
    )
    sub_cost = llm_adapter.estimate_cost(
        sub_model,
        sub_agent_usage["prompt_tokens"],
        sub_agent_usage["completion_tokens"],
        sub_agent_usage["cached_tokens"],
        sub_agent_usage["cache_creation_tokens"],
    ) if has_sub else 0.0
    total_cost = round(main_cost + sub_cost, 6)

    task_usage: dict = {
        "main": {
            "model": main_model,
            **main_usage,
            "cost_yuan": main_cost,
        },
        # 汇总字段（兼容老前端读取）
        **cumulative_usage,
        "estimated_cost_yuan": total_cost,
        "model": main_model,
        # 循环诊断信息
        "steps": actual_steps,
        "max_steps": max_steps,
        "forced_summary": forced_summary,
        # 老兼容字段（老 session 读取不会报错）
        "sub_agent_prompt_tokens": sub_agent_usage["prompt_tokens"],
        "sub_agent_completion_tokens": sub_agent_usage["completion_tokens"],
        "sub_agent_cached_tokens": sub_agent_usage["cached_tokens"],
    }
    if has_sub:
        task_usage["sub"] = {
            "model": sub_model,
            **sub_agent_usage,
            "cost_yuan": sub_cost,
        }
    if partial:
        task_usage["partial"] = True
    return task_usage


async def run_agent(
    db: Session,
    question: str,
    chat_ids: list[str] | None = None,
    history: list[dict] | None = None,
    session_id: str | None = None,
    usage_collector: dict | None = None,
) -> AsyncIterator[dict]:
    """Agent 主循环，yield 事件给上层。

    Args:
        session_id: 当前对话 session 的 ID。Artifact 类工具需要它作为上下文；
            缺失时这些工具会返回 ``no_session`` 错误（不会让整个 agent 崩）。
        usage_collector: 可选 dict 引用——本函数会在每次累加 usage 后把当前
            partial task_usage 快照写入 ``usage_collector["snapshot"]``。这样即使
            上层把本 generator cancel 掉，也能从 collector 读到中断前的累计用量
            + 费用估算（主 agent 累计 + 子 agent 累计 + 主/子分模型计价）。
            ``run_registry._run_worker`` 用此机制保证 aborted 状态也能持久化 task_usage。
    """
    # 构造 user 消息；如果指定了 chat_ids，注入上下文
    # 注意：时间戳和 artifact 摘要的注入发生在上游 run_registry._run_worker 中
    #（那边同时写入 DB + 传给这里），目的是让保存的历史和传给 LLM 的一致，
    # 从而前缀缓存能覆盖整段历史。这里不要重复注入。
    user_content = question
    if chat_ids:
        user_content += f"\n\n[用户限定只在这些群聊 chat_id 中检索: {json.dumps(chat_ids, ensure_ascii=False)}]"

    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
    ]

    # 注入多轮对话历史（只保留 user/assistant 的文本，不含工具调用细节）
    if history:
        for h in history:
            role = h.get("role") if isinstance(h, dict) else h.role
            content = h.get("content") if isinstance(h, dict) else h.content
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})

    messages.append({"role": "user", "content": user_content})

    yield {"type": "status", "message": f"Agent 启动（最多 {MAX_STEPS} 步）"}

    # 收集引用的消息 ID，最后用来生成 sources
    cited_message_ids: set[int] = set()
    final_text_parts: list[str] = []

    # 累计 token 用量（含子 Agent）—— 优先用 collector 提供的 dict 引用，
    # 这样 generator 被 cancel 后上层仍能读到中断前的累计值。
    if usage_collector is not None:
        cumulative_usage = usage_collector.setdefault("cumulative", {
            "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
            "cached_tokens": 0, "cache_creation_tokens": 0,
        })
        sub_agent_usage = usage_collector.setdefault("sub_agent", {
            "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
            "cached_tokens": 0, "cache_creation_tokens": 0,
        })
    else:
        cumulative_usage = {
            "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
            "cached_tokens": 0, "cache_creation_tokens": 0,
        }
        sub_agent_usage = {
            "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
            "cached_tokens": 0, "cache_creation_tokens": 0,
        }
    # 是否走到了"达到 MAX_STEPS 强制总结"分支（前端可提示"被截断"）
    forced_summary = False
    # 最终实际用到的步数（未被截断时 = 正常结束步；被截断时 = MAX_STEPS）
    actual_steps = 0

    main_model = settings.llm_model_qa
    sub_model = settings.effective_sub_agent_model

    def _add_usage(target: dict, u: dict):
        for k in target:
            target[k] += u.get(k, 0)

    def _refresh_usage_snapshot() -> None:
        """每次累加 usage 后调用：把当前累计值写入 collector["snapshot"]。

        ``run_registry`` 在 cancel 后会读 ``snapshot`` 持久化到 turn meta，
        让 aborted 状态也能展示"已花费 X 元"。
        """
        if usage_collector is None:
            return
        usage_collector["snapshot"] = _build_task_usage_dict(
            cumulative_usage, sub_agent_usage,
            main_model, sub_model,
            actual_steps or 0, MAX_STEPS, forced_summary,
            partial=True,
        )

    for step in range(1, MAX_STEPS + 1):
        yield {"type": "step_start", "step": step}

        # 上下文预算管理：在调用 LLM 前检查并截断
        model = settings.llm_model_qa
        messages, was_trimmed = _trim_messages_to_budget(messages, model)
        if was_trimmed:
            # 发送估算的 usage 事件，让前端 ContextBadge 及时更新
            est_tokens = _estimate_tokens(messages)
            max_ctx = llm_adapter.get_context_window(model)
            yield {
                "type": "usage",
                "step": step,
                "prompt_tokens": est_tokens,
                "completion_tokens": 0,
                "total_tokens": est_tokens,
                "max_context": max_ctx,
                "percent": round(est_tokens / max_ctx, 4) if max_ctx else 0.0,
                "model": model,
            }
            yield {"type": "status", "message": f"上下文已截断以适应 {model} 窗口（估算 {est_tokens} tokens）"}

        # 本轮要收集的文本 & tool_calls & 思考链
        step_text = ""
        step_tool_calls: list[dict] = []
        step_reasoning = ""  # Kimi reasoning_content

        try:
            async for ev in _stream_llm_step(messages):
                if ev["type"] == "text_delta":
                    step_text += ev["text"]
                    yield {"type": "thinking_delta", "step": step, "text": ev["text"]}
                elif ev["type"] == "reasoning_delta":
                    yield {"type": "reasoning_delta", "step": step, "text": ev["text"]}
                elif ev["type"] == "reasoning_content":
                    step_reasoning = ev["text"]
                elif ev["type"] == "tool_calls":
                    step_tool_calls = ev["calls"]
                elif ev["type"] == "usage":
                    # 累加到全局用量
                    _add_usage(cumulative_usage, ev)
                    # 立即刷新 partial 快照——cancel 落在后续 await 时仍能保留
                    actual_steps = step
                    _refresh_usage_snapshot()
                    # 向上透传 usage（上下文占比显示）
                    yield {**ev, "step": step}
                elif ev["type"] == "done":
                    break
        except Exception as e:
            # 如果有工具结果积累，尝试强制总结而非直接失败
            if step > 1 or any(m.get("role") == "tool" for m in messages):
                yield {"type": "status", "message": f"LLM 调用失败({e})，尝试缩减上下文后强制总结..."}
                recovery_text = await _force_summarize_recovery(messages, cited_message_ids)
                if recovery_text:
                    final_text_parts.append(recovery_text)
                    break
            yield {"type": "error", "error": f"LLM 调用失败: {e}"}
            return

        # 把本轮 assistant 消息加回去
        assistant_msg: dict = {"role": "assistant"}
        if step_text:
            assistant_msg["content"] = step_text
        else:
            assistant_msg["content"] = None
        # Kimi 要求多步工具调用时 assistant message 保留 reasoning_content
        if step_reasoning:
            assistant_msg["reasoning_content"] = step_reasoning
        if step_tool_calls:
            assistant_msg["tool_calls"] = [
                {
                    "id": c["id"],
                    "type": "function",
                    "function": {"name": c["name"], "arguments": c["arguments"]},
                }
                for c in step_tool_calls
            ]
        messages.append(assistant_msg)

        # 没有 tool_calls —— LLM 给出最终答案，循环结束
        if not step_tool_calls:
            final_text_parts.append(step_text)
            actual_steps = step
            yield {"type": "step_done", "step": step, "had_tool_calls": False}
            break

        # 有 tool_calls —— 执行工具并把结果喂回去
        yield {"type": "step_done", "step": step, "had_tool_calls": True,
               "tool_count": len(step_tool_calls)}

        # 解析所有 tool_calls 的参数
        parsed_calls: list[tuple[dict, dict]] = []  # (call, args)
        for call in step_tool_calls:
            name = call["name"]
            args_str = call["arguments"] or "{}"
            try:
                args = json.loads(args_str) if args_str else {}
            except json.JSONDecodeError as e:
                args = {}
                err_result = {"error": f"参数 JSON 解析失败: {e}", "raw": args_str[:200]}
                err_result_str = json.dumps(err_result, ensure_ascii=False)
                yield {
                    "type": "tool_call",
                    "step": step,
                    "id": call["id"],
                    "name": name,
                    "args": {},
                    "args_raw": args_str,
                }
                yield {
                    "type": "tool_result",
                    "step": step,
                    "id": call["id"],
                    "name": name,
                    "output_preview": err_result,
                    # output_full 供 run_registry 持久化到 trajectory（在 _emit 里 pop、不发给订阅者）
                    "output_full": err_result_str,
                    "duration_ms": 0,
                    "error": True,
                }
                messages.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": err_result_str,
                })
                continue
            parsed_calls.append((call, args))

        # 发射所有 tool_call 事件
        for call, args in parsed_calls:
            yield {
                "type": "tool_call",
                "step": step,
                "id": call["id"],
                "name": call["name"],
                "args": args,
            }

        # 收集子 Agent 进度事件，稍后统一 yield（因为 asyncio.gather 中不能 yield）
        sub_events: list[dict] = []

        async def _sub_event_cb(ev: dict):
            sub_events.append(ev)
            # 子 Agent 每次累加 usage 时实时透传增量，让主 Agent 也实时累加；
            # 这样如果用户在子 Agent 跑一半时点 abort，主 Agent 的 cumulative_usage /
            # sub_agent_usage 已经反映了部分用量，写入 collector["snapshot"] 后
            # run_registry 的 finally 块能持久化中断时的费用估算。
            if ev.get("type") == "sub_usage_delta":
                u = ev.get("usage") or {}
                _add_usage(cumulative_usage, u)
                _add_usage(sub_agent_usage, u)
                _refresh_usage_snapshot()

        # Artifact 工具与 sub_agent 都通过 dispatch_tool 入口；
        # session_id 给 artifact handlers 用，event_callback 给 sub_agent 用。
        tool_context = {"session_id": session_id}

        # 并发执行所有 research 调用（semaphore 自动限流），其他工具顺序执行
        async def _exec_one(call: dict, args: dict) -> tuple[dict, dict, int]:
            """执行单个工具，返回 (call, result, duration_ms)"""
            t0 = time.time()
            cb = _sub_event_cb if call["name"] == "research" else None
            result = await dispatch_tool(
                db, call["name"], args,
                event_callback=cb, context=tool_context,
            )
            duration_ms = int((time.time() - t0) * 1000)
            return call, result, duration_ms

        # 判断是否有 research 调用 → 并发执行全部（包括 list_chats 等轻量工具）
        has_research = any(c["name"] == "research" for c, _ in parsed_calls)
        if has_research and len(parsed_calls) > 1:
            # 并发执行所有工具调用，由 semaphore 自然限流
            exec_results = await asyncio.gather(
                *[_exec_one(c, a) for c, a in parsed_calls],
                return_exceptions=True,
            )
        else:
            # 单个或无 research：顺序执行（同样捕获异常，保持与 gather 行为一致）
            exec_results = []
            for c, a in parsed_calls:
                try:
                    exec_results.append(await _exec_one(c, a))
                except Exception as exc:
                    exec_results.append(exc)

        # 先 yield 子 Agent 进度事件
        for ev in sub_events:
            yield {"type": "sub_agent_event", "step": step, **ev}

        # 处理执行结果
        for i, item in enumerate(exec_results):
            if isinstance(item, Exception):
                # asyncio.gather return_exceptions=True
                err_call = parsed_calls[i][0]
                err_result = {"error": f"工具执行异常: {item}"}
                err_result_str = json.dumps(err_result, ensure_ascii=False)
                yield {
                    "type": "tool_result",
                    "step": step,
                    "id": err_call["id"],
                    "name": err_call["name"],
                    "output_preview": err_result,
                    "output_full": err_result_str,
                    "duration_ms": 0,
                    "error": True,
                }
                messages.append({
                    "role": "tool",
                    "tool_call_id": err_call["id"],
                    "content": err_result_str,
                })
                continue

            call, result, duration_ms = item

            # 收集子 Agent usage（research 工具返回中包含 usage 字段）。
            # research 工具的 usage 已经通过 sub_usage_delta 事件实时累加（见上面
            # _sub_event_cb），这里跳过避免双计；其他工具不会返回 usage 字段。
            if call["name"] != "research":
                sub_usage = result.get("usage") if isinstance(result, dict) else None
                if sub_usage and isinstance(sub_usage, dict):
                    _add_usage(cumulative_usage, sub_usage)
                    _add_usage(sub_agent_usage, sub_usage)
                    _refresh_usage_snapshot()

            # 收集消息 ID 用于后续引用
            _collect_ids(result, cited_message_ids)

            # 提取 artifact_event（artifact 类工具会在 result 里塞 _artifact_event）；
            # 用 pop 把它从 result 中剥掉，避免回灌到 LLM 上下文里浪费 token。
            artifact_ev_payload: dict | None = None
            if isinstance(result, dict) and "_artifact_event" in result:
                artifact_ev_payload = result.pop("_artifact_event")

            # 截断过长输出
            result_str = _truncate_tool_output(result)

            yield {
                "type": "tool_result",
                "step": step,
                "id": call["id"],
                "name": call["name"],
                "output_preview": _make_preview(result),
                # 完整结果供下游持久化到 trajectory.tool_calls[i].output（多轮对话重放用）
                # _emit 会在入 run.events 前 pop 走、不推送给前端 SSE 订阅者
                "output_full": result_str,
                "duration_ms": duration_ms,
                "error": "error" in result,
            }

            # 紧随 tool_result 之后发射 artifact_event（前端用于刷新侧边面板）
            if artifact_ev_payload is not None:
                yield {
                    "type": "artifact_event",
                    "step": step,
                    "tool_call_id": call["id"],
                    **artifact_ev_payload,
                }

            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": result_str,
            })

    else:
        # 走到 MAX_STEPS 仍在调用工具——强制让 LLM 基于已有信息总结答案（禁用工具）
        forced_summary = True
        actual_steps = MAX_STEPS
        yield {"type": "status", "message": f"已达最大步数 {MAX_STEPS}，强制总结..."}
        messages.append({
            "role": "user",
            "content": "你已经达到最大工具调用次数。请基于上面已经收集到的所有工具结果，给出最终答案。"
                       "**不要再调用任何工具**。如果信息不足，明确说明'根据已检索信息无法完整回答'，"
                       "并总结已找到的相关内容。",
        })
        # 截断上下文
        _model = settings.llm_model_qa
        messages, _ = _trim_messages_to_budget(messages, _model)
        try:
            _client = llm_adapter.get_client_for_model(_model)
            _effective_messages = messages
            if (
                settings.enable_qwen_explicit_cache
                and llm_adapter.is_qwen_model(_model)
                and not llm_adapter.is_kimi_model(_model)
            ):
                _effective_messages = llm_adapter.inject_cache_control(messages)
            _force_kwargs = dict(
                model=_model,
                messages=_effective_messages,
                stream=True,
            )
            if llm_adapter.is_kimi_model(_model):
                _force_kwargs.update(llm_adapter.kimi_chat_kwargs(_model, False))
            else:
                _force_kwargs["temperature"] = 0.3
            _sem = llm_adapter.get_chat_semaphore(_model)
            forced_text = ""
            _force_kwargs["stream_options"] = {"include_usage": True}
            async with _sem:
                stream = await _client.chat.completions.create(**_force_kwargs)
                async for chunk in stream:
                    _fu = getattr(chunk, "usage", None)
                    if _fu is not None:
                        _add_usage(cumulative_usage, llm_adapter.parse_usage(_fu))
                        _refresh_usage_snapshot()
                    choice = chunk.choices[0] if chunk.choices else None
                    if choice and choice.delta.content:
                        forced_text += choice.delta.content
                        yield {"type": "thinking_delta", "step": MAX_STEPS + 1, "text": choice.delta.content}
            final_text_parts.append(forced_text)
        except Exception as e:
            yield {"type": "error", "error": f"强制总结失败: {e}"}

    # 构造 sources（从引用过的消息中选前 5 条，按话题去重）
    sources = _build_sources(db, cited_message_ids)

    # 构造 final task_usage（与 _refresh_usage_snapshot 走同一份逻辑，partial=False）
    task_usage = _build_task_usage_dict(
        cumulative_usage, sub_agent_usage,
        main_model, sub_model,
        actual_steps or MAX_STEPS, MAX_STEPS, forced_summary,
        partial=False,
    )
    # 同步 collector 的最新快照为最终值（覆盖 partial=True 的中间快照）
    if usage_collector is not None:
        usage_collector["snapshot"] = task_usage

    yield {
        "type": "final_answer",
        "answer": "".join(final_text_parts),
        "sources": sources,
        "task_usage": task_usage,
    }


async def _force_summarize_recovery(messages: list[dict], cited_ids: set[int]) -> str:
    """LLM 调用失败后的恢复策略：激进截断工具输出后强制总结。"""
    model = settings.llm_model_qa

    # 只保留 system + user + 大幅截断的工具结果
    recovery_msgs: list[dict] = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            recovery_msgs.append(m)
        elif role == "user":
            recovery_msgs.append(m)
        elif role == "tool":
            content = m.get("content", "")
            # 每个工具结果最多保留 3000 字符
            if len(content) > 3000:
                content = content[:3000] + "\n...[截断]"
            recovery_msgs.append({**m, "content": content})
        elif role == "assistant":
            # 保留 assistant 消息但去掉 reasoning_content
            slim = {"role": "assistant", "content": m.get("content")}
            if m.get("tool_calls"):
                slim["tool_calls"] = m["tool_calls"]
            recovery_msgs.append(slim)

    recovery_msgs.append({
        "role": "user",
        "content": "之前的 LLM 调用因上下文过长失败了。请基于上面已收集到的工具结果（可能被截断），"
                   "给出最终答案。**不要再调用任何工具**。",
    })

    # 再次做预算检查
    recovery_msgs, _ = _trim_messages_to_budget(recovery_msgs, model)

    try:
        client = llm_adapter.get_client_for_model(model)
        sem = llm_adapter.get_chat_semaphore(model)
        if (
            settings.enable_qwen_explicit_cache
            and llm_adapter.is_qwen_model(model)
            and not llm_adapter.is_kimi_model(model)
        ):
            recovery_msgs = llm_adapter.inject_cache_control(recovery_msgs)
        kwargs = dict(
            model=model,
            messages=recovery_msgs,
            stream=False,
        )
        if llm_adapter.is_kimi_model(model):
            kwargs.update(llm_adapter.kimi_chat_kwargs(model, False))
        else:
            kwargs["temperature"] = 0.3
        async with sem:
            resp = await client.chat.completions.create(**kwargs)
        return resp.choices[0].message.content or ""
    except Exception:
        return ""


def _collect_ids(result: dict, acc: set[int]) -> None:
    """从工具结果中提取所有 message_id，用来构建最终引用"""
    if not isinstance(result, dict):
        return
    # sub_agent 返回的 message_ids 列表（research 工具）
    mids_top = result.get("message_ids")
    if isinstance(mids_top, list):
        for x in mids_top:
            if isinstance(x, int):
                acc.add(x)
    # 直接的 messages 列表
    for key in ("messages", "results"):
        items = result.get(key)
        if isinstance(items, list):
            for it in items:
                if isinstance(it, dict):
                    mid = it.get("message_id")
                    if isinstance(mid, int):
                        acc.add(mid)
                    # semantic_search 返回的 message_ids 列表
                    mids = it.get("message_ids")
                    if isinstance(mids, list):
                        for x in mids:
                            if isinstance(x, int):
                                acc.add(x)


def _make_preview(result: dict) -> dict:
    """为前端生成紧凑的工具结果预览"""
    if not isinstance(result, dict):
        return {"value": str(result)[:200]}

    if "error" in result:
        return {"error": result["error"]}

    # research 工具（子 Agent）返回
    if "report" in result:
        report_text = result["report"] or ""
        return {
            "summary": f"子 Agent 报告 ({result.get('steps', '?')} 步, "
                       f"{result.get('tool_calls_count', '?')} 次工具调用, "
                       f"{len(result.get('message_ids', []))} 条引用)",
            "report_preview": report_text[:300],
        }

    # Artifact 工具返回（create / update / rewrite）
    if "artifact_key" in result:
        ak = result["artifact_key"]
        ver = result.get("version")
        title = result.get("title", "")
        return {
            "summary": f"📄 《{title}》 v{ver}（{ak}）",
            "artifact_key": ak,
            "version": ver,
            "title": title,
        }

    # 根据不同工具返回，生成简洁摘要
    preview: dict = {}
    if "count" in result:
        preview["count"] = result["count"]
    if "chats" in result and isinstance(result["chats"], list):
        preview["count"] = len(result["chats"])
        preview["summary"] = f"{len(result['chats'])} 个群聊"
    if "results" in result and isinstance(result["results"], list):
        items = result["results"]
        preview["items"] = [
            {
                "text": (it.get("chunk_preview") or it.get("text") or "")[:100],
                "sender": it.get("sender"),
                "date": it.get("date") or it.get("start_date"),
                "distance": it.get("distance"),
            }
            for it in items[:3]
        ]
    if "messages" in result and isinstance(result["messages"], list):
        msgs = result["messages"]
        preview["items"] = [
            {
                "text": (m.get("text") or "")[:100],
                "sender": m.get("sender"),
                "date": m.get("date"),
            }
            for m in msgs[:3]
        ]
    return preview


def _build_sources(db: Session, msg_ids: set[int]) -> list[dict]:
    """从引用过的 message_ids 构建最终来源列表（按话题去重，最多 5 条）"""
    if not msg_ids:
        return []

    msgs = (
        db.query(Message)
        .filter(Message.id.in_(list(msg_ids)[:100]))
        .order_by(Message.date)
        .all()
    )

    sources: list[dict] = []
    seen_topics: set[int | None] = set()
    for m in msgs:
        if m.topic_id in seen_topics and m.topic_id is not None:
            continue
        seen_topics.add(m.topic_id)
        sources.append({
            "message_ids": [m.id],
            "sender": m.sender,
            "date": m.date.strftime("%Y-%m-%d") if m.date else None,
            "preview": (m.text_plain or "")[:200],
            "topic_id": m.topic_id,
        })
        if len(sources) >= 5:
            break
    return sources
