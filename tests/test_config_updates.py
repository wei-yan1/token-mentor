import json
import tempfile
import unittest
from pathlib import Path

from token_mentor.models import ModelQuote, utc_now
from token_mentor.orchestration.config_updates import ConfigUpdateService
from token_mentor.state.storage import Catalog


class ConfigUpdateTests(unittest.TestCase):
    def setup_case(self, root):
        config = root / 'models.json'
        config.write_text(json.dumps({'provider': 'Fixture', 'model': 'old', 'temperature': 0.3}))
        catalog = Catalog(root / 'catalog.sqlite3')
        for model, cost in [('old', 4), ('new', 1)]:
            catalog.upsert_quotes([ModelQuote('Fixture', model, cost, cost * 2,
                'https://example.invalid/prices', utc_now(), capabilities={'coding': 0.9})])
        service = ConfigUpdateService(catalog)
        service.register('demo', str(config), 'coding')
        return config, service

    def test_propose_apply_restart_and_rollback_preserves_other_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            config, service = self.setup_case(Path(tmp))
            version = service.propose('demo')
            self.assertLess(version['new_cost_cny'], version['old_cost_cny'])
            self.assertEqual(json.loads(config.read_text())['model'], 'old')
            service.change(version['id'])
            self.assertEqual(json.loads(config.read_text()), {'provider': 'Fixture', 'model': 'new', 'temperature': 0.3})
            restarted = ConfigUpdateService(service.catalog)
            restarted.change(version['id'], rollback=True)
            self.assertEqual(json.loads(config.read_text())['model'], 'old')
            self.assertEqual(restarted.versions.get(version['id'])['status'], 'rolled_back')

    def test_external_config_change_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            config, service = self.setup_case(Path(tmp))
            version = service.propose('demo')
            config.write_text(json.dumps({'provider': 'Fixture', 'model': 'user-change'}))
            with self.assertRaisesRegex(ValueError, '配置已被修改'):
                service.change(version['id'])
            self.assertEqual(json.loads(config.read_text())['model'], 'user-change')

    def test_missing_task_evidence_blocks_automatic_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            config, service = self.setup_case(Path(tmp))
            service.register('other-task', str(config), 'translation')
            with self.assertRaisesRegex(ValueError, '能力评分'):
                service.propose('other-task')

    def test_price_change_invalidates_proposal(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, service = self.setup_case(Path(tmp))
            version = service.propose('demo')
            quote = version['quote']
            quote['input_price_per_million'] = 100
            service.catalog.upsert_quotes([ModelQuote.from_dict(quote)])
            with self.assertRaisesRegex(ValueError, '已变化'):
                service.change(version['id'])
