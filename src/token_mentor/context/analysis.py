from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import os

from token_mentor.models import ProjectAnalysis


from token_mentor.constraints.scanning import (
    IGNORED_DIRS, IGNORED_FILE_NAMES, READABLE_SUFFIXES, MAX_FILES,
    MAX_DIRECTORIES, MAX_DEPTH, MAX_FILE_CHARS, MAX_TOTAL_CHARS,
)

TASK_TERMS: dict[str, tuple[str, ...]] = {
    "coding": ("代码", "开发", "编程", "接口", "api", "backend", "python", "javascript", "软件", "实现", "coding", "refactor", "测试"),
    "reasoning": ("架构", "规划", "决策", "推理", "agent", "方案", "分析", "推荐", "research", "reasoning"),
    "translation": ("翻译", "多语言", "translate", "localization", "i18n"),
    "summarization": ("摘要", "总结", "长文", "summarize", "summary", "报告"),
    "rewrite": ("改写", "润色", "文案", "rewrite", "copy edit"),
    "general": ("聊天", "问答", "assistant", "chatbot", "客服", "通用"),
}

TECH_TERMS = {
    "FastAPI": ("fastapi",),
    "Django": ("django",),
    "Flask": ("flask",),
    "PostgreSQL": ("postgres", "postgresql"),
    "SQLite": ("sqlite",),
    "Redis": ("redis",),
    "MCP": ("mcp", "model context protocol"),
    "OpenAI-compatible API": ("openai", "chat/completions"),
}


def _files(root: Path) -> list[Path]:
    picked: list[Path] = []
    for visited, (current, directories, filenames) in enumerate(os.walk(root, topdown=True)):
        if len(picked) >= MAX_FILES * 20:
            break
        current_path = Path(current)
        depth = len(current_path.relative_to(root).parts)
        if visited >= MAX_DIRECTORIES or depth >= MAX_DEPTH:
            directories[:] = []
        else:
            directories[:] = sorted(directory for directory in directories if directory.lower() not in IGNORED_DIRS)
        for filename in filenames:
            path = Path(current) / filename
            if filename.lower() in IGNORED_FILE_NAMES or filename.lower().endswith((".key", ".pem", ".p12", ".pfx")):
                continue
            special = filename.lower().startswith("readme") or filename.lower() in {
                "pyproject.toml", "requirements.txt", "setup.cfg", "cargo.toml", "package.json"
            }
            if special or path.suffix.lower() in READABLE_SUFFIXES:
                picked.append(path)
    return sorted(
        picked,
        key=lambda p: (
            0 if p.name.lower().startswith("readme") else
            1 if p.name.lower() in {"pyproject.toml", "requirements.txt", "setup.cfg", "package.json"} else
            2 if p.suffix.lower() == ".md" else 3,
            str(p).lower(),
        ),
    )[:MAX_FILES]


def infer_tasks(text: str, *, has_python: bool = False, has_docs: bool = False) -> tuple[str, ...]:
    lowered = text.lower()
    scores = {
        task: sum(1 for term in terms if term in lowered)
        for task, terms in TASK_TERMS.items()
    }
    tasks = [task for task in TASK_TERMS if scores[task] > 0]
    if has_python and "coding" not in tasks:
        tasks.append("coding")
    if has_docs and "summarization" not in tasks:
        tasks.append("summarization")
    if not tasks:
        tasks = ["general"]
    return tuple(sorted(set(tasks), key=lambda task: (-scores.get(task, 0), list(TASK_TERMS).index(task))))


def analyze_project(root: str | Path, intent: str | None = None) -> ProjectAnalysis:
    project_root = Path(root).expanduser().resolve()
    if not project_root.exists() or not project_root.is_dir():
        raise ValueError(f"项目目录不存在或不是目录：{project_root}")

    files = _files(project_root)
    pieces: list[str] = []
    included: list[str] = []
    remaining = MAX_TOTAL_CHARS
    for path in files:
        if remaining <= 0:
            break
        try:
            text = path.read_text(encoding="utf-8", errors="replace")[: min(MAX_FILE_CHARS, remaining)]
        except OSError:
            continue
        if text.strip():
            pieces.append(text)
            included.append(path.relative_to(project_root).as_posix())
            remaining -= len(text)

    names = [p.name for p in project_root.iterdir() if p.is_file()]
    corpus = "\n".join(pieces)
    user_text = intent.strip() if intent and intent.strip() else ""
    combined = f"{user_text}\n{corpus}\n{' '.join(names)}"
    if user_text:
        tasks = infer_tasks(user_text)
        if tasks == ("general",):
            tasks = infer_tasks(
                combined,
                has_python=any(path.suffix.lower() == ".py" for path in files),
                has_docs=any(path.suffix.lower() == ".md" for path in files),
            )
    else:
        tasks = infer_tasks(
            combined,
            has_python=any(path.suffix.lower() == ".py" for path in files),
            has_docs=any(path.suffix.lower() == ".md" for path in files),
        )
    technologies = tuple(
        name for name, terms in TECH_TERMS.items()
        if any(term in combined.lower() for term in terms)
    )
    evidence: list[str] = []
    if user_text:
        evidence.append("用户提供了项目用途/意图")
    if any(Path(name).name.lower().startswith("readme") for name in included):
        evidence.append("读取了 README")
    elif any(name.lower().endswith(".md") for name in included):
        evidence.append("未发现 README；读取了 Markdown 文档")
    elif included:
        evidence.append("未发现项目说明；依据少量源码和配置文件推断")
    else:
        evidence.append("未发现可读取的说明或源码；依据目录名和用户输入推断")
    evidence.extend(f"技术线索：{technology}" for technology in technologies)

    if user_text:
        summary = user_text[:500]
    elif corpus.strip():
        summary = f"根据 {len(included)} 个项目文件和目录信息推断的用途；请在报告中核对并补充。"
    else:
        summary = f"{project_root.name} 项目尚无可识别说明；请使用 --intent 输入用途以获得更准确的推荐。"
    fingerprint = sha256((combined + "\n" + "\n".join(included)).encode("utf-8")).hexdigest()
    return ProjectAnalysis(
        root=str(project_root),
        summary=summary,
        evidence=tuple(evidence),
        tasks=tasks,
        technologies=technologies,
        content_fingerprint=fingerprint,
    )

