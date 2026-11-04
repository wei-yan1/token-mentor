"""Intent decision orchestration with optional local Laya classification."""
from __future__ import annotations

import os
from typing import Any

from token_mentor.context.task_intent import ALLOWED_TASKS, TaskIntent, rule_classify

TASK_ALIASES = {
    "embedding": "embeddings", "vectorization": "embeddings", "vector_search": "embeddings",
    "asr": "speech_recognition", "speech_to_text": "speech_recognition",
    "tts": "speech_generation", "text_to_speech": "speech_generation",
    "video": "video_understanding", "image": "multimodal", "visual_understanding": "multimodal",
    "text_generation": "general", "chat": "general", "analysis": "reasoning",
}


class LayaIntentClassifier:
    def __init__(self) -> None:
        self.model_name = os.getenv("TOKEN_MENTOR_LAYA_MODEL", "convaiinnovations/laya-multilingual")
        self.device = os.getenv("TOKEN_MENTOR_LAYA_DEVICE", "auto")
        self._agent: Any | None = None

    def _load(self) -> Any:
        if self._agent is None:
            import laya

            loader = getattr(laya, "load", None)
            if not callable(loader):
                raise RuntimeError("本地 laya 包未提供 load()")
            self._agent = loader(self.model_name, **({} if self.device == "auto" else {"device": self.device}))
        return self._agent

    def classify(self, message: str) -> TaskIntent:
        questions = {
            "primary_task": {
                "type": "choice",
                "instructions": "识别用户项目或请求最主要的大模型任务类型，只根据用户提供的内容判断，不评价厂商或价格。",
                "criteria": {
                    "coding": "代码生成、代码修改、软件开发或测试",
                    "reasoning": "复杂推理、分析、规划、决策或工具编排",
                    "translation": "翻译、本地化或跨语言转换",
                    "summarization": "长文摘要、信息提取或报告生成",
                    "rewrite": "改写、润色或内容生成",
                    "embeddings": "文本向量化、多模态向量或向量检索",
                    "speech_recognition": "语音识别、ASR 或音频转写",
                    "speech_generation": "语音合成、TTS 或文本转语音",
                    "video_generation": "视频生成",
                    "video_understanding": "视频理解或视频分析",
                    "image_generation": "图像生成或文生图",
                    "multimodal": "多模态、视觉或图像理解",
                    "reranking": "搜索结果重排或文本精排",
                    "3d_generation": "3D 或三维内容生成",
                    "self_hosted": "自部署、私有化或本地推理模型",
                    "general": "通用问答、聊天或无法归入其他类型的任务",
                },
            },
        }
        result = self._load().predict({"message": message}, questions)
        if not isinstance(result, dict):
            raise TypeError("Laya 意图分类结果必须是对象")
        primary = str(result.get("primary_task", result.get("intent", ""))).strip().lower()
        primary = TASK_ALIASES.get(primary, primary)
        if primary not in ALLOWED_TASKS:
            raise ValueError("Laya 返回了不支持的任务类别")
        rules = rule_classify(message)
        tasks = tuple(dict.fromkeys((primary, *rules.tasks)))
        return TaskIntent(primary, tasks, 0.75,
                          str(result.get("rationale", f"Laya 将主要任务判断为 {primary}"))[:500], "laya")


class TaskIntentDecisionAgent:
    """Classify work with Laya, then let deterministic price ranking choose models."""

    def __init__(self, classifier: LayaIntentClassifier | None = None) -> None:
        self.classifier = classifier or LayaIntentClassifier()

    def classify(self, message: str) -> TaskIntent:
        if not message.strip():
            raise ValueError("待判断的项目用途不能为空")
        rule_result = rule_classify(message)
        mode = os.getenv("TOKEN_MENTOR_DECISION_BACKEND", "laya").strip().lower()
        if mode == "rule":
            return rule_result
        if mode not in {"laya", "laya_shadow"}:
            raise ValueError("TOKEN_MENTOR_DECISION_BACKEND 只能是 laya、laya_shadow 或 rule")
        try:
            laya_result = self.classifier.classify(message)
        except (ImportError, RuntimeError, ValueError, OSError, TypeError, AttributeError):
            return TaskIntent(rule_result.primary_task, rule_result.tasks, rule_result.confidence,
                              "本地 Laya 不可用，已回退到任务规则", "rule_fallback")
        combined_tasks = tuple(dict.fromkeys((laya_result.primary_task, *laya_result.tasks, *rule_result.tasks)))
        if mode == "laya_shadow":
            return TaskIntent(rule_result.primary_task, rule_result.tasks, rule_result.confidence,
                              f"规则判断；Laya 对照类别：{laya_result.primary_task}", "rule_shadow")
        return TaskIntent(laya_result.primary_task, combined_tasks, laya_result.confidence,
                          laya_result.rationale, laya_result.source)
