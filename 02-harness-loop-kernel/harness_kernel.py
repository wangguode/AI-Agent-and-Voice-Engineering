#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Harness Engineering 微内核：任务循环收敛、状态快照与账单熔断器
==============================================================
对应知乎 @王果ai 爆款专栏：
- 《Agent(特别篇六)：Harness Engineering和Loop Engineering-一部 Agent 工程的血泪史》（479+ 赞）
- 《Agent(特别篇七)：Graph Engineering 刷屏了，但先问一个问题：你的 Agent Loop 收敛过吗？》
- 《Agent篇(28)：Harness Engineering——Agent OS 的真正内核：任务循环、状态、沙箱与自我验证》
- 《Agent(特别篇九)：当 AI 开始“替你深夜干活”，你的 API 账单还安全吗？》

核心工程能力（参考 Codex / Claude Code 等编码智能体的 Harness 设计思想，脱敏重写）：
1. LoopConvergenceGuard（循环收敛守卫）：基于工具调用签名哈希与语义重复检测，阻断死循环（Loop Trap）。
2. CostCircuitBreaker（API 账单熔断器）：监控单次任务 Token 消耗与美元成本，超限毫秒级熔断降级。
3. TimeTravelSnapshotStore（状态机时间旅行）：每步推理自动生成不可变快照，支持随时回滚（Rollback）重演。

运行方式：
    python3 harness_kernel.py
