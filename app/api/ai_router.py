# ============================================================
# File: c:\autocoin\app\api\ai_router.py
# Autocoin OS v3-H — AI Operations Router
# ============================================================

from fastapi import APIRouter, Request, Query
import os
import json
import time
from typing import Dict, Any, Optional
from app.manager.ai_trainer import ai_trainer, _ml_available, _ml_import_attempted

import logging
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ai", tags=["ai"])

@router.get("/debug", summary="Debug ML import status")
def debug_ml_status() -> Dict[str, Any]:
    """Check if pandas/sklearn are available."""
    try:
        import pandas as pd
        pd_version = pd.__version__
    except (ImportError, AttributeError, TypeError) as e:
        logger.warning("ai_router.debug_ml_status L26: %s", e)
        pd_version = f"Error: {e}"
    
    try:
        import sklearn
        sk_version = sklearn.__version__
    except (ImportError, AttributeError, TypeError) as e:
        logger.warning("ai_router.debug_ml_status L32: %s", e)
        sk_version = f"Error: {e}"
    
    return {
        "ml_available": _ml_available,
        "ml_import_attempted": _ml_import_attempted,
        "pandas_version": pd_version,
        "sklearn_version": sk_version,
    }

@router.get(
    "/info",
    summary="Get AI model information",
    responses={
        200: {"description": "AI model metadata and training info"},
    },
)
def get_ai_info() -> Dict[str, Any]:
    """
    Retrieve current AI model information including training data size and last update.
    """
    return {"ok": True, "info": ai_trainer.get_info()}

@router.get(
    "/history",
    summary="Get AI prediction accuracy history",
    responses={
        200: {"description": "Hourly accuracy buckets for the last 24 hours"},
    },
)
def get_ai_history() -> Dict[str, Any]:
    """
    Retrieve AI prediction accuracy history over the last 24 hours.
    """
    return ai_trainer.get_accuracy_history(window_hours=24, bucket_minutes=60)

@router.post(
    "/extract",
    summary="Extract training data from ledger",
    responses={
        200: {"description": "Training data extracted successfully"},
    },
)
def extract_data(
    strategy: Optional[str] = Query(None, description="Filter by strategy type"),
) -> Dict[str, Any]:
    """
    Extract training data from the ledger for AI model training.
    """
    return ai_trainer.extract_data(strategy=strategy)

@router.post(
    "/train",
    summary="Train AI model",
    responses={
        200: {"description": "Model training completed"},
    },
)
def train_model() -> Dict[str, Any]:
    """
    Train the AI prediction model using extracted data.
    """
    return ai_trainer.train_model()

@router.post(
    "/reload",
    summary="Reload AI brain model",
    responses={
        200: {"description": "Brain model reloaded into engine"},
        400: {"description": "Engine pipeline/brain not found"},
    },
)
def reload_brain(request: Request) -> Dict[str, Any]:
    """
    Reload the trained AI model into the running engine.
    """
    system = request.app.state.system
    try:
        if hasattr(system.engine, "pipeline") and hasattr(system.engine.pipeline, "brain"):
            system.engine.pipeline.brain.reload_model()
            return {"ok": True, "message": "Brain model reloaded"}
        else:
            return {"ok": False, "error": "Engine pipeline/brain not found"}
    except (KeyError, AttributeError, TypeError) as e:
        logger.warning("ai_router.reload_brain L115: %s", e)
        return {"ok": False, "error": str(e)}

@router.post(
    "/auto_full",
    summary="Full AI pipeline: extract, train, reload",
    responses={
        200: {"description": "Full pipeline completed successfully"},
        400: {"description": "Pipeline failed at a specific step"},
    },
)
def auto_full(request: Request) -> Dict[str, Any]:
    """
    Run the complete AI training pipeline.

    - Extract training data from ledger
    - Train the model
    - Reload the model into the engine
    """
    res_ext = ai_trainer.extract_data()
    if not res_ext.get("ok"):
        return {"ok": False, "step": "extract", "detail": res_ext}
    
    res_train = ai_trainer.train_model()
    if not res_train.get("ok"):
        return {"ok": False, "step": "train", "detail": res_train}
        
    res_reload = reload_brain(request)
    
    return {
        "ok": True,
        "extract": res_ext,
        "train": res_train,
        "reload": res_reload
    }

