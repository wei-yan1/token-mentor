"""Task intent contracts shared by rule and optional local Laya classifiers."""
from __future__ import annotations

from dataclasses import dataclass

from token_mentor.context.analysis import infer_tasks

TASK_TERMS = {
    "embeddings": ("embedding", "embeddings", "向量化", "向量检索", "文本向量"),
    "speech_recognition": ("speech recognition", "asr", "语音识别", "语音转文字", "转录"),
    "speech_generation": ("text to speech", "tts", "语音合成", "文本转语音", "配音"),
    "video_generation": ("video generation", "视频生成", "生成视频"),
    "video_understanding": ("video understanding", "视频理解", "视频分析"),
    "image_generation": ("image generation", "图像生成", "图片生成", "文生图"),
    "multimodal": ("multimodal", "vision", "多模态", "图像理解", "视觉理解"),
    "reranking": ("rerank", "reranking", "重排", "精排"),
    "3d_generation": ("3d generation", "3d生成", "三维生成"),
    "self_hosted": ("self-hosted", "self hosted", "自部署", "本地部署", "私有化部署"),
}
ALLOWED_TASKS = frozenset({"coding", "reasoning", "translation", "summarization", "rewrite", "general", *TASK_TERMS})


@dataclass(frozen=True, slots=True)
class TaskIntent:
    primary_task: str
    tasks: tuple[str, ...]
    confidence: float
    rationale: str
    source: str


def rule_classify(message: str) -> TaskIntent:
    lowered = message.casefold()
    lane_matches = [task for task, terms in TASK_TERMS.items() if any(term.casefold() in lowered for term in terms)]
    if lane_matches:
        tasks = tuple(lane_matches)
        return TaskIntent(tasks[0], tasks, 0.75, "基于用户明确描述的模型任务类型", "rules")
    tasks = tuple(task for task in infer_tasks(message) if task in ALLOWED_TASKS)
    if not tasks:
        tasks = ("general",)
    return TaskIntent(tasks[0], tasks, 0.55, "基于本地任务词典", "rules")