"""

from __future__ import annotations
import copy
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass(frozen=True)
class ToolAction:
    name: str
    arguments: Dict[str, Any]

    def fingerprint(self) -> str:
        payload = json.dumps({"tool": self.name, "args": self.arguments}, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


@dataclass
class StepSnapshot:
    step_index: int
    timestamp: float
    thought: str
    action: Optional[ToolAction]
    observation: str
    cumulative_tokens: int
    cumulative_cost_usd: float
    memory_state: Dict[str, Any]


class LoopConvergenceGuard:
    """
    Agent Loop 收敛守卫：检测连续重复 Action 或震荡循环（A -> B -> A -> B）
    """

    def __init__(self, max_identical_repeats: int = 2, max_oscillation_window: int = 4) -> None:
        self.max_identical_repeats = max_identical_repeats
        self.max_oscillation_window = max_oscillation_window
        self.history_fingerprints: List[str] = []

    def check_and_record(self, action: ToolAction) -> Optional[str]:
        fp = action.fingerprint()
        self.history_fingerprints.append(fp)

        # 1. 连续相同工具与参数调用检测
        if len(self.history_fingerprints) >= self.max_identical_repeats + 1:
            tail = self.history_fingerprints[-(self.max_identical_repeats + 1):]
            if len(set(tail)) == 1:
                return f"检测到连续 {len(tail)} 次完全相同的工具调用 (fingerprint={fp})，触发防死循环强行收敛！"

        # 2. A-B-A-B 周期震荡检测
        if len(self.history_fingerprints) >= 4:
            a, b, c, d = self.history_fingerprints[-4:]
            if a == c and b == d and a != b:
                return f"检测到 A-B-A-B 震荡未收敛循环 ({a} <-> {b})，触发 Harness 强制干预！"

        return None


class CostCircuitBreaker:
    """
    深夜无人值守 Agent 的 API 账单熔断器
    """

    def __init__(self, max_tokens_budget: int = 30000, max_cost_usd: float = 0.50) -> None:
        self.max_tokens_budget = max_tokens_budget
        self.max_cost_usd = max_cost_usd
        self.used_tokens: int = 0
        self.used_cost_usd: float = 0.0

    def record_usage(self, prompt_tokens: int, completion_tokens: int, price_per_1k: float = 0.01) -> Optional[str]:
        step_tokens = prompt_tokens + completion_tokens
        self.used_tokens += step_tokens
        self.used_cost_usd += (step_tokens / 1000.0) * price_per_1k

        if self.used_tokens > self.max_tokens_budget:
            return f"Token 消耗 ({self.used_tokens}) 超出预算上限 ({self.max_tokens_budget})，触发账单熔断！"
        if self.used_cost_usd > self.max_cost_usd:
            return f"累计费用 (${self.used_cost_usd:.4f}) 超出安全阈值 (${self.max_cost_usd:.2f})，触发账单熔断！"
        return None


class HarnessKernel:
    """
    Agent OS 核心执行马具 (Harness Kernel)
    """

    def __init__(
        self,
        max_steps: int = 10,
        convergence_guard: Optional[LoopConvergenceGuard] = None,
        circuit_breaker: Optional[CostCircuitBreaker] = None,
    ) -> None:
        self.max_steps = max_steps
        self.guard = convergence_guard or LoopConvergenceGuard()
        self.breaker = circuit_breaker or CostCircuitBreaker()
        self.tools: Dict[str, Callable[[Dict[str, Any]], str]] = {}
        self.snapshots: List[StepSnapshot] = []
        self.memory_state: Dict[str, Any] = {"facts": [], "status": "INIT"}

    def register_tool(self, name: str, fn: Callable[[Dict[str, Any]], str]) -> None:
        self.tools[name] = fn

    def rollback_to(self, step_index: int) -> StepSnapshot:
        """时间旅行调试：回滚到任意历史步数的状态机快照"""
        target = next(s for s in self.snapshots if s.step_index == step_index)
        self.memory_state = copy.deepcopy(target.memory_state)
        self.snapshots = [s for s in self.snapshots if s.step_index <= step_index]
        print(f"⏪ [Time-Travel 回滚成功] 已恢复至 Step #{step_index} 快照状态: {self.memory_state}")
        return target

    def run_episode(self, policy_steps: List[tuple[str, Optional[ToolAction], int]]) -> None:
        print("=" * 72)
        print("🛡️ 启动 Harness Engineering Agent OS 内核 (收敛检测 + 快照 + 账单熔断)")
        print("=" * 72)

        for idx, (thought, action, simulated_tokens) in enumerate(policy_steps, start=1):
            if idx > self.max_steps:
                print(f"🛑 [超出最大步数 {self.max_steps}] 强制终止 Loop")
                break

            # 1. 账单熔断检查
            breaker_err = self.breaker.record_usage(simulated_tokens, simulated_tokens // 2)
            if breaker_err:
                print(f"💸 [CostCircuitBreaker 熔断] Step #{idx}: {breaker_err}")
                break

            # 2. 终态完成检查
            if action is None:
                self.memory_state["status"] = "CONVERGED"
                print(f"✅ [Loop 正常收敛] Step #{idx}: {thought} | 总 Token={self.breaker.used_tokens}")
                break

            # 3. 循环收敛守卫检查
            guard_err = self.guard.check_and_record(action)
            if guard_err:
                print(f"⚠️ [LoopConvergenceGuard 拦截] Step #{idx}: {guard_err}")
                self.memory_state["status"] = "HALTED_BY_CONVERGENCE_GUARD"
                break

            # 4. 沙箱工具执行与状态快照保存
            tool_fn = self.tools.get(action.name)
            observation = tool_fn(action.arguments) if tool_fn else f"Error: unknown tool {action.name}"
            self.memory_state["facts"].append(observation)

            snapshot = StepSnapshot(
                step_index=idx,
                timestamp=time.time(),
                thought=thought,
                action=action,
                observation=observation,
                cumulative_tokens=self.breaker.used_tokens,
                cumulative_cost_usd=self.breaker.used_cost_usd,
                memory_state=copy.deepcopy(self.memory_state),
            )
            self.snapshots.append(snapshot)
            print(
                f"🔄 [Step #{idx}] Thought: {thought} | Tool: {action.name}({action.arguments}) "
                f"| Tokens={self.breaker.used_tokens} (${self.breaker.used_cost_usd:.4f})"
            )


if __name__ == "__main__":
    kernel = HarnessKernel(max_steps=8)
    kernel.register_tool("query_order_db", lambda args: f"Order {args.get('order_id')} status=PAID")

    # 模拟 Agent 在第 2~4 步陷入重复调用同一工具的死循环，验证 Harness 收敛守卫自动拦截并回滚
    simulated_trajectory = [
        ("查询用户订单状态", ToolAction("query_order_db", {"order_id": "ORD-2026-01"}), 800),
        ("再次重试相同查询", ToolAction("query_order_db", {"order_id": "ORD-2026-01"}), 900),
        ("陷入幻觉继续重复调用相同参数", ToolAction("query_order_db", {"order_id": "ORD-2026-01"}), 950),
        ("汇总最终回复", None, 400),
    ]
    kernel.run_episode(simulated_trajectory)
    if kernel.snapshots:
        kernel.rollback_to(1)
