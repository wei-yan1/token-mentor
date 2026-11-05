"""Small, explicit propose/apply/rollback loop for local JSON model configs."""
import json
import os
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from token_mentor.constraints.recovery import exclusive_run
from token_mentor.models import parse_time, utc_now
from token_mentor.state.config_versions import ConfigVersions
from token_mentor.state.storage import Catalog


def read_config(path: Path) -> dict:
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict) or any(not isinstance(value.get(k), str) or not value[k].strip()
                                          for k in ('provider', 'model')):
        raise ValueError('配置必须是含 provider、model 非空字符串的 JSON 对象')
    return value


def write_config(path: Path, value: dict) -> None:
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.model-update-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class ConfigUpdateService:
    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog
        self.versions = ConfigVersions(catalog.path)

    def register(self, project_id: str, config_path: str, task: str) -> dict:
        from token_mentor.context.task_intent import ALLOWED_TASKS
        if not project_id.strip() or task not in ALLOWED_TASKS:
            raise ValueError('项目 ID 不能为空且 task 必须是已支持的任务类别')
        path = Path(config_path).resolve()
        config = read_config(path)
        self.versions.register(project_id, str(path), task)
        return {'project_id': project_id, 'task': task, 'config': config}

    def propose(self, project_id: str) -> dict:
        from token_mentor.orchestration.report import capabilities_for_task
        path, task = self.versions.project(project_id)
        before = read_config(Path(path))
        quotes = self.catalog.latest_quotes()
        cutoff = datetime.now(UTC) - timedelta(days=30)
        quotes = [q for q in quotes if cutoff <= parse_time(q.observed_at) <= datetime.now(UTC)]
        current = next((q for q in quotes if (q.provider, q.model) == (before['provider'], before['model'])), None)
        if current is None:
            raise ValueError('当前模型没有正式报价，无法计算替换收益')
        aliases = capabilities_for_task(task)
        # Require a comparable numeric score on the same task for both models.
        axis = next((a for a in aliases if a in current.capabilities), None)
        if axis is None:
            raise ValueError('当前模型缺少任务能力评分；暂不自动建议替换')
        eligible = [q for q in quotes if axis in q.capabilities
                    and q.capabilities[axis] >= current.capabilities[axis]
                    and q.input_price_per_million <= current.input_price_per_million
                    and q.output_price_per_million <= current.output_price_per_million
                    and (q.input_price_per_million < current.input_price_per_million
                         or q.output_price_per_million < current.output_price_per_million)]
        if not eligible:
            return {'project_id': project_id, 'status': 'keep', 'reason': '未发现任务评分不降且输入输出单价均不升的替代模型'}
        best = min(eligible, key=lambda q: (q.estimated_cost(4000, 1200), q.provider, q.model))
        after = {**before, 'provider': best.provider, 'model': best.model}
        version = {'id': uuid.uuid4().hex, 'project_id': project_id, 'status': 'proposed',
                   'created_at': utc_now(), 'before': before, 'after': after, 'task': task,
                   'reason': '同任务评分不降，输入输出单价均不升；成本估算按 4000 输入、1200 输出 token',
                   'old_cost_cny': current.estimated_cost(4000, 1200),
                   'new_cost_cny': best.estimated_cost(4000, 1200),
                   'quote': best.to_dict(), 'current_quote': current.to_dict()}
        self.versions.save(version)
        return version

    def change(self, version_id: str, *, rollback: bool = False) -> dict:
        version = self.versions.get(version_id)
        path, _ = self.versions.project(version['project_id'])
        path = Path(path)
        with exclusive_run(path.parent / (path.name + '.update.lock')):
            version = self.versions.get(version_id)
            expected = version['after'] if rollback else version['before']
            desired = version['before'] if rollback else version['after']
            allowed = {'applied', 'applying', 'rolling_back'} if rollback else {'proposed', 'applying'}
            if version['status'] not in allowed:
                raise ValueError('当前版本状态不允许执行此操作')
            actual = read_config(path)
            recovering = version['status'] in {'applying', 'rolling_back'}
            if actual != expected and not (recovering and actual == desired):
                raise ValueError('配置已被修改，拒绝覆盖；请重新生成建议')
            if not rollback:
                if any(parse_time(q['observed_at']) < datetime.now(UTC) - timedelta(days=30)
                       for q in (version['quote'], version['current_quote'])):
                    raise ValueError('报价已过期，请重新采集并生成建议')
                latest = {(q.provider, q.model): q.to_dict() for q in self.catalog.latest_quotes()}
                for snapshot in (version['quote'], version['current_quote']):
                    if json.dumps(latest.get((snapshot['provider'], snapshot['model'])), sort_keys=True) != json.dumps(snapshot, sort_keys=True):
                        raise ValueError('报价或能力评分已变化，请重新生成建议')
            version['status'] = 'rolling_back' if rollback else 'applying'
            self.versions.save(version)
            write_config(path, desired)
            if read_config(path) != desired:
                write_config(path, expected)
                raise RuntimeError('配置写入校验失败，已恢复旧配置')
            version['status'] = 'rolled_back' if rollback else 'applied'
            version['updated_at'] = utc_now()
            self.versions.save(version)
        return version
