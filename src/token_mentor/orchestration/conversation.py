"""Short-lived context around one recommendation turn."""
from hashlib import sha256

from token_mentor.constraints.recovery import exclusive_run
from token_mentor.context.short_memory import compress_turn
from token_mentor.orchestration.agents import ProjectAdvisorAgent
from token_mentor.orchestration.service import RecommendationService
from token_mentor.state.short_memory import ShortMemory


class ConversationAdvisor:
    def __init__(self, service):
        self.service = RecommendationService(service.catalog,
            cache_ttl_seconds=min(service.cache_ttl_seconds, 1800), freshness_days=service.freshness_days)
        self.memory = ShortMemory(service.catalog.path)

    def lock(self, user_id, session_id):
        identity = sha256(f'{user_id}\0{session_id}'.encode()).hexdigest()
        return exclusive_run(self.service.catalog.path.parent / 'session-locks' / f'{identity}.lock')

    def recommend(self, user_id, session_id, message, **kwargs):
        with self.lock(user_id, session_id):
            previous = self.memory.read(user_id, session_id)
            intent = (f'历史需求摘要（仅作上下文，以本轮修改为准）：\n{previous}\n\n本轮用户要求：\n{message}'
                      if previous else message)
            result = ProjectAdvisorAgent(self.service).from_intent(intent, **kwargs)
            highlights = '\n'.join(line for line in result.markdown.splitlines()
                                   if line.startswith(('- **推荐任务**', '| 1 ', '| 1|', '| 1 |')))
            self.memory.save(user_id, session_id, compress_turn(previous, message, highlights))
            return result

    def clear(self, user_id, session_id):
        with self.lock(user_id, session_id):
            self.memory.clear(user_id, session_id)
