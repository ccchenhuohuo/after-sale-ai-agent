import argparse
import json
import os
import re
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

import httpx
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_DIR = ROOT / "data" / "formal_kb" / "source"
DEFAULT_OUTPUT_DIR = ROOT / "data" / "formal_kb" / "index" / "latest"
DEFAULT_DIMENSION = 768
DEFAULT_BAILIAN_EMBEDDING_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_BAILIAN_EMBEDDING_MODEL = "text-embedding-v4"
SUPPORTED_TEXT_SUFFIXES = {".md", ".txt"}
JSONL_MANIFEST_NAME = "sources.jsonl"
SOURCE_TYPES = {"official_kb", "mrd", "manual", "policy"}
SKU_RE = re.compile(r"\b[A-Z]{1,4}\d{2,5}[A-Z0-9-]*\b")


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def env_value(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value:
        return value
    env_path = ROOT / ".env"
    if not env_path.exists():
        return default
    for line in env_path.read_text(errors="ignore").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        if key == name:
            return raw_value.strip()
    return default


def tokenize(text: str) -> list[str]:
    normalized = clean_text(text).upper()
    tokens: list[str] = []
    tokens.extend(SKU_RE.findall(normalized))
    tokens.extend(re.findall(r"[A-Z0-9-]{2,}", normalized))
    chinese = re.findall(r"[\u4e00-\u9fff]", normalized)
    tokens.extend(chinese)
    tokens.extend("".join(pair) for pair in zip(chinese, chinese[1:]))
    return [token for token in tokens if token]


def hashed_embedding(text: str, dimension: int = DEFAULT_DIMENSION) -> np.ndarray:
    vector = np.zeros(dimension, dtype=np.float32)
    for token, count in Counter(tokenize(text)).items():
        index = int.from_bytes(sha256(token.encode("utf-8")).digest()[:8], "big") % dimension
        vector[index] += float(count)
    norm = np.linalg.norm(vector)
    if norm:
        vector /= norm
    return vector


def short_hash(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()[:12]


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                payload = json.loads(line)
                if isinstance(payload, dict):
                    rows.append(payload)
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )


def load_manifest(source_dir: Path) -> dict[str, dict[str, Any]]:
    manifest: dict[str, dict[str, Any]] = {}
    for row in read_jsonl(source_dir / JSONL_MANIFEST_NAME):
        keys = [
            clean_text(row.get("path")),
            clean_text(row.get("filename")),
            clean_text(row.get("source_id")),
        ]
        for key in keys:
            if key:
                manifest[key] = row
    return manifest


def first_heading(text: str) -> str:
    for line in text.splitlines():
        match = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", line)
        if match:
            return clean_text(match.group(1))
    return ""


def normalize_source_type(value: Any) -> str:
    source_type = clean_text(value).lower()
    return source_type if source_type in SOURCE_TYPES else "official_kb"


def row_text(row: dict[str, Any]) -> str:
    return clean_text(row.get("text") or row.get("content") or row.get("body") or row.get("snippet"))


def text_documents(source_dir: Path) -> list[dict[str, Any]]:
    manifest = load_manifest(source_dir)
    documents: list[dict[str, Any]] = []
    for path in sorted(source_dir.rglob("*")):
        if not path.is_file() or path.name == JSONL_MANIFEST_NAME:
            continue
        if path.suffix.lower() in SUPPORTED_TEXT_SUFFIXES:
            text = path.read_text(encoding="utf-8", errors="ignore")
            rel_path = path.relative_to(source_dir).as_posix()
            meta = manifest.get(rel_path) or manifest.get(path.name) or manifest.get(path.stem) or {}
            title = clean_text(meta.get("title")) or first_heading(text) or path.stem
            source_id = clean_text(meta.get("source_id")) or f"{path.stem}-{short_hash(rel_path)}"
            documents.append(_document_from_metadata(source_id, title, text, rel_path, meta))
        elif path.suffix.lower() == ".jsonl":
            for index, row in enumerate(read_jsonl(path), start=1):
                text = row_text(row)
                if not text:
                    continue
                rel_path = path.relative_to(source_dir).as_posix()
                title = clean_text(row.get("title")) or f"{path.stem} #{index}"
                source_id = clean_text(row.get("source_id")) or f"{path.stem}-{index}-{short_hash(text)}"
                documents.append(_document_from_metadata(source_id, title, text, rel_path, row))
    return documents


def _document_from_metadata(source_id: str, title: str, text: str, rel_path: str, meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "title": title,
        "source_type": normalize_source_type(meta.get("source_type")),
        "product_model": clean_text(meta.get("product_model") or meta.get("sku")),
        "version": clean_text(meta.get("version")),
        "updated_at": clean_text(meta.get("updated_at")),
        "source_url": clean_text(meta.get("source_url") or meta.get("url")),
        "section": clean_text(meta.get("section")),
        "allow_visible_title": bool(meta.get("allow_visible_title", True)),
        "source_path": rel_path,
        "text": text,
    }


def section_chunks(document: dict[str, Any], max_chars: int) -> list[dict[str, Any]]:
    text = document["text"].replace("\r", "\n")
    sections: list[tuple[str, list[str]]] = []
    current_title = document.get("section") or document["title"]
    current_lines: list[str] = []
    for line in text.splitlines():
        heading = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", line)
        if heading and current_lines:
            sections.append((current_title, current_lines))
            current_title = clean_text(heading.group(1))
            current_lines = []
        elif heading:
            current_title = clean_text(heading.group(1))
        else:
            current_lines.append(line)
    if current_lines:
        sections.append((current_title, current_lines))
    if not sections:
        sections = [(document["title"], [text])]

    chunks: list[dict[str, Any]] = []
    for section, lines in sections:
        paragraphs = [clean_text(part) for part in "\n".join(lines).split("\n\n") if clean_text(part)]
        buffer: list[str] = []
        for paragraph in paragraphs or [clean_text("\n".join(lines))]:
            candidate = "\n".join([*buffer, paragraph]).strip()
            if buffer and len(candidate) > max_chars:
                chunks.append(_chunk(document, section, len(chunks) + 1, "\n".join(buffer)))
                buffer = [paragraph]
            else:
                buffer.append(paragraph)
        if buffer:
            chunks.append(_chunk(document, section, len(chunks) + 1, "\n".join(buffer)))
    return chunks


def _chunk(document: dict[str, Any], section: str, index: int, text: str) -> dict[str, Any]:
    return {
        "chunk_id": f"{document['source_id']}::chunk_{index}",
        "source_id": document["source_id"],
        "source_type": document["source_type"],
        "evidence_level": "formal",
        "verified": True,
        "authority": "formal",
        "can_be_formal_evidence": True,
        "title": document["title"],
        "section": section,
        "product_model": document["product_model"],
        "sku": document["product_model"],
        "version": document["version"],
        "updated_at": document["updated_at"],
        "source_url": document["source_url"],
        "allow_visible_title": document["allow_visible_title"],
        "source_path": document["source_path"],
        "text": "\n".join(
            [
                f"标题：{document['title']}",
                f"资料类型：{document['source_type']}",
                f"章节：{section}",
                f"SKU/型号：{document['product_model'] or '未限定'}",
                clean_text(text),
            ]
        ).strip(),
    }


def auth_headers(auth_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {auth_token}"} if auth_token else {}


def endpoint(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def extract_embedding_payload(payload: dict[str, Any]) -> list[list[float]]:
    if isinstance(payload.get("embeddings"), list):
        return payload["embeddings"]
    data = payload.get("data")
    if isinstance(data, list):
        embeddings = []
        for item in data:
            if isinstance(item, dict) and isinstance(item.get("embedding"), list):
                embeddings.append(item["embedding"])
        if embeddings:
            return embeddings
    return []


def bailian_embeddings(
    base_url: str,
    api_key: str,
    model: str,
    texts: list[str],
    *,
    batch_size: int = 8,
    text_max_chars: int = 1600,
    timeout: float = 120.0,
) -> np.ndarray:
    if not api_key:
        raise RuntimeError("BAILIAN_API_KEY or DASHSCOPE_API_KEY is required for provider=bailian.")
    vectors: list[list[float]] = []
    prepared_texts = [clean_text(text)[:text_max_chars] if text_max_chars > 0 else text for text in texts]
    for start in range(0, len(prepared_texts), batch_size):
        batch = prepared_texts[start : start + batch_size]
        print(f"formal kb embedding batch {start + 1}-{start + len(batch)} / {len(prepared_texts)}", flush=True)
        response = httpx.post(
            endpoint(base_url, "embeddings"),
            json={"model": model, "input": batch},
            headers=auth_headers(api_key),
            timeout=timeout,
        )
        response.raise_for_status()
        embeddings = extract_embedding_payload(response.json())
        if not isinstance(embeddings, list) or len(embeddings) != len(batch):
            raise RuntimeError(f"Bailian embedding returned invalid payload for batch starting at {start}.")
        vectors.extend(embeddings)
    array = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(array, axis=1, keepdims=True)
    norms[norms == 0] = 1
    return array / norms


def build_index(
    source_dir: Path,
    output_dir: Path,
    dimension: int = DEFAULT_DIMENSION,
    provider: str = "local_hash",
    embedding_batch_size: int = 8,
    embedding_text_max_chars: int = 1600,
    bailian_api_key: str = "",
    bailian_embedding_base_url: str = DEFAULT_BAILIAN_EMBEDDING_BASE_URL,
    embedding_model: str = DEFAULT_BAILIAN_EMBEDDING_MODEL,
    max_chunk_chars: int = 1200,
) -> dict[str, Any]:
    documents = text_documents(source_dir)
    chunks: list[dict[str, Any]] = []
    for document in documents:
        chunks.extend(section_chunks(document, max_chunk_chars))

    output_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(output_dir / "formal_sources.jsonl", documents)
    write_jsonl(output_dir / "formal_chunks.jsonl", chunks)

    embedding_backend = "local_hashed_formal_kb"
    if provider == "bailian" and chunks:
        embeddings = bailian_embeddings(
            bailian_embedding_base_url,
            bailian_api_key,
            embedding_model,
            [chunk["text"] for chunk in chunks],
            batch_size=embedding_batch_size,
            text_max_chars=embedding_text_max_chars,
        )
        embedding_backend = "bailian"
    else:
        embeddings = (
            np.vstack([hashed_embedding(chunk["text"], dimension) for chunk in chunks])
            if chunks
            else np.zeros((0, dimension), dtype=np.float32)
        )
    np.save(output_dir / "embeddings.npy", embeddings)

    manifest = {
        "index_type": "formal_kb_rag_v1",
        "source_dir": str(source_dir),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "document_count": len(documents),
        "chunk_count": len(chunks),
        "source_types": sorted({chunk.get("source_type") for chunk in chunks if chunk.get("source_type")}),
        "embedding_dimension": int(embeddings.shape[1]) if embeddings.ndim == 2 else dimension,
        "embedding_backend": embedding_backend,
        "formal_kb_provider": provider,
        "embedding_model": embedding_model if provider == "bailian" else None,
        "bailian_embedding_base_url": bailian_embedding_base_url if provider == "bailian" else None,
        "embedding_text_max_chars": embedding_text_max_chars if provider == "bailian" else None,
        "evidence_level": "formal",
        "verified": True,
        "authority": "formal",
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build formal KB/MRD/manual/policy RAG index from local text exports.")
    parser.add_argument("--source-dir", default=str(DEFAULT_SOURCE_DIR), help="Directory containing md/txt/jsonl formal source exports.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Formal KB index output directory.")
    parser.add_argument("--dimension", type=int, default=DEFAULT_DIMENSION, help="Hashed embedding dimension.")
    parser.add_argument("--provider", choices=["local_hash", "bailian"], default="local_hash")
    parser.add_argument("--embedding-batch-size", type=int, default=8, help="Remote embedding batch size.")
    parser.add_argument("--embedding-text-max-chars", type=int, default=1600, help="Maximum chars sent per chunk to remote embedding.")
    parser.add_argument("--max-chunk-chars", type=int, default=1200, help="Approximate max chars per source chunk.")
    parser.add_argument("--bailian-api-key", default=env_value("BAILIAN_API_KEY") or env_value("DASHSCOPE_API_KEY"))
    parser.add_argument("--bailian-embedding-base-url", default=env_value("BAILIAN_EMBEDDING_BASE_URL", DEFAULT_BAILIAN_EMBEDDING_BASE_URL))
    parser.add_argument("--embedding-model", default=env_value("FORMAL_KB_EMBEDDING_MODEL", DEFAULT_BAILIAN_EMBEDDING_MODEL))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    manifest = build_index(
        Path(args.source_dir),
        Path(args.output_dir),
        args.dimension,
        provider=args.provider,
        embedding_batch_size=args.embedding_batch_size,
        embedding_text_max_chars=args.embedding_text_max_chars,
        bailian_api_key=args.bailian_api_key,
        bailian_embedding_base_url=args.bailian_embedding_base_url,
        embedding_model=args.embedding_model,
        max_chunk_chars=args.max_chunk_chars,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
