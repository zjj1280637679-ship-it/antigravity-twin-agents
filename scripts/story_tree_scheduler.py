#!/usr/bin/env python3
"""
Story Tree Scheduler (故事树池化调度器)
=========================================
基于 Claude Code 架构启发的多智能体并发池调度程序：
- 严格限制滑动窗口最大并发智能体数（默认 <= 6）
- 祖先亲和度分组调度（Ancestor-Grouped Batching），最大化命中 >= 4096 Tokens 的 Gemini Prefix Cache
- 全局树深度控制与终结判定（Max Depth Clamping）
- 故事树拓扑组装，支持导出为 Mermaid 与结构化 JSON/Markdown
"""

import sys
import json
import argparse
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional, Any

@dataclass
class StoryNode:
    node_id: str
    parent_id: Optional[str]
    depth: int
    choice_text: str
    ancestor_chain: str
    status: str = "pending"  # pending, running, completed, ended
    is_ending: bool = False
    ending_type: Optional[str] = None  # good_end, bad_end, normal_end, none
    content: Optional[str] = None
    next_branches: List[Dict[str, str]] = field(default_factory=list)

class StoryTreeScheduler:
    def __init__(self, max_depth: int = 3, max_concurrency: int = 6, cache_threshold: int = 4096):
        self.max_depth = max_depth
        self.max_concurrency = max_concurrency
        self.cache_threshold = cache_threshold
        self.pending_queue: List[StoryNode] = []
        self.running_pool: Dict[str, StoryNode] = {}
        self.completed_nodes: Dict[str, StoryNode] = {}

    def initialize_root(self, root_title: str, root_content: str, initial_choices: List[Dict[str, str]]):
        """初始化故事树根节点"""
        root = StoryNode(
            node_id="root",
            parent_id=None,
            depth=0,
            choice_text="起点",
            ancestor_chain=f"【第0章：{root_title}】\n{root_content}",
            status="completed",
            content=root_content,
            is_ending=False,
            next_branches=initial_choices
        )
        self.completed_nodes["root"] = root
        for i, choice in enumerate(initial_choices):
            cid = choice.get("choice_id", f"C{i+1}")
            node_id = f"root_{cid}"
            child = StoryNode(
                node_id=node_id,
                parent_id="root",
                depth=1,
                choice_text=choice.get("summary", ""),
                ancestor_chain=root.ancestor_chain,
                status="pending"
            )
            self.pending_queue.append(child)

    def sort_queue_by_ancestor_affinity(self):
        """
        祖先亲和度排序算法 (Ancestor Affinity Sorting)
        ----------------------------------------------
        将共享相同父节点/祖先链的任务紧密排在一起，确保同一个滑动批次（<=6个分身）
        具有最大长度的相同公共前缀 Token，从而精准跨越 >= 4096 Token 的 Prefix Cache 门槛。
        """
        self.pending_queue.sort(key=lambda node: (node.parent_id or "", node.depth))

    def get_next_batch(self) -> List[StoryNode]:
        """获取下一个调度批次（滑动窗口补齐至 max_concurrency）"""
        self.sort_queue_by_ancestor_affinity()
        slots_available = self.max_concurrency - len(self.running_pool)
        if slots_available <= 0:
            return []
        
        batch = self.pending_queue[:slots_available]
        self.pending_queue = self.pending_queue[slots_available:]
        for node in batch:
            node.status = "running"
            self.running_pool[node.node_id] = node
        return batch

    def build_prompt_for_node(self, node: StoryNode) -> str:
        """为分身子代理构造注入祖先链路的显式 Prompt"""
        is_terminal_depth = (node.depth >= self.max_depth)
        
        instruction = (
            "【终局要求】：当前已达到最大设定深度，本步必须根据前因后果编写收束结局（Good End 或 Bad End），"
            "将 is_ending 设为 true 并标明 ending_type，严禁再产生 next_branches。"
            if is_terminal_depth else
            "【推演要求】：沉浸推演本步（300~400字），自主评估是否达成结局。若未达成，请给出 2~3 个后续候选分支。"
        )

        prompt = (
            f"【前序因果链（所有同祖先分身共享此公共前缀）】\n"
            f"{node.ancestor_chain}\n\n"
            f"【本支线专属抉择（深度 Layer {node.depth}）】\n"
            f"分支路径：{node.node_id}\n"
            f"玩家选择了：【{node.choice_text}】\n\n"
            f"【任务指令】\n"
            f"1. 严格基于前序因果，撰写当前分支的后续剧情。\n"
            f"2. {instruction}\n"
            f"3. 按照 branch_step 格式通过 send_message 结构化汇报。"
        )
        return prompt

    def submit_node_result(self, node_id: str, content: str, is_ending: bool, ending_type: Optional[str], next_branches: List[Dict[str, str]]):
        """接收分身汇报并推进故事树拓扑"""
        if node_id not in self.running_pool:
            raise ValueError(f"节点 {node_id} 不在运行池中")
        
        node = self.running_pool.pop(node_id)
        node.content = content
        node.is_ending = is_ending
        node.ending_type = ending_type
        node.status = "ended" if is_ending else "completed"
        
        # 更新该分支的累积因果链
        updated_chain = f"{node.ancestor_chain}\n\n【第{node.depth}步走向：{node.choice_text}】\n{content}"
        node.ancestor_chain = updated_chain
        self.completed_nodes[node_id] = node

        # 若未到终局且未达到最大深度，将新分支压入待执行队列
        if not is_ending and node.depth < self.max_depth:
            for i, branch in enumerate(next_branches):
                bid = branch.get("choice_id", f"B{i+1}")
                child_id = f"{node_id}_{bid}"
                child = StoryNode(
                    node_id=child_id,
                    parent_id=node_id,
                    depth=node.depth + 1,
                    choice_text=branch.get("summary", ""),
                    ancestor_chain=updated_chain,
                    status="pending"
                )
                self.pending_queue.append(child)

    def is_finished(self) -> bool:
        """所有队列与运行池清空即完成"""
        return len(self.pending_queue) == 0 and len(self.running_pool) == 0

    def export_mermaid(self) -> str:
        """导出故事树的 Mermaid 流程图"""
        lines = ["graph TD"]
        for nid, node in self.completed_nodes.items():
            label = f"{node.node_id}[\"{node.choice_text}\"]"
            if node.is_ending:
                label = f"{node.node_id}[\"{node.choice_text} <br/>【{node.ending_type}】\"]"
            lines.append(f"    {label}")
            if node.parent_id:
                lines.append(f"    {node.parent_id} --> {node.node_id}")
        return "\n".join(lines)

    def export_json(self) -> str:
        """导出结构化故事树 JSON"""
        return json.dumps({nid: asdict(node) for nid, node in self.completed_nodes.items()}, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Antigravity Twin Agents 故事树并发池调度器")
    parser.add_argument("--depth", type=int, default=2, help="故事树最大深度")
    parser.add_argument("--concurrency", type=int, default=6, help="并发池最大槽位")
    parser.add_argument("--demo", action="store_true", help="运行内置确定性模拟测试")
    args = parser.parse_args()

    print(f"=== Antigravity Story Tree Scheduler ===")
    print(f"最大深度: {args.depth} | 并发上限: {args.concurrency}")

    scheduler = StoryTreeScheduler(max_depth=args.depth, max_concurrency=args.concurrency)
    scheduler.initialize_root(
        root_title="雨夜古董钟表店",
        root_content="大雨倾盆的午夜，停摆六十七年的古钟突然鸣响，钟摆血字警告：第十三次敲响时，镜中的你将不再是你。",
        initial_choices=[
            {"choice_id": "A", "summary": "用铁棍强行卡死摆锤"},
            {"choice_id": "B", "summary": "打碎房间所有镜子"},
            {"choice_id": "C", "summary": "逆转怀表试图重置时间"}
        ]
    )

    step = 1
    while not scheduler.is_finished():
        batch = scheduler.get_next_batch()
        print(f"\n[调度批次 {step}] 激活 {len(batch)} 个分身（运行池水位: {len(scheduler.running_pool)}/{scheduler.max_concurrency}）:")
        for node in batch:
            print(f"  -> 分派分身处理: {node.node_id} (深度: {node.depth}, 选项: {node.choice_text})")
            # 模拟执行与自决回传
            is_end = (node.depth >= scheduler.max_depth) or (node.node_id.endswith("_C"))
            end_type = "BAD_END" if is_end else None
            next_b = [] if is_end else [
                {"choice_id": "1", "summary": f"{node.choice_text}-深入选项1"},
                {"choice_id": "2", "summary": f"{node.choice_text}-规避选项2"}
            ]
            scheduler.submit_node_result(
                node_id=node.node_id,
                content=f"在选择了【{node.choice_text}】后，因果涟漪扩散，神秘力量显现...",
                is_ending=is_end,
                ending_type=end_type,
                next_branches=next_b
            )
        step += 1

    print("\n=== 故事树推演完成！===")
    print(f"总计完成节点数: {len(scheduler.completed_nodes)}")
    print("\n[Mermaid 故事拓扑图]:\n")
    print(scheduler.export_mermaid())
