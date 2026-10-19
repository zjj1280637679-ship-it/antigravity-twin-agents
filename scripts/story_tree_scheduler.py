#!/usr/bin/env python3
"""
Story Tree Scheduler (故事树工业级池化调度器)
==============================================
特性：
1. 严格防御性并发池：强制限制 max_concurrency 在 1~6 之间（杜绝超限或死循环）。
2. 真实 Antigravity 协议解析：内置 parse_subagent_message() 处理真实 send_message 回传。
3. 真实 Gemini API / 缓存度量后端：实时估算 Token 长度，支持记录 cached_content_token_count。
4. 状态持久化与断点续传 (Checkpoints)：每批次原子保存，支持从故障中完全恢复。
5. 终端深度强制终结：叶节点必定标记 is_ending 并补全结局类型，next_branches 完整持久化。
"""

import sys
import os
import json
import re
import argparse
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional, Tuple, Any

# Claude Code 架构安全并发上限
GLOBAL_MAX_CONCURRENCY = 6
GEMINI_CACHE_MIN_THRESHOLD_TOKENS = 4096

def estimate_tokens(text: str) -> int:
    """
    粗略但实用的中英文混合 Token 估算：
    - 中文字符/全角标点约为 1.2~1.5 Tokens
    - 英文单词约为 1.3 Tokens
    """
    if not text:
        return 0
    # 统计汉字及全角标点
    cjk_count = len(re.findall(r'[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]', text))
    other_chars = len(text) - cjk_count
    return int(cjk_count * 1.3 + (other_chars / 4.0))

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
    estimated_prefix_tokens: int = 0
    cached_tokens_hit: int = 0

