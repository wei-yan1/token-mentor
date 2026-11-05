import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from token_mentor.context.short_memory import compress_turn
from token_mentor.orchestration.conversation import ConversationAdvisor
from token_mentor.orchestration.service import RecommendationResult, RecommendationService
from token_mentor.state.storage import Catalog


class ShortMemoryTests(unittest.TestCase):
    def test_turn_context_is_compressed_isolated_expiring_and_clearable(self):
        with tempfile.TemporaryDirectory() as tmp:
            advisor = ConversationAdvisor(RecommendationService(Catalog(Path(tmp) / 'db.sqlite3')))
            seen = []
            def recommend(_, intent, **kwargs):
                seen.append(intent)
                return RecommendationResult('report', '- **推荐任务**：语音识别\n| 1 | fixture/model |', False)
            with patch('token_mentor.orchestration.conversation.ProjectAdvisorAgent.from_intent', recommend):
                advisor.recommend('alice', 'one', '我想做语音识别')
                advisor.recommend('alice', 'one', '预算降低一点')
                self.assertIn('语音识别', seen[-1])
                self.assertIn('预算降低一点', seen[-1])
                advisor.recommend('bob', 'one', '新需求')
                self.assertEqual(seen[-1], '新需求')
                advisor.recommend('alice', 'two', '另一会话')
                self.assertEqual(seen[-1], '另一会话')
                advisor.clear('alice', 'one')
                self.assertEqual(advisor.memory.read('alice', 'one'), '')
                with patch('token_mentor.state.short_memory.time.time', return_value=10):
                    advisor.memory.save('alice', 'one', '短期摘要')
                with patch('token_mentor.state.short_memory.time.time', return_value=1811):
                    self.assertEqual(advisor.memory.read('alice', 'one'), '')

    def test_summary_size_is_bounded_across_many_turns(self):
        summary = ''
        for i in range(100):
            summary = compress_turn(summary, '用途' * 4000 + f'最新要求{i}', '答复' * 1000)
            self.assertLessEqual(len(summary), 2000)
        self.assertIn('最新要求99', summary)

    def test_failed_recommendation_does_not_commit_new_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            advisor = ConversationAdvisor(RecommendationService(Catalog(Path(tmp) / 'db.sqlite3')))
            advisor.memory.save('alice', 'one', '先前摘要')
            with patch('token_mentor.orchestration.conversation.ProjectAdvisorAgent.from_intent', side_effect=ValueError('failed')):
                with self.assertRaises(ValueError):
                    advisor.recommend('alice', 'one', '失败本轮')
            self.assertEqual(advisor.memory.read('alice', 'one'), '先前摘要')
