"""Train the LR disclosure classifier from labeled JSONL examples.

Usage:
    python scripts/train_disclosure_classifier.py \
        --examples tests/disclosure_examples.jsonl \
        --output config/disclosure_model.npz

Pure numpy implementation — no sklearn required.
Trains on one partition and evaluates held-out examples with runtime decisions.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

# We need to import the feature extraction logic
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_router.disclosure_classifier import DisclosureClassifier
from ai_router.identity import _IDENTITY_DISCLOSURE_PATTERNS, _IDENTITY_FOLLOWUP_PATTERNS


def load_examples(path: Path) -> list[dict]:
    """Load JSONL examples: {"text": "...", "label": 0|1, "context": "..."}"""
    examples = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            examples.append(obj)
    return examples


def feature_options(example: dict) -> dict:
    return {
        "identity_context": bool(example.get("identity_context", example.get("context", ""))),
        "has_tool_choice": bool(example.get("has_tool_choice", False)),
        "has_response_format": bool(example.get("has_response_format", False)),
        "patterns": _IDENTITY_DISCLOSURE_PATTERNS,
        "followup_patterns": _IDENTITY_FOLLOWUP_PATTERNS,
    }


def split_examples(
    examples: list[dict], *, validation_fraction: float = 0.2, seed: int = 42,
) -> tuple[list[dict], list[dict]]:
    """Deduplicate equivalent inputs, then split each label reproducibly."""
    if not 0 < validation_fraction < 1:
        raise ValueError("validation_fraction must be between 0 and 1")
    unique = {}
    for example in examples:
        if not isinstance(example.get("text"), str) or not example["text"].strip():
            raise ValueError("Each example must have non-empty text")
        if type(example.get("label")) is not int or example["label"] not in (0, 1):
            raise ValueError("Each example label must be 0 or 1")
        options = feature_options(example)
        key = (example["text"].strip(), options["identity_context"],
               options["has_tool_choice"], options["has_response_format"])
        if key in unique and unique[key]["label"] != example["label"]:
            raise ValueError("Equivalent inputs have conflicting labels")
        unique.setdefault(key, example)

    rng = np.random.default_rng(seed)
    train, validation = [], []
    for label in (0, 1):
        rows = [unique[key] for key in sorted(unique) if unique[key]["label"] == label]
        if len(rows) < 2:
            raise ValueError("Need at least two unique examples per label for held-out evaluation")
        rng.shuffle(rows)
        count = min(len(rows) - 1, max(1, round(len(rows) * validation_fraction)))
        validation.extend(rows[:count])
        train.extend(rows[count:])
    rng.shuffle(train)
    rng.shuffle(validation)
    return train, validation


def build_dataset(examples: list[dict]) -> tuple[np.ndarray, np.ndarray]:
    """Build feature matrix X and label vector y."""
    clf = DisclosureClassifier()  # no model loaded, just for feature extraction
    X_list = []
    y_list = []
    for ex in examples:
        text = ex["text"]
        label = int(ex["label"])
        features = clf.extract_features(text, **feature_options(ex))
        X_list.append(features.to_vector())
        y_list.append(label)
    return np.array(X_list, dtype=np.float32), np.array(y_list, dtype=np.int32)


def train_logistic_regression(
    X: np.ndarray,
    y: np.ndarray,
    *,
    lr: float = 0.1,
    epochs: int = 500,
    l2: float = 0.01,
) -> tuple[np.ndarray, float]:
    """Train binary LR via mini-batch gradient descent. Pure numpy."""
    n_samples, n_features = X.shape
    weights = np.zeros(n_features, dtype=np.float64)
    bias = 0.0

    for epoch in range(epochs):
        # Forward pass
        logits = X @ weights + bias
        probs = 1.0 / (1.0 + np.exp(-np.clip(logits, -500, 500)))

        # Gradients
        error = probs - y.astype(np.float64)
        dw = (X.T @ error) / n_samples + l2 * weights
        db = np.mean(error)

        # Update
        weights -= lr * dw
        bias -= lr * db

    return weights.astype(np.float32), float(bias)


def evaluate(
    examples: list[dict],
    weights: np.ndarray,
    bias: float,
) -> dict:
    """Evaluate the same guards, thresholds and fallback used at runtime."""
    if not examples:
        raise ValueError("Evaluation requires held-out examples")
    clf = DisclosureClassifier()
    clf._weights = weights.astype(np.float32)
    clf._bias = float(bias)
    y = np.array([ex["label"] for ex in examples], dtype=np.int32)
    preds = np.array([
        clf.classify(ex["text"], **feature_options(ex))[0] for ex in examples
    ], dtype=np.int32)

    tp = int(np.sum((preds == 1) & (y == 1)))
    fp = int(np.sum((preds == 1) & (y == 0)))
    fn = int(np.sum((preds == 0) & (y == 1)))
    tn = int(np.sum((preds == 0) & (y == 0)))

    accuracy = (tp + tn) / max(n := len(y), 1)
    precision = tp / max(tp + fp, 1)
    recall = tp / max(tp + fn, 1)
    f1 = 2 * precision * recall / max(precision + recall, 1e-9)

    return {
        "accuracy": round(accuracy, 4),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Train disclosure LR classifier")
    parser.add_argument("--examples", type=Path, required=True, help="JSONL file")
    parser.add_argument("--output", type=Path, required=True, help="Output .npz path")
    parser.add_argument("--lr", type=float, default=0.1)
    parser.add_argument("--epochs", type=int, default=500)
    parser.add_argument("--l2", type=float, default=0.01)
    parser.add_argument("--validation-fraction", type=float, default=0.2)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    print(f"Loading examples from {args.examples}...")
    examples = load_examples(args.examples)
    print(f"  Total: {len(examples)}")
    pos = sum(1 for e in examples if e["label"] == 1)
    neg = len(examples) - pos
    print(f"  Positive (disclosure): {pos}")
    print(f"  Negative (normal):     {neg}")

    try:
        train, validation = split_examples(
            examples, validation_fraction=args.validation_fraction, seed=args.seed,
        )
    except ValueError as exc:
        parser.error(str(exc))
    print(f"  Unique training examples: {len(train)}")
    print(f"  Held-out examples:        {len(validation)} (seed={args.seed})")

    print("\nBuilding feature matrix...")
    X, y = build_dataset(train)
    print(f"  Shape: {X.shape}")

    print(f"\nTraining LR (lr={args.lr}, epochs={args.epochs}, l2={args.l2})...")
    weights, bias = train_logistic_regression(X, y, lr=args.lr, epochs=args.epochs, l2=args.l2)

    print("\nHeld-out evaluation (runtime guards, thresholds and regex fallback):")
    metrics = evaluate(validation, weights, bias)
    print(f"  Accuracy:  {metrics['accuracy']}")
    print(f"  Precision: {metrics['precision']}")
    print(f"  Recall:    {metrics['recall']}")
    print(f"  F1:        {metrics['f1']}")
    print(f"  Confusion: TP={metrics['tp']} FP={metrics['fp']} FN={metrics['fn']} TN={metrics['tn']}")

    # Save
    args.output.parent.mkdir(parents=True, exist_ok=True)
    np.savez(args.output, weights=weights, bias=np.float32(bias))
    print(f"\nModel saved to {args.output} ({args.output.stat().st_size} bytes)")

    # Print weight interpretation
    print("\nFeature weights (top contributors):")
    feature_names = [
        "text_length_norm", "has_quote", "has_newline", "has_mixed_task",
        "is_chinese", "is_english", "has_tool_choice", "has_response_format",
        "identity_context", "pat_0", "pat_1", "pat_2", "pat_3",
        "pat_4", "pat_5", "pat_6", "followup_match",
        "encode_request", "roleplay", "multi_turn", "injection",
    ]
    indexed = sorted(enumerate(weights), key=lambda x: abs(x[1]), reverse=True)
    for idx, w in indexed[:10]:
        direction = "+" if w > 0 else "-"
        print(f"  {direction}{abs(w):.4f}  {feature_names[idx]}")


if __name__ == "__main__":
    main()
