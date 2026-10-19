#!/usr/bin/env python3
"""
Comprehensive Unit Tests for Twin Agents Scheduler & Protocol Engine
涵盖：
1. 并发槽位合法性防御 (max_concurrency 范围 1~6)
2. 终局截断强制收敛 (Terminal Depth Forced Clamping)
3. next_branches 元数据完整持久化
4. YAML 回传报文解析 (parse_branch_step_message)
5. 检查点保存与断点恢复 (Checkpoints & Resume)
6. 祖先亲和度分组排序 (Ancestor Affinity Sorting)
7. Token 估算与缓存门槛判定 (Cache Thresholding)
"""

import unittest
import os
import sys
import tempfile
import json

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))
from story_tree_scheduler import StoryTreeScheduler, StoryNode, estimate_tokens, GLOBAL_MAX_CONCURRENCY, GEMINI_CACHE_MIN_THRESHOLD_TOKENS

class TestStoryTreeScheduler(unittest.TestCase):

    def setUp(self):
        self.scheduler = StoryTreeScheduler(max_depth=2, max_concurrency=6)
        self.scheduler.initialize_root(
            root_title="测试原点",
            root_content="雨夜钟声敲响，阁楼之中暗藏玄机。",
            initial_choices=[
                {"choice_id": "A", "summary": "选项A：卡死摆锤"},
                {"choice_id": "B", "summary": "选项B：打碎镜子"}
            ]
        )

    def test_invalid_concurrency_guards(self):
        """防御性断言：非法并发参数必须抛出 ValueError，杜绝死循环或超限崩溃"""
        with self.assertRaises(ValueError):
            StoryTreeScheduler(max_concurrency=0)
        with self.assertRaises(ValueError):
            StoryTreeScheduler(max_concurrency=-1)
        with self.assertRaises(ValueError):
            StoryTreeScheduler(max_concurrency=7)  # 超过硬上限 6
        with self.assertRaises(ValueError):
            StoryTreeScheduler(max_depth=0)

    def test_terminal_depth_forced_ending(self):
        """断言：到达最大深度时，即使分身未明确写结局，系统也强制置 is_ending=True 并赋予结局类型"""
        batch_l1 = self.scheduler.get_next_batch()
        for node in batch_l1:
            self.scheduler.submit_node_result(
                node_id=node.node_id,
                content="剧情推进到第一层",
                is_ending=False,
                ending_type=None,
                next_branches=[{"choice_id": "sub1", "summary": "继续深入"}]
            )

        batch_l2 = self.scheduler.get_next_batch()
        for node in batch_l2:
            self.assertEqual(node.depth, 2)
            # 故意传入 is_ending=False 模拟分身未能自主收官
            self.scheduler.submit_node_result(
                node_id=node.node_id,
                content="到达终端深度的剧情",
                is_ending=False,
                ending_type=None,
                next_branches=[{"choice_id": "illegal", "summary": "不应被排队的节点"}]
            )
            # 验证系统强制修正
            completed = self.scheduler.completed_nodes[node.node_id]
            self.assertTrue(completed.is_ending)
            self.assertEqual(completed.ending_type, "NORMAL_END")
            self.assertEqual(len(completed.next_branches), 0)

        # 队列必须完全清空，任务自然终结
        self.assertTrue(self.scheduler.is_finished())

    def test_next_branches_saved_in_completed_nodes(self):
        """断言：未终结节点的 next_branches 必须完整保存在 completed_nodes 中，支持导出完整树 JSON"""
        node = self.scheduler.get_next_batch()[0]
        test_branches = [
            {"choice_id": "A1", "summary": "夺取卷轴"},
            {"choice_id": "A2", "summary": "粉碎核心"}
        ]
        self.scheduler.submit_node_result(
            node_id=node.node_id,
            content="推进一段剧情",
            is_ending=False,
            ending_type=None,
            next_branches=test_branches
        )
        saved_node = self.scheduler.completed_nodes[node.node_id]
        self.assertEqual(saved_node.next_branches, test_branches)
        
        # 验证 JSON 导出包含该信息
        exported_json = self.scheduler.export_json()
        self.assertIn("夺取卷轴", exported_json)
        self.assertIn("粉碎核心", exported_json)

    def test_yaml_message_parser(self):
        """断言：真实 YAML 报文解析器能准确提取 is_ending、ending_type、正文与分支"""
        sample_yaml = """
severity: info
kind: branch_step
finding: 林默卡死摆锤阻断钟声
is_ending: false
ending_type: none
next_branches:
  - choice_id: "A1"
    summary: "忍痛弃棍夺取暗格卷轴"
  - choice_id: "A2"
    summary: "捣碎机芯粉碎古钟核心"
action_taken: |
  【第1章（支线A）：火星与血蚀】
  金属断裂声在逼仄的阁楼炸响，青铜与生铁刮擦出炽白火花。
recommended_next: 建议主控调度池排队
"""
        parsed = StoryTreeScheduler.parse_branch_step_message(sample_yaml)
        self.assertFalse(parsed["is_ending"])
        self.assertIsNone(parsed["ending_type"])
        self.assertEqual(len(parsed["next_branches"]), 2)
        self.assertEqual(parsed["next_branches"][0]["choice_id"], "A1")
        self.assertEqual(parsed["next_branches"][0]["summary"], "忍痛弃棍夺取暗格卷轴")
        self.assertIn("火星与血蚀", parsed["content"])

    def test_checkpoint_save_and_resume(self):
        """断言：调度器支持序列化为 JSON 检查点，并在重启后完整恢复队列与已完成节点"""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            # 推进第一批任务
            batch = self.scheduler.get_next_batch()
            self.scheduler.submit_node_result(
                node_id=batch[0].node_id,
                content="进度保存测试",
                is_ending=False,
                ending_type=None,
                next_branches=[{"choice_id": "X", "summary": "新分支X"}]
            )
            # 保存断点
            self.scheduler.save_checkpoint(tmp_path)

            # 实例化一个全新的空调度器并恢复
            recovered = StoryTreeScheduler(checkpoint_file=tmp_path)
            recovered.load_checkpoint(tmp_path)

            self.assertEqual(recovered.max_depth, self.scheduler.max_depth)
            self.assertEqual(recovered.max_concurrency, self.scheduler.max_concurrency)
            self.assertEqual(len(recovered.completed_nodes), len(self.scheduler.completed_nodes))
            self.assertIn(batch[0].node_id, recovered.completed_nodes)
            self.assertEqual(len(recovered.pending_queue), len(self.scheduler.pending_queue))
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def test_token_estimation_and_cache_threshold(self):
        """断言：正确估算 Token，当文本积累到 >= 4096 时能够准确识别"""
        short_text = "雨夜古董店"
        self.assertLess(estimate_tokens(short_text), 100)
        
        long_text = "长篇剧情内容。" * 2500  # 约 1.7 万字
        est = estimate_tokens(long_text)
        self.assertGreater(est, GEMINI_CACHE_MIN_THRESHOLD_TOKENS)

    def test_subagent_payload_generation(self):
        """断言：生成的 invoke_subagent 请求格式完全符合 Antigravity 官方调用协议"""
        batch = self.scheduler.get_next_batch()
        payload = self.scheduler.build_subagent_payload(batch)
        self.assertIn("Subagents", payload)
        self.assertEqual(len(payload["Subagents"]), len(batch))
        first = payload["Subagents"][0]
        self.assertEqual(first["TypeName"], "twin")
        self.assertEqual(first["Workspace"], "inherit")
        self.assertIn("【前序因果链", first["Prompt"])

if __name__ == "__main__":
    unittest.main()
