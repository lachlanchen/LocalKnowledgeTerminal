from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    """Runtime settings loaded from environment variables."""

    project_root: Path
    data_dir: Path
    corpus_db: Path
    answers_db: Path
    questions_db: Path
    roots_db: Path
    affixes_db: Path
    cards_db: Path
    knowledge_db: Path
    graph_db: Path
    freedict_db: Path
    jmdict_db: Path
    llm_url: str
    llm_model: str
    host: str
    port: int
    request_timeout: int
    max_evidence: int

    @classmethod
    def from_env(cls) -> "Settings":
        project_root = Path(
            os.environ.get("LKT_SOURCE", Path(__file__).resolve().parents[1])
        ).resolve()
        data_dir = Path(
            os.environ.get("LKT_DATA_DIR", project_root / "var")
        ).resolve()
        return cls(
            project_root=project_root,
            data_dir=data_dir,
            corpus_db=Path(
                os.environ.get("LKT_CORPUS_DB", data_dir / "word-origins.sqlite3")
            ).resolve(),
            answers_db=Path(
                os.environ.get("LKT_ANSWERS_DB", data_dir / "book-of-answers.sqlite3")
            ).resolve(),
            questions_db=Path(
                os.environ.get("LKT_QUESTIONS_DB", data_dir / "book-of-questions.sqlite3")
            ).resolve(),
            roots_db=Path(
                os.environ.get("LKT_ROOTS_DB", data_dir / "english-roots.sqlite3")
            ).resolve(),
            affixes_db=Path(
                os.environ.get("LKT_AFFIXES_DB", data_dir / "english-affixes.sqlite3")
            ).resolve(),
            cards_db=Path(
                os.environ.get("LKT_CARDS_DB", data_dir / "cards.sqlite3")
            ).resolve(),
            knowledge_db=Path(
                os.environ.get("LKT_KNOWLEDGE_DB", data_dir / "knowledge.sqlite3")
            ).resolve(),
            graph_db=Path(
                os.environ.get("LKT_GRAPH_DB", data_dir / "knowledge-graph.lbdb")
            ).resolve(),
            freedict_db=Path(
                os.environ.get(
                    "LKT_FREEDICT_DB",
                    data_dir / "lexicons" / "freedict-eng-ara.sqlite3",
                )
            ).resolve(),
            jmdict_db=Path(
                os.environ.get(
                    "LKT_JMDICT_DB",
                    data_dir / "lexicons" / "jmdict.sqlite3",
                )
            ).resolve(),
            llm_url=os.environ.get(
                "LKT_LLM_URL", "http://127.0.0.1:8081/v1/chat/completions"
            ),
            llm_model=os.environ.get("LKT_LLM_MODEL", "Qwen3-4B-Q4_K_M"),
            host=os.environ.get("LKT_HOST", "0.0.0.0"),
            port=int(os.environ.get("LKT_PORT", "8090")),
            request_timeout=int(os.environ.get("LKT_REQUEST_TIMEOUT", "720")),
            max_evidence=int(os.environ.get("LKT_MAX_EVIDENCE", "4")),
        )

    def ensure_runtime_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