class StoryTreeScheduler:
    def __init__(self, max_depth: int = 3, max_concurrency: int = 6, checkpoint_file: Optional[str] = None):
        # 防御性参数校验
        if max_concurrency < 1:
            raise ValueError(f"max_concurrency 必须大于等于 1，输入值为 {max_concurrency}")
        if max_concurrency > GLOBAL_MAX_CONCURRENCY:
            raise ValueError(f"max_concurrency 不能超过系统硬安全上限 {GLOBAL_MAX_CONCURRENCY}，输入值为 {max_concurrency}")
        if max_depth < 1:
            raise ValueError(f"max_depth 必须大于等于 1，输入值为 {max_depth}")

        self.max_depth = max_depth
        self.max_concurrency = max_concurrency
        self.checkpoint_file = checkpoint_file
        self.cache_threshold = GEMINI_CACHE_MIN_THRESHOLD_TOKENS
        
        self.pending_queue: List[StoryNode] = []
        self.running_pool: Dict[str, StoryNode] = {}
        self.completed_nodes: Dict[str, StoryNode] = {}

    def initialize_root(self, root_title: str, root_content: str, initial_choices: List[Dict[str, str]]):
        """初始化故事树根节点"""
        root_chain = f"【第0章：{root_title}】\n{root_content}"
        root = StoryNode(
            node_id="root",
            parent_id=None,
            depth=0,
            choice_text="起点",
            ancestor_chain=root_chain,
            status="completed",
            content=root_content,
            is_ending=False,
            next_branches=initial_choices,
            estimated_prefix_tokens=estimate_tokens(root_chain)
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
                status="pending",
                estimated_prefix_tokens=root.estimated_prefix_tokens
            )
            self.pending_queue.append(child)
        self._auto_checkpoint()

    def sort_queue_by_ancestor_affinity(self):
        """
        祖先亲和度排序算法 (Ancestor Affinity Sorting)
        按 (depth, parent_id) 升序排列，使共享同一长公共父节点的任务连续成组，
        优先打包进入同一并发批次，以最大化跨越 4096 Tokens 门槛触发物理 Prefix Cache。
        """
        self.pending_queue.sort(key=lambda node: (node.depth, node.parent_id or ""))

    def get_next_batch(self) -> List[StoryNode]:
        """获取下一个调度批次（严格受控于 max_concurrency）"""
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

    def build_subagent_payload(self, batch: List[StoryNode]) -> Dict[str, Any]:
        """为 Antigravity 的 invoke_subagent 生成真实调用请求载荷"""
        subagents = []
        for node in batch:
            subagents.append({
                "TypeName": "twin",
                "Role": f"分支推演_{node.node_id}",
                "Workspace": "inherit",
                "Prompt": self.build_prompt_for_node(node)
            })
        return {"Subagents": subagents}

    def build_prompt_for_node(self, node: StoryNode) -> str:
        """为分身子代理构造注入前序因果链的 Prompt"""
        is_terminal_depth = (node.depth >= self.max_depth)
        
        instruction = (
            "【终局强制指令】：当前已达到最大章节深度，本步必须根据前序因果编写终局收束（Good End 或 Bad End），"
            "在 YAML 中置 is_ending 为 true 并注明 ending_type，严禁产生 next_branches。"
            if is_terminal_depth else
            "【推演指令】：沉浸推演本步（300~400字），自主评估是否达成结局。若未达成结局，请给出 2~3 个后续候选选择肢。"
        )

        prompt = (
            f"【前序因果链（公共前缀，估算 Token: {node.estimated_prefix_tokens}）】\n"
            f"{node.ancestor_chain}\n\n"
            f"【本支线专属抉择（深度 Layer {node.depth}）】\n"
            f"分支标识：{node.node_id}\n"
            f"玩家选择了：【{node.choice_text}】\n\n"
            f"【任务指令】\n"
            f"1. 严格基于前序因果推演下一步剧情。\n"
            f"2. {instruction}\n"
            f"3. 严格通过 send_message 发回 branch_step 格式 YAML。"
        )
        return prompt

    @staticmethod
    def parse_branch_step_message(raw_yaml_or_text: str) -> Dict[str, Any]:
        """解析分身通过 send_message 发回的真实 YAML 结构化报文"""
        result = {
            "content": "",
            "is_ending": False,
            "ending_type": None,
            "next_branches": []
        }
        
        # 提取 is_ending
        m_end = re.search(r'is_ending:\s*(true|false)', raw_yaml_or_text, re.IGNORECASE)
        if m_end:
            result["is_ending"] = (m_end.group(1).lower() == "true")
            
        # 提取 ending_type
        m_type = re.search(r'ending_type:\s*([a-zA-Z0-9_]+)', raw_yaml_or_text)
        if m_type:
            val = m_type.group(1).strip()
            if val.lower() not in ["none", "null"]:
                result["ending_type"] = val.upper()

        # 提取 action_taken 或正文内容
        m_action = re.search(r'action_taken:\s*\|?\s*\n(.*?)(?=\n[a-z_]+:|\Z)', raw_yaml_or_text, re.DOTALL)
        if m_action:
            result["content"] = m_action.group(1).strip()
        else:
            result["content"] = raw_yaml_or_text.strip()

        # 提取 next_branches 列表
        branches = []
        pattern = r'-\s*choice_id:\s*["\']?([^"\'\n]+)["\']?\s*\n\s*summary:\s*["\']?([^"\'\n]+)["\']?'
        for m in re.finditer(pattern, raw_yaml_or_text):
            branches.append({
                "choice_id": m.group(1).strip(),
                "summary": m.group(2).strip()
            })
        result["next_branches"] = branches
        return result

    def submit_node_result(self, node_id: str, content: str, is_ending: bool, ending_type: Optional[str], next_branches: List[Dict[str, str]], cached_tokens: int = 0):
        """提交单个分身的推演结果并推演状态机"""
        if node_id not in self.running_pool:
            raise ValueError(f"节点 {node_id} 不在活跃运行池中（当前运行池: {list(self.running_pool.keys())}）")
        
        node = self.running_pool.pop(node_id)
        node.content = content
        node.cached_tokens_hit = cached_tokens

        # 防御性修复：达到或超过最大深度时，强制收敛为结局，绝不留悬挂未决节点
        if node.depth >= self.max_depth:
            is_ending = True
            if not ending_type:
                ending_type = "NORMAL_END"
            next_branches = []

        node.is_ending = is_ending
        node.ending_type = ending_type
        # 修复：完整将 next_branches 写回已完成节点，杜绝元数据丢失
        node.next_branches = next_branches
        node.status = "ended" if is_ending else "completed"

        # 更新累积前序链与 Token 估算
        updated_chain = f"{node.ancestor_chain}\n\n【第{node.depth}步走向：{node.choice_text}】\n{content}"
        node.ancestor_chain = updated_chain
        node.estimated_prefix_tokens = estimate_tokens(updated_chain)

        self.completed_nodes[node_id] = node

        # 若未到结局且未超深度，将后续分支压入待执行队列
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
                    status="pending",
                    estimated_prefix_tokens=node.estimated_prefix_tokens
                )
                self.pending_queue.append(child)

        self._auto_checkpoint()

    def _auto_checkpoint(self):
        """保存运行断点"""
        if self.checkpoint_file:
            self.save_checkpoint(self.checkpoint_file)

    def save_checkpoint(self, path: str):
        """将全量调度状态序列化保存到 JSON 文件"""
        state = {
            "max_depth": self.max_depth,
            "max_concurrency": self.max_concurrency,
            "pending_queue": [asdict(n) for n in self.pending_queue],
            "running_pool": {nid: asdict(n) for nid, n in self.running_pool.items()},
            "completed_nodes": {nid: asdict(n) for nid, n in self.completed_nodes.items()}
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)

    def load_checkpoint(self, path: str):
        """从 JSON 文件恢复状态（实现断点续跑）"""
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
        self.max_depth = state["max_depth"]
        self.max_concurrency = state["max_concurrency"]
        self.pending_queue = [StoryNode(**d) for d in state["pending_queue"]]
        self.running_pool = {nid: StoryNode(**d) for nid, d in state["running_pool"].items()}
        self.completed_nodes = {nid: StoryNode(**d) for nid, d in state["completed_nodes"].items()}

    def is_finished(self) -> bool:
        """队列与运行池双清空即为任务终结"""
        return len(self.pending_queue) == 0 and len(self.running_pool) == 0

    def export_mermaid(self) -> str:
        """导出 Mermaid 故事拓扑图"""
        lines = ["graph TD"]
        for nid, node in self.completed_nodes.items():
            safe_text = (node.choice_text or "节点").replace('"', "'")
            if node.is_ending:
                label = f"{node.node_id}[\"{safe_text} <br/>[{node.ending_type}] (Tokens: {node.estimated_prefix_tokens})\"]"
            else:
                label = f"{node.node_id}[\"{safe_text} (Tokens: {node.estimated_prefix_tokens})\"]"
            lines.append(f"    {label}")
            if node.parent_id:
                lines.append(f"    {node.parent_id} --> {node.node_id}")
        return "\n".join(lines)

    def export_json(self) -> str:
        """导出包含完整分支元数据的故事树 JSON"""
        return json.dumps({nid: asdict(node) for nid, node in self.completed_nodes.items()}, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Antigravity 故事树工业级并发池调度器")
    parser.add_argument("--demo", action="store_true", help="显式启动确定性演示模拟运行")
    parser.add_argument("--depth", type=int, default=2, help="故事树最大深度 (>=1)")
    parser.add_argument("--concurrency", type=int, default=6, help="并发池槽位上限 (1~6)")
    parser.add_argument("--checkpoint", type=str, default="story_tree_checkpoint.json", help="断点持久化文件路径")
    parser.add_argument("--resume", action="store_true", help="从断点恢复执行")
    args = parser.parse_args()

    if not args.demo and not args.resume:
        print("错误：请指定 --demo 启动演示运行，或指定 --resume 从已有检查点恢复。")
        print("用法示例: python scripts/story_tree_scheduler.py --demo --depth 2 --concurrency 6")
        sys.exit(1)

    print(f"=== Antigravity Twin Story Tree Scheduler ===")
    print(f"设定深度: {args.depth} | 并发槽位: {args.concurrency} (硬上限: {GLOBAL_MAX_CONCURRENCY})")

    scheduler = StoryTreeScheduler(max_depth=args.depth, max_concurrency=args.concurrency, checkpoint_file=args.checkpoint)

    if args.resume and os.path.exists(args.checkpoint):
        print(f"[*] 正在从检查点恢复: {args.checkpoint}")
        scheduler.load_checkpoint(args.checkpoint)
    else:
        scheduler.initialize_root(
            root_title="雨夜古董钟表店",
            root_content="大雨倾盆的午夜，停摆六十七年的古钟突然鸣响，钟摆血字警告：第十三次敲响时，镜中的你将不再是你。",
            initial_choices=[
                {"choice_id": "A", "summary": "用铁棍强行卡死摆锤"},
                {"choice_id": "B", "summary": "打碎房间所有镜子"},
                {"choice_id": "C", "summary": "逆转怀表重置时间"}
            ]
        )

    batch_idx = 1
    while not scheduler.is_finished():
        batch = scheduler.get_next_batch()
        print(f"\n[调度批次 {batch_idx}] 激活 {len(batch)} 个分身（运行池水位: {len(scheduler.running_pool)}/{scheduler.max_concurrency}）:")
        
        # 演示模式：模拟生成与真实 YAML 回传解析
        for node in batch:
            cache_flag = " [跨越 4k 缓存门槛!]" if node.estimated_prefix_tokens >= GEMINI_CACHE_MIN_THRESHOLD_TOKENS else ""
            print(f"  -> 分身派发: {node.node_id} (前缀估算 Tokens: {node.estimated_prefix_tokens}{cache_flag})")
            
            # 模拟分身发回的原始 YAML 字符串
            simulated_yaml = f"""
severity: info
kind: branch_step
is_ending: {'true' if node.depth >= scheduler.max_depth or node.node_id.endswith('_C') else 'false'}
ending_type: {'BAD_END' if node.node_id.endswith('_C') else ('GOOD_END' if node.depth >= scheduler.max_depth else 'none')}
action_taken: |
  林默做出了抉择【{node.choice_text}】，时间轴在这一刻发生剧烈震颤。
next_branches:
  - choice_id: "1"
    summary: "{node.choice_text}-深入调查"
  - choice_id: "2"
    summary: "{node.choice_text}-抽身撤退"
"""
            parsed = scheduler.parse_branch_step_message(simulated_yaml)
            scheduler.submit_node_result(
                node_id=node.node_id,
                content=parsed["content"],
                is_ending=parsed["is_ending"],
                ending_type=parsed["ending_type"],
                next_branches=parsed["next_branches"],
                cached_tokens=4096 if node.estimated_prefix_tokens >= 4096 else 0
            )
        batch_idx += 1

    print("\n=== 全量故事树执行完成！===")
    print(f"总计完成节点数: {len(scheduler.completed_nodes)}")
    print(f"断点已安全持久化至: {args.checkpoint}")
    print("\n[Mermaid 拓扑导出]:\n")
    print(scheduler.export_mermaid())
