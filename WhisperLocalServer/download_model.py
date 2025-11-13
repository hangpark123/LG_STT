import argparse
import json
import os
from pathlib import Path

from huggingface_hub import snapshot_download

SIZES = {
    "tiny": "Systran/faster-whisper-tiny",
    "base": "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small",
    "medium": "Systran/faster-whisper-medium",
    "large-v2": "Systran/faster-whisper-large-v2",
    "large-v3": "Systran/faster-whisper-large-v3",
}


def main():
    p = argparse.ArgumentParser(description="Download faster-whisper model to HF cache")
    p.add_argument("--model", default=os.getenv("WHISPER_MODEL", "small"), help="tiny|base|small|medium|large-v2|large-v3 or repo id")
    p.add_argument("--hf-home", default=os.getenv("HF_HOME"), help="HF cache root (e.g., C:/Users/<me>/.cache/huggingface)")
    p.add_argument("--revision", default="main")
    args = p.parse_args()

    if args.hf_home:
        os.environ["HF_HOME"] = args.hf_home
        Path(args.hf_home).mkdir(parents=True, exist_ok=True)

    repo = SIZES.get(args.model.lower(), args.model)

    path = snapshot_download(repo_id=repo, revision=args.revision)

    out = {
        "repo": repo,
        "revision": args.revision,
        "snapshot_path": path,
        "hf_home": os.getenv("HF_HOME"),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
