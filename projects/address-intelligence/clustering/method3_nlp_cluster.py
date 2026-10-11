import os
import re

import numpy as np
import pandas as pd
import requests


INPUT_FILE = "测试_cleaned.xlsx"
OUTPUT_FILE = "测试_cleaned_方法三聚类.xlsx"
EMBEDDING_URL = os.environ['EMBEDDING_API_URL']
EMBEDDING_MODEL = "Qwen/Qwen3-Embedding-8B"
TOP_K = 10
EMBEDDING_BATCH_SIZE = 64
LEIDEN_SEED = 42

CITY_RE = re.compile(r"([\u4e00-\u9fa5]{2,12}市)")
DISTRICT_RE = re.compile(r"([\u4e00-\u9fa5]{1,12}(?:区|县|市))")
ROAD_RE = re.compile(r"([\u4e00-\u9fa5A-Za-z0-9]{2,20}(?:大道[东西南北]?|[东西南北]路|路|街))")
ROAD_NO_RE = re.compile(r"([0-9０-９]+(?:-[0-9０-９]+)?号)")


def parse_address_anchors(address: str) -> dict[str, str]:
    text = "" if address is None else str(address)

    city_match = CITY_RE.search(text)
    city = city_match.group(1) if city_match else ""
    city_tail = text[city_match.end() :] if city_match else text

    district_match = DISTRICT_RE.search(city_tail)
    district = district_match.group(1) if district_match else ""
    district_tail = city_tail[district_match.end() :] if district_match else city_tail

    road_match = ROAD_RE.search(district_tail)
    road = road_match.group(1) if road_match else ""
    road_tail = district_tail[road_match.end() :] if road_match else district_tail

    road_no_match = ROAD_NO_RE.search(road_tail)
    road_no = road_no_match.group(1) if road_no_match else ""

    return {
        "city": city,
        "district": district,
        "road": road,
        "road_no": road_no,
    }


def fetch_embeddings(texts: list[str], api_key: str, model: str, batch_size: int = EMBEDDING_BATCH_SIZE) -> np.ndarray:
    vectors = []
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        batch_no = start // batch_size + 1
        batch_total = (len(texts) + batch_size - 1) // batch_size
        print(f"embedding {batch_no}/{batch_total}")

        response = requests.post(
            EMBEDDING_URL,
            headers=headers,
            json={
                "model": model,
                "input": batch,
                "encoding_format": "float",
            },
            timeout=60,
        )
        response.raise_for_status()

        data = response.json().get("data", [])
        if len(data) != len(batch):
            raise ValueError("Embedding response length does not match request")

        for item in sorted(data, key=lambda x: x.get("index", 0)):
            embedding = item.get("embedding")
            if not embedding:
                raise ValueError("Embedding response missing vector")
            vectors.append(embedding)

    return np.asarray(vectors, dtype=float)


def detect_communities(embeddings: np.ndarray, top_k: int = TOP_K, seed: int = LEIDEN_SEED) -> list[int]:
    try:
        import igraph as ig
        import leidenalg
    except ImportError as exc:
        raise RuntimeError("Missing required packages: python-igraph, leidenalg") from exc

    if len(embeddings) == 0:
        return []
    if len(embeddings) == 1:
        return [0]

    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    if np.any(norms == 0):
        raise ValueError("Embedding contains zero vector")

    normalized = embeddings / norms
    similarity = normalized @ normalized.T
    np.fill_diagonal(similarity, -np.inf)

    k = min(top_k, len(embeddings) - 1)
    edge_map: dict[tuple[int, int], float] = {}

    for i in range(len(embeddings)):
        neighbors = np.argpartition(similarity[i], -k)[-k:]
        for j in neighbors:
            left, right = sorted((i, int(j)))
            if left == right:
                continue
            edge_map[(left, right)] = max(float(similarity[i, j]), edge_map.get((left, right), -np.inf))

    edges = [(left, right, weight) for (left, right), weight in edge_map.items()]
    graph = ig.Graph(n=len(embeddings), edges=[(left, right) for left, right, _ in edges], directed=False)
    if edges:
        graph.es["weight"] = [weight for _, _, weight in edges]

    partition = leidenalg.find_partition(
        graph,
        leidenalg.ModularityVertexPartition,
        weights=graph.es["weight"] if edges else None,
        seed=seed,
    )
    return list(partition.membership)