# ------------------------------------------------------------
# AI Gate Settings (Dashboard-controlled strictness)
# Stored in app/data/ai_gate_settings.json (atomic write).
# ------------------------------------------------------------
_AI_DATA_DIR = os.path.join("app", "data")
_AI_GATE_PATH = os.path.join(_AI_DATA_DIR, "ai_gate_settings.json")
_AI_SCOREBOARD_PATH = os.path.join(_AI_DATA_DIR, "ai_market_scoreboard.json")

def _gate_thresholds_from_strictness(strictness: int) -> Dict[str, Any]:
    s = max(0, min(100, int(strictness)))
    # Linear mappings (operational defaults)
    min_test_samples = int(round(150 + (800 - 150) * (s / 100.0)))
    min_acc_mean = float(0.52 + (0.60 - 0.52) * (s / 100.0))
    min_high_conf_acc_mean = float(0.55 + (0.65 - 0.55) * (s / 100.0))
    return {
        "strictness": s,
        "min_test_samples": min_test_samples,
        "min_acc_mean": min_acc_mean,
        "min_high_conf_acc_mean": min_high_conf_acc_mean,
    }

def _load_gate_settings() -> Dict[str, Any]:
    # default
    out = _gate_thresholds_from_strictness(60)
    out["updated_ts"] = 0.0
    try:
        if os.path.exists(_AI_GATE_PATH):
            with open(_AI_GATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                strict = data.get("strictness", out["strictness"])
                out = _gate_thresholds_from_strictness(int(strict))
                out["updated_ts"] = float(data.get("updated_ts") or 0.0)
    except (OSError, json.JSONDecodeError, KeyError, AttributeError, TypeError, ValueError) as exc:
        logger.warning("[ai_router] %s: %s", 'default', exc, exc_info=True)
    return out

def _atomic_write_json(path: str, data: Dict[str, Any]) -> None:
    from app.core.io_utils import safe_write_json
    safe_write_json(path, data)

def _load_scoreboard() -> Dict[str, Any]:
    if not os.path.exists(_AI_SCOREBOARD_PATH):
        return {}
    try:
        with open(_AI_SCOREBOARD_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        logger.warning("ai_router._load_scoreboard L203 except", exc_info=True)
        return {}

def _compute_gate_stats(gate: Dict[str, Any]) -> Dict[str, Any]:
    sb = _load_scoreboard()
    markets = sb.get("markets") if isinstance(sb.get("markets"), dict) else {}
    if not isinstance(markets, dict):
        markets = {}

    total = 0
    eligible = 0
    min_samples = int(gate.get("min_test_samples") or 0)
    min_acc = float(gate.get("min_acc_mean") or 0.0)
    min_hc = float(gate.get("min_high_conf_acc_mean") or 0.0)

    for mkt, rec in markets.items():
        if not isinstance(rec, dict):
            continue
        total += 1
        tsamp = float(rec.get("test_samples") or 0)
        acc = float(rec.get("acc_mean") or 0)
        hc = float(rec.get("high_conf_acc_mean") or 0)
        if tsamp >= min_samples and acc >= min_acc and hc >= min_hc:
            eligible += 1

    return {"scoreboard_total": total, "eligible": eligible}

@router.get(
    "/gate",
    summary="Get AI gate settings",
    responses={
        200: {"description": "Current AI gate thresholds and eligibility stats"},
    },
)
def get_ai_gate() -> Dict[str, Any]:
    """
    Retrieve AI gate settings that control strictness of model deployment.
    """
    gate = _load_gate_settings()
    stats = _compute_gate_stats(gate)
    return {"ok": True, "gate": gate, "stats": stats}

@router.post(
    "/gate",
    summary="Set AI gate strictness",
    responses={
        200: {"description": "Gate settings updated"},
    },
)
async def set_ai_gate(req: Request) -> Dict[str, Any]:
    """
    Update the AI gate strictness level (0-100).

    Higher strictness requires better model accuracy for deployment.
    """
    try:
        body = await req.json()
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
        logger.warning("ai_router.get_ai_gate L260 except", exc_info=True)
        body = {}
    if not isinstance(body, dict):
        body = {}
    strict = body.get("strictness", 60)
    try:
        strict = int(strict)
    except (TypeError, ValueError):
        logger.warning("ai_router.get_ai_gate L267 except", exc_info=True)
        strict = 60

    gate = _gate_thresholds_from_strictness(strict)
    gate["updated_ts"] = time.time()
    _atomic_write_json(_AI_GATE_PATH, gate)

    stats = _compute_gate_stats(gate)
    return {"ok": True, "gate": gate, "stats": stats}

