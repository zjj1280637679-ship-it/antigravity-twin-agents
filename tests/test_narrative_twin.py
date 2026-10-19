#!/usr/bin/env python3
"""
Unit tests for Twin Agents story tree scheduler & protocol validation.
"""

import unittest
import sys
import os

# 将 scripts 目录加入 sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))
from story_tree_scheduler import StoryTreeScheduler, StoryNode

class TestStoryTreeScheduler(unittest.TestCase):

    def setUp(self):
        self.scheduler = StoryTreeScheduler(max_depth=2, max_concurrency=6)
        self.scheduler.initialize_root(
            root_title="测试原点",
            root_content="这是一个测试用的初始剧情节点。",
            initial_choices=[
                {"choice_id": "A", "summary": "走向光明路径"},
                {"choice_id": "B", "summary": "潜入黑暗密道"},
                {"choice_id": "C", "summary": "驻留原地探索"}
            ]
        )

    def test_initial_state(self):
        """验证初始状态：根节点已完成，队列有 3 个待执行分支"""
        self.assertIn("root", self.scheduler.completed_nodes)
        self.assertEqual(len(self.scheduler.pending_queue), 3)
        self.assertEqual(len(self.scheduler.running_pool), 0)

    def test_concurrency_pool_limit(self):
        """严格断言：并发运行池中任务数绝不超过 max_concurrency (6)"""
        # 手动塞入 10 个待执行任务
        for i in range(10):
            node = StoryNode(
                node_id=f"mock_{i}",
                parent_id="root",
                depth=1,
                choice_text=f"选项_{i}",
                ancestor_chain="测试历史"
            )
            self.scheduler.pending_queue.append(node)

        batch = self.scheduler.get_next_batch()
        self.assertLessEqual(len(batch), self.scheduler.max_concurrency)
        self.assertLessEqual(len(self.scheduler.running_pool), self.scheduler.max_concurrency)
        self.assertEqual(len(self.scheduler.running_pool), 6)

    def test_ancestor_affinity_grouping(self):
        """验证祖先亲和度排序：相同父节点的分支排在一起，以最大化 Prefix Caching 命中"""
        self.scheduler.pending_queue.clear()
        node_b = StoryNode("node_b", parent_id="parent_2", depth=2, choice_text="B", ancestor_chain="C")
        node_a = StoryNode("node_a", parent_id="parent_1", depth=2, choice_text="A", ancestor_chain="C")
        node_c = StoryNode("node_c", parent_id="parent_1", depth=2, choice_text="C", ancestor_chain="C")
        
        self.scheduler.pending_queue.extend([node_b, node_a, node_c])
        self.scheduler.sort_queue_by_ancestor_affinity()

        # 排序后 parent_1 的任务（node_a, node_c）应相邻连续排列
        parents = [n.parent_id for n in self.scheduler.pending_queue]
        self.assertEqual(parents, ["parent_1", "parent_1", "parent_2"])

    def test_depth_clamping_and_termination(self):
        """验证最大深度截断：到达 max_depth 时强制终局收尾，不再裂变子节点"""
        batch = self.scheduler.get_next_batch()
        # 处理 Layer 1
        for node in batch:
            self.scheduler.submit_node_result(
                node_id=node.node_id,
                content="推演走向...",
                is_ending=False,
                ending_type=None,
                next_branches=[{"choice_id": "sub1", "summary": "进一步分支"}]
            )

        # 取出 Layer 2 (已达到 max_depth=2)
        batch_l2 = self.scheduler.get_next_batch()
        for node in batch_l2:
            self.assertEqual(node.depth, 2)
            # 提交时即使提供 next_branches，由于达到 max_depth，也不会入队新节点
            self.scheduler.submit_node_result(
                node_id=node.node_id,
                content="最终收束...",
                is_ending=True,
                ending_type="GOOD_END",
                next_branches=[{"choice_id": "overflow", "summary": "不应出现的节点"}]
            )

        self.assertTrue(self.scheduler.is_finished())
        self.assertEqual(len(self.scheduler.pending_queue), 0)

if __name__ == "__main__":
    unittest.main()