def process_workbook(
    input_path: str = INPUT_FILE,
    output_path: str = OUTPUT_FILE,
    top_k: int = TOP_K,
) -> None:
    source_df = pd.read_excel(input_path)
    missing = {"company_name", "clean_address"} - set(source_df.columns)
    if missing:
        raise ValueError(f"Missing required columns: {', '.join(sorted(missing))}")

    anchors = source_df["clean_address"].map(parse_address_anchors).apply(pd.Series)
    result = source_df.join(anchors)
    result["embedding_text"] = (
        "城市:" + result["city"].fillna("")
        + " 区县:" + result["district"].fillna("")
        + " 道路:" + result["road"].fillna("")
        + " 门牌:" + result["road_no"].fillna("")
    )
    result["is_unresolved"] = result[["city", "district", "road", "road_no"]].fillna("").eq("").all(axis=1)

    resolved = result.loc[~result["is_unresolved"]].copy()
    unique_texts = resolved["embedding_text"].drop_duplicates().tolist()

    text_clusters = pd.DataFrame(columns=["embedding_text", "community_id", "is_noise"])
    summary = pd.DataFrame(columns=["cluster_id", "cluster_company_count", "sample_address"])

    if unique_texts:
        api_key = os.getenv("SILICONFLOW_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("Missing required environment variable: SILICONFLOW_API_KEY")

        model = os.getenv("SILICONFLOW_EMBEDDING_MODEL", "").strip() or EMBEDDING_MODEL
        print(f"model {model}")

        embeddings = fetch_embeddings(unique_texts, api_key=api_key, model=model)
        memberships = detect_communities(embeddings, top_k=top_k)

        text_clusters = pd.DataFrame(
            {
                "embedding_text": unique_texts,
                "community_id": memberships,
            }
        )
        text_clusters["community_size"] = text_clusters.groupby("community_id")["embedding_text"].transform("size")
        text_clusters["is_noise"] = text_clusters["community_size"] == 1

        clustered_texts = text_clusters.loc[~text_clusters["is_noise"], ["embedding_text", "community_id"]]
        if not clustered_texts.empty:
            summary = (
                resolved.merge(clustered_texts, on="embedding_text", how="inner")
                .groupby("community_id", as_index=False)
                .agg(
                    cluster_company_count=("company_name", "size"),
                    sample_address=("clean_address", "min"),
                )
                .sort_values(
                    by=["cluster_company_count", "sample_address"],
                    ascending=[False, True],
                    kind="mergesort",
                )
                .reset_index(drop=True)
            )
            summary["cluster_id"] = pd.Series(range(len(summary)), dtype="Int64")
            summary = summary[["cluster_id", "cluster_company_count", "sample_address", "community_id"]]
            text_clusters = text_clusters.merge(
                summary[["community_id", "cluster_id", "cluster_company_count"]],
                on="community_id",
                how="left",
            )
        else:
            text_clusters["cluster_id"] = pd.Series(pd.array([pd.NA] * len(text_clusters), dtype="Int64"))
            text_clusters["cluster_company_count"] = pd.Series(
                pd.array([pd.NA] * len(text_clusters), dtype="Int64")
            )

    result["cluster_id"] = pd.Series(pd.array([pd.NA] * len(result), dtype="Int64"), index=result.index)
    result["cluster_company_count"] = pd.Series(pd.array([pd.NA] * len(result), dtype="Int64"), index=result.index)
    result["is_noise"] = False

    if not text_clusters.empty:
        mapping = text_clusters.set_index("embedding_text")
        resolved_mask = ~result["is_unresolved"]
        resolved_text = result.loc[resolved_mask, "embedding_text"]
        result.loc[resolved_mask, "cluster_id"] = resolved_text.map(mapping["cluster_id"]).astype("Int64")
        result.loc[resolved_mask, "cluster_company_count"] = resolved_text.map(mapping["cluster_company_count"]).astype(
            "Int64"
        )
        result.loc[resolved_mask, "is_noise"] = resolved_text.map(mapping["is_noise"]).fillna(False).astype(bool)

    detail_columns = list(source_df.columns) + ["city", "district", "road", "road_no"]
    clustered_result = result.loc[result["cluster_id"].notna(), detail_columns + ["cluster_id", "cluster_company_count"]]
    noise_result = result.loc[(~result["is_unresolved"]) & result["cluster_id"].isna(), detail_columns]
    unresolved_result = result.loc[result["is_unresolved"], detail_columns]
    summary_result = summary[["cluster_id", "cluster_company_count", "sample_address"]] if not summary.empty else summary

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        clustered_result.to_excel(writer, sheet_name="聚类结果", index=False)
        noise_result.to_excel(writer, sheet_name="噪声点", index=False)
        unresolved_result.to_excel(writer, sheet_name="未解析", index=False)
        summary_result.to_excel(writer, sheet_name="簇汇总", index=False)

    print(f"已输出: {output_path}")


if __name__ == "__main__":
    process_workbook()
