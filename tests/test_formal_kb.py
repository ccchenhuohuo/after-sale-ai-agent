import importlib.util
import json
import sys
from pathlib import Path

from agent_runtime.settings import Settings
from agent_runtime.tools.formal_kb import formal_kb_index_available, search_formal_kb
from agent_runtime.tools.rag import search_official_kb_evidence


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "build_formal_kb_index.py"


def load_builder_module():
    spec = importlib.util.spec_from_file_location("build_formal_kb_index", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_formal_source(source_dir: Path) -> None:
    source_dir.mkdir(parents=True)
    (source_dir / "sources.jsonl").write_text(
        json.dumps(
            {
                "path": "l023-power.md",
                "source_id": "kb-l023-power",
                "title": "L023 不亮排查 SOP",
                "source_type": "official_kb",
                "product_model": "L023",
                "source_url": "https://kb.example.test/l023-power",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    (source_dir / "l023-power.md").write_text(
        "# L023 不亮排查 SOP\n\nL023 不亮时，先确认充电线、插头、接口、按键和指示灯状态。",
        encoding="utf-8",
    )
    (source_dir / "mrd.jsonl").write_text(
        json.dumps(
            {
                "source_id": "mrd-l023",
                "title": "L023 MRD",
                "source_type": "mrd",
                "product_model": "L023",
                "section": "供电设计",
                "text": "L023 供电异常需要结合产品铭牌和客户视频判断。",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


def test_build_formal_kb_index_creates_chunks_and_manifest(tmp_path):
    module = load_builder_module()
    source_dir = tmp_path / "source"
    output_dir = tmp_path / "index"
    write_formal_source(source_dir)

    manifest = module.build_index(source_dir, output_dir, dimension=32)

    chunks = [json.loads(line) for line in (output_dir / "formal_chunks.jsonl").read_text(encoding="utf-8").splitlines()]
    assert manifest["index_type"] == "formal_kb_rag_v1"
    assert manifest["document_count"] == 2
    assert manifest["chunk_count"] >= 2
    assert {chunk["source_type"] for chunk in chunks} == {"official_kb", "mrd"}
    assert all(chunk["evidence_level"] == "formal" for chunk in chunks)
    assert all(chunk["verified"] is True for chunk in chunks)
    assert (output_dir / "embeddings.npy").exists()


def test_search_formal_kb_returns_formal_evidence(tmp_path):
    module = load_builder_module()
    source_dir = tmp_path / "source"
    output_dir = tmp_path / "index"
    write_formal_source(source_dir)
    module.build_index(source_dir, output_dir, dimension=32)
    settings = Settings(
        formal_kb_index_path=str(output_dir),
        formal_kb_provider="local_hash",
        formal_kb_embedding_dimension=32,
        formal_kb_top_k=4,
        formal_kb_top_n=2,
    )

    result = search_formal_kb("L023 不亮 充电线 按键 指示灯", product_model="L023", settings=settings)

    assert "命中正式依据" in result
    assert "kb-l023-power" in result
    assert "L023 不亮排查 SOP" in result
    assert "审核状态：正式依据" in result
    assert formal_kb_index_available(settings) is True


def test_search_official_kb_evidence_parses_formal_hits(tmp_path):
    module = load_builder_module()
    source_dir = tmp_path / "source"
    output_dir = tmp_path / "index"
    write_formal_source(source_dir)
    module.build_index(source_dir, output_dir, dimension=32)
    settings = Settings(
        formal_kb_index_path=str(output_dir),
        formal_kb_provider="local_hash",
        formal_kb_embedding_dimension=32,
        formal_kb_top_k=4,
        formal_kb_top_n=2,
    )

    evidence = search_official_kb_evidence("L023 不亮 充电线", product_model="L023", settings=settings)

    assert evidence[0].status == "hit"
    assert evidence[0].evidence_level == "formal"
    assert evidence[0].verified is True
    assert evidence[0].title
    assert evidence[0].source_type in {"official_kb", "mrd"}


def test_search_formal_kb_returns_missing_when_index_absent(tmp_path):
    settings = Settings(formal_kb_index_path=str(tmp_path / "missing-index"))

    result = search_formal_kb("L023 不亮", product_model="L023", settings=settings)

    assert result.startswith("未查询到可信正式依据")
    assert formal_kb_index_available(settings) is False
