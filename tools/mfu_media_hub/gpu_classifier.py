"""Standalone CUDA classifier; invoked by Media Hub's isolated Python runtime.

Only the public model weights are downloaded. Image pixels stay on this PC.
Output is an unconfirmed suggestion, never a saved image-viewer tag.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


MODEL_ID = "openai/clip-vit-base-patch32"
PROMPTS = {
    "lolita": "A fashion photograph of a person wearing Japanese Lolita fashion, an ornate Victorian-inspired dress with lace and ruffles.",
    "jirai": "A Japanese jirai-kei fashion outfit: black gothic clothes, short skirt, twin tails, ribbon accessories, knee socks and platform shoes.",
    "casual": "An ordinary everyday outfit: a simple shirt, jeans, skirt or sweater, with minimal decorative accessories.",
    "disney": "A person wearing a Disney character cosplay costume at a Disney theme park, with a princess dress or character outfit.",
}
DETAILS = {
    "lolita": [("ゴスロリ", "Gothic Lolita fashion with a dark ornate dress"),
               ("甘ロリ", "Sweet Lolita fashion with a pastel ruffled dress"),
               ("クラロリ", "Classic Lolita fashion with a Victorian-inspired dress")],
    "jirai": [("地雷系", "Japanese jirai-kei fashion with dark cute styling"),
              ("量産型", "Japanese ryousangata fashion with pink ribbons and frills"),
              ("サブカル", "Japanese subculture fashion with alternative streetwear")],
    "casual": [("カジュアル", "casual everyday street clothes"),
               ("フォーマル", "formal smart clothes"),
               ("ディズニー", "a Disney character costume or themed outfit")],
}


def classify(image_path: Path) -> dict:
    import torch
    from PIL import Image
    from transformers import CLIPModel, CLIPProcessor

    if not torch.cuda.is_available():
        raise RuntimeError("NVIDIA GPUを使用できません。CPUへの自動切替は行いません。")
    device = "cuda:0"
    model = CLIPModel.from_pretrained(MODEL_ID, use_safetensors=True).to(device).eval()
    processor = CLIPProcessor.from_pretrained(MODEL_ID)
    with Image.open(image_path) as source:
        image = source.convert("RGB")
    prompts = list(PROMPTS.values())
    inputs = processor(text=prompts, images=image, return_tensors="pt", padding=True)
    inputs = {key: value.to(device) for key, value in inputs.items()}
    with torch.inference_mode():
        logits = model(**inputs).logits_per_image[0]
        probabilities = logits.softmax(dim=0).cpu().tolist()
    raw_scores = dict(zip(PROMPTS, probabilities))
    scores = {key: raw_scores[key] for key in ("lolita", "jirai")}
    scores["casual"] = raw_scores["casual"] + raw_scores["disney"]
    major = max(scores, key=scores.get)

    # Fine-grained fashion tags are especially uncertain with generic CLIP.
    # Propose just the strongest middle tag for explicit human review.
    detail_labels = DETAILS[major]
    detail_inputs = processor(
        text=[f"A fashion photograph of a person wearing {description}." for _, description in detail_labels],
        images=image, return_tensors="pt", padding=True,
    )
    detail_inputs = {key: value.to(device) for key, value in detail_inputs.items()}
    with torch.inference_mode():
        detail_scores = model(**detail_inputs).logits_per_image[0].softmax(dim=0).cpu().tolist()
    best = max(range(len(detail_scores)), key=detail_scores.__getitem__)
    middle = [{"name": detail_labels[best][0], "smallTags": []}] if detail_scores[best] >= 0.55 else []
    if major == "casual" and raw_scores["disney"] > raw_scores["casual"]:
        middle = [{"name": "ディズニー", "smallTags": []}]
    return {
        "assignment": {"major": major, "middleTags": middle},
        "scores": scores, "model": MODEL_ID,
    }


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2:
            raise ValueError("画像パスを1つ指定してください。")
        result = classify(Path(sys.argv[1]))
        print(json.dumps({"ok": True, "suggestion": result}, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)[:300]}, ensure_ascii=False))
        raise SystemExit(1)
