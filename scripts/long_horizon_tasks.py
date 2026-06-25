"""Sample long-horizon tasks for A/B subagent experiments (skill lookup vs naive execution).

These are planning/recall workloads. Full execution may take minutes–hours; the harness
times *search_strategies* (setup 1) and a no-search baseline stub (setup 2 planning only).
"""
from __future__ import annotations

from typing import Any

# Each task: id, title, skill query (task+scaffold), optional efficient_path hint for judges,
# naive_path cost note, subagent_persona suggestion.
LONG_HORIZON_TASKS: list[dict[str, Any]] = [
    {
        "id": "ray_dev_setup",
        "title": "Prepare ray-project/ray repo from scratch for development",
        "persona": "v9-dietcoke",
        "task_query": (
            "Prepare ray-project/ray repository from scratch for local Python development "
            "and contribution; build Ray Python from source if needed"
        ),
        "scaffold_query": (
            "Linux; Python 3.10+; git clone ray-project/ray; Bazel optional; "
            "docs.ray.io contribute development; pip/wheels; 8 CPU machine"
        ),
        "efficient_path": (
            "Install nightly/prebuilt wheels per Ray contribute docs "
            "(https://docs.ray.io/en/latest/ray-contribute/development.html#building-ray-python) "
            "instead of full Bazel build (~minutes vs >1h on 8 CPUs)"
        ),
        "naive_path": "Clone repo and run full Bazel build of Ray Python from source (often >1 hour, 8 CPUs)",
        "horizon": "hours_if_naive",
        "domain_tags": ["ray", "bazel", "python", "dev-setup"],
    },
    {
        "id": "etl_tb_parquet_dask_slurm",
        "title": "Multi-TB parquet/jsonl ETL via Dask or SLURM",
        "persona": "v9-pizzaparty",
        "task_query": (
            "Design and run a massive ETL pipeline converting multi-TB parquet and jsonl "
            "datasets into normalized training shards using Dask distributed or SLURM batch "
            "jobs with checkpointing, schema validation, and cost/runtime predictability"
        ),
        "scaffold_query": (
            "Linux HPC; SLURM; Dask/distributed optional; PyArrow parquet; jsonl; "
            "Python 3.11; large disk/network; agent coding environment"
        ),
        "efficient_path": "Reuse prior segmented ETL/checkpoint/resume strategy if in experience memory",
        "naive_path": "Design pipeline from scratch without prior agent trajectories",
        "horizon": "hours_to_days",
        "domain_tags": ["etl", "dask", "slurm", "parquet", "jsonl"],
    },
    {
        "id": "hf_dataset_resumable_download",
        "title": "Resumable multi-hundred-GB HuggingFace dataset ingest",
        "persona": "v9-bottlerocket",
        "task_query": (
            "Download and prepare a multi-hundred-GB HuggingFace dataset for training with "
            "resumable transfers, disk budget checks, and failure_waste minimization"
        ),
        "scaffold_query": (
            "Python; datasets/huggingface_hub; limited disk; intermittent network; "
            "agent loop must not block forever on download"
        ),
        "efficient_path": "Resume/partial download strategies from prior trajectories",
        "naive_path": "Full re-download on any failure",
        "horizon": "hours",
        "domain_tags": ["huggingface", "dataset", "download"],
    },
    {
        "id": "qdrant_reindex_large_collection",
        "title": "Re-embed and reindex large trajectory/experience collection",
        "persona": "v9-dietcoke",
        "task_query": (
            "Re-embed and reindex a large Qdrant experience trajectory collection after "
            "changing embed model or dimension with minimal downtime and idempotent upserts"
        ),
        "scaffold_query": (
            "Yggdrasil; Qdrant; SQLite canonical store; OpenAI-compat embed endpoint; "
            "named vectors task+scaffold; batch import"
        ),
        "efficient_path": "Use stable mongo-session/seg ids and experience-grade importer path",
        "naive_path": "Delete collection and re-import everything without idempotency",
        "horizon": "tens_of_minutes",
        "domain_tags": ["qdrant", "yggdrasil", "embed", "reindex"],
    },
    {
        "id": "cuda_oom_training_fix",
        "title": "Unblock CUDA OOM training loop on 24GB GPU",
        "persona": "v9-pizzaparty",
        "task_query": (
            "Fix CUDA OOM in training loop on RTX 4090 24GB; reduce memory footprint "
            "with batch size, gradient checkpointing, or CPU offload"
        ),
        "scaffold_query": "PyTorch; CUDA; 24GB VRAM; agent coding; tests optional",
        "efficient_path": "Prior fail/partial trajectories with waste_seconds and successful configs",
        "naive_path": "Trial-and-error without recalling prior OOM attempts",
        "horizon": "hours",
        "domain_tags": ["cuda", "pytorch", "oom", "training"],
    },
    {
        "id": "monorepo_ci_green_from_scratch",
        "title": "Bring large monorepo CI green from cold clone",
        "persona": "v9-bottlerocket",
        "task_query": (
            "Cold clone a large monorepo and get primary CI / unit tests green with "
            "correct toolchain versions and minimal redundant builds"
        ),
        "scaffold_query": "Git; Linux CI; pytest or bazel/npm; flaky tests; agent coding",
        "efficient_path": "Prior scaffold with pinned toolchains and known flaky skips",
        "naive_path": "Full install + full test matrix without prior agent notes",
        "horizon": "hours",
        "domain_tags": ["ci", "monorepo", "tests"],
    },
]
