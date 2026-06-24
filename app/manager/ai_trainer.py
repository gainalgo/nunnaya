# ============================================================
# File: app/manager/ai_trainer.py
# Autocoin OS v3-H — AI Training Manager (LightGBM + Time-Split)
# ============================================================
# PATCH 2026-01-28: LightGBM swap, Time-split validation, Feature expansion
# PATCH 2026-01-28b: Per-coin-tier sample weights, hybrid labeling support
# PATCH 2026-01-30: Multi-horizon (1m/5m/15m) model training support
# ============================================================

import json
import logging
import os
import glob
import pickle
import time
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

# Multi-horizon settings
HORIZONS = [60, 300, 900, 3600]  # 1m, 5m, 15m, 1h (in seconds)
HORIZON_WEIGHTS = [0.15, 0.35, 0.30, 0.20]  # ensemble weights (1h added)
MIN_RET_THRESHOLD = 0.2  # minimum threshold (%)
ATR_THRESHOLD_MULT = 0.3  # ATR-based threshold multiplier

try:
    from app.notify.telegram import send_telegram
    from app.strategy import indicators
    from app.ai.features import (
        extract_features_from_row,
        get_feature_names,
        REGIME_CATEGORIES,
    )
    from app.ai.coin_tiers import (
        get_sample_weight,
        extract_strategy_from_reason,
        normalize_strategy,
    )
except ImportError:
    logger.warning("[AITrainer] Failed to import telegram/features/coin_tiers", exc_info=True)
    def send_telegram(msg): pass
    def extract_features_from_row(row): return {}
    def get_feature_names(): return []
    def get_sample_weight(strategy=None, market=None): return 1.0
    def extract_strategy_from_reason(reason): return "unknown"
    def normalize_strategy(s): return "unknown"
    REGIME_CATEGORIES = []

# Lazy import cache
_ml_modules: Dict[str, Any] = {}
_ml_import_attempted: bool = False
_ml_available: bool = False
_lgbm_available: bool = False
_xgb_available: bool = False


def _ensure_ml_imports() -> bool:
    """Lazy import pandas/sklearn/lightgbm/xgboost. Returns True if available."""
    global _ml_modules, _ml_import_attempted, _ml_available, _lgbm_available, _xgb_available
    
    if _ml_import_attempted:
        return _ml_available
    
    _ml_import_attempted = True
    
    try:
        import warnings
        warnings.filterwarnings("ignore", category=UserWarning)
        os.environ["PYTHONWARNINGS"] = "ignore::UserWarning"
        
        import pandas as pd
        import numpy as np
        from sklearn.metrics import (
            accuracy_score, 
            precision_score, 
            recall_score, 
            roc_auc_score,
            classification_report,
        )
        
        _ml_modules["pd"] = pd
        _ml_modules["np"] = np
        _ml_modules["accuracy_score"] = accuracy_score
        _ml_modules["precision_score"] = precision_score
        _ml_modules["recall_score"] = recall_score
        _ml_modules["roc_auc_score"] = roc_auc_score
        _ml_modules["classification_report"] = classification_report
        
        _ml_available = True
        
        # Try LightGBM (successful import = available)
        try:
            import lightgbm as lgb
            # Test if DLL loads properly
            lgb.Dataset
            _ml_modules["lgb"] = lgb
            _lgbm_available = True
        except (ImportError, OSError):
            logger.warning("[AITrainer] LightGBM import failed", exc_info=True)
            _lgbm_available = False
        
        # Try XGBoost (LightGBM alternative)
        try:
            import xgboost as xgb
            _ml_modules["xgb"] = xgb
            _xgb_available = True
        except ImportError:
            logger.warning("[AITrainer] XGBoost import failed", exc_info=True)
            _xgb_available = False
        
        # RandomForest is always loaded (for fallback)
        from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
        _ml_modules["RandomForestClassifier"] = RandomForestClassifier
        _ml_modules["GradientBoostingClassifier"] = GradientBoostingClassifier
        
        return True
    except ImportError as e:
        import logging
        logging.getLogger(__name__).error(f"[AITrainer] ML import failed: {e}")
        _ml_available = False
        return False
    except (KeyError, AttributeError, TypeError, ValueError) as e:
        import logging
        logging.getLogger(__name__).error(f"[AITrainer] ML import error: {e}")
        _ml_available = False
        return False


class AITrainer:
    def __init__(self):
        self.ledger_dir = "runtime"
        self.data_dir = os.path.join("app", "data")
        self.dataset_path = os.path.join(self.ledger_dir, "training_dataset.csv")
        self.model_path = os.path.join(self.data_dir, "ai_model.pkl")
        self.meta_path = os.path.join(self.data_dir, "ai_model_meta.json")

    def extract_data(
        self, 
        days: float = 7.0, 
        strategy: Optional[str] = None,
        horizons: Optional[List[int]] = None,
    ) -> Dict[str, Any]:
        """
        Multi-horizon labeling support.
        horizons: list of prediction horizons (in seconds). Default [60, 300, 900]
        """
        if not _ensure_ml_imports():
            return {"ok": False, "error": "pandas/sklearn not installed"}
        
        if horizons is None:
            horizons = HORIZONS
        
        pd = _ml_modules["pd"]
        np = _ml_modules["np"]

        pattern = os.path.join(self.ledger_dir, "trade_ledger.jsonl*")
        files = sorted(glob.glob(pattern))
        
        cutoff_ts = time.time() - (float(days) * 86400.0)
        target_event = f"{strategy.upper()}_SNAPSHOT" if strategy else None
        
        records = []
        for file in files:
            try:
                with open(file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line: continue
                        try:
                            rec = json.loads(line)
                            event = rec.get("event")
                            ts = float(rec.get("ts") or 0.0)
                            if ts < cutoff_ts:
                                continue
                            is_target = False
                            if strategy:
                                if event == target_event:
                                    is_target = True
                            elif event and "_SNAPSHOT" in str(event).upper():
                                # Include all strategy snapshots: AUTOLOOP, PINGPONG, LADDER, LIGHTNING, GAZUA, CONTRARIAN, etc.
                                is_target = True

                            if is_target:
                                data = rec.get("data", {})
                                row = {k: v for k, v in data.items() if isinstance(v, (int, float, str, bool))}
                                row["market"] = str(rec.get("market") or "UNKNOWN")
                                row["ts"] = rec.get("ts")
                                row["event"] = event
                                records.append(row)
                        except (KeyError, TypeError, ValueError):
                            logger.warning("[AITrainer] extract_data: record parse failed", exc_info=True)
                            continue
            except (OSError, json.JSONDecodeError, KeyError, AttributeError, TypeError, ValueError) as exc:
                logger.warning("[AI_TRAIN] extract_data: ledger file read failed: %s", exc, exc_info=True)

        if not records:
            strat_msg = f", strategy={strategy}" if strategy else ""
            msg = f"[AI] Extraction failed: No records found in ledger (days={days}{strat_msg})"
            print(msg)
            send_telegram(msg)
            return {"ok": False, "error": "no_records_found"}

        df = pd.DataFrame(records)
        df = df.sort_values(["market", "ts"])

        if "price" not in df.columns or "ts" not in df.columns:
            msg = "[AI] Extraction failed: 'price' or 'ts' column missing"
            print(msg)
            send_telegram(msg)
            return {"ok": False, "error": "no_price_column"}

        df["price"] = pd.to_numeric(df["price"], errors="coerce")
        df["ts"] = pd.to_numeric(df["ts"], errors="coerce")
        df = df.dropna(subset=["price", "ts"])
        
        # ========================================
        # Strategy extraction (event-based)
        # ========================================
        def extract_strategy_from_event(event: str) -> str:
            if not event:
                return "unknown"
            event_upper = str(event).upper()
            for s in ["AUTOLOOP", "PINGPONG", "LIGHTNING", "GAZUA", "LADDER", "CONTRARIAN"]:
                if s in event_upper:
                    return s.lower()
            return "unknown"
        
        df["strategy"] = df["event"].apply(extract_strategy_from_event)
        
        # Also try extracting from the reason column
        if "reason" in df.columns:
            def refine_strategy(row):
                if row["strategy"] == "unknown" and row.get("reason"):
                    return extract_strategy_from_reason(str(row["reason"]))
                return row["strategy"]
            df["strategy"] = df.apply(refine_strategy, axis=1)
        
        # ========================================
        # Extended feature computation (groupby per market)
        # ========================================
        def compute_extended_features(g):
            g = g.sort_values("ts").copy()
            price = g["price"]

            # Multi-window returns (%)
            for n in [1, 3, 5, 10, 20]:
                g[f"ret_{n}"] = price.pct_change(n) * 100.0

            # EMA
            g["ema_fast"] = price.ewm(span=12, adjust=False).mean()
            g["ema_slow"] = price.ewm(span=26, adjust=False).mean()
            g["ema_gap_pct"] = (g["ema_fast"] - g["ema_slow"]) / price * 100.0
            g["ema_slope"] = g["ema_fast"].diff(3) / price * 100.0

            # Volatility
            returns = price.pct_change() * 100.0
            g["realized_vol_5"] = returns.rolling(5, min_periods=2).std()
            g["realized_vol_20"] = returns.rolling(20, min_periods=5).std()

            # ATR approximation (based on price range)
            g["atr_pct"] = price.rolling(14, min_periods=3).apply(
                lambda x: (x.max() - x.min()) / x.mean() * 100.0 if x.mean() > 0 else 0.0,
                raw=True
            )
            
            # Bollinger Width
            sma20 = price.rolling(20, min_periods=5).mean()
            std20 = price.rolling(20, min_periods=5).std()
            g["bb_width"] = (std20 * 2) / sma20 * 100.0
            
            # Volume Z-score (if available)
            if "volume" in g.columns:
                vol = pd.to_numeric(g["volume"], errors="coerce")
                vol_mean = vol.rolling(20, min_periods=5).mean()
                vol_std = vol.rolling(20, min_periods=5).std()
                g["vol_ratio"] = vol / vol_mean.replace(0, np.nan)
                g["notional_z"] = (vol - vol_mean) / vol_std.replace(0, np.nan)
            
            return g
        
        # Keep market as a column, not an index
        df = df.reset_index(drop=True)
        df = df.groupby("market", group_keys=False).apply(compute_extended_features)
        df = df.reset_index(drop=True)
        
        # ========================================
        # BTC beta (relative to the whole market)
        # ========================================
        if "market" not in df.columns:
            df["market"] = df.index.get_level_values("market") if "market" in df.index.names else "UNKNOWN"
        btc_df = df[df["market"].str.contains("BTC", case=False, na=False)]
        if not btc_df.empty:
            btc_ret = btc_df.groupby(btc_df["ts"].round(-1))["price"].first().pct_change() * 100.0
            btc_ret_dict = btc_ret.to_dict()
            df["btc_ret_pct"] = df["ts"].round(-1).map(btc_ret_dict).fillna(0.0)
            df["coin_vs_btc"] = df["ret_5"] - df["btc_ret_pct"]
        else:
            df["btc_ret_pct"] = 0.0
            df["coin_vs_btc"] = 0.0
        
        # ========================================
        # Per-horizon label generation (merge_asof vectorized)
        # ========================================
        # Estimate data interval (dynamic tolerance setting)
        ts_diff = df.groupby("market")["ts"].diff().median()
        base_tolerance = max(60, ts_diff * 1.5 if pd.notna(ts_diff) else 60)
        
        for horizon in horizons:
            tolerance = max(horizon * 0.3, base_tolerance)
            
            # Future price matching (vectorized)
            future_prices = []
            for market, g in df.groupby("market"):
                g = g.sort_values("ts")
                timestamps = g["ts"].values
                prices = g["price"].values
                fp = []
                
                for i, ts in enumerate(timestamps):
                    target_ts = ts + horizon
                    # Binary search for the nearest future timestamp
                    future_idx = np.searchsorted(timestamps, target_ts)
                    
                    if future_idx >= len(timestamps):
                        fp.append(np.nan)
                    else:
                        # Compare both candidates
                        candidates = []
                        if future_idx < len(timestamps):
                            candidates.append((future_idx, abs(timestamps[future_idx] - target_ts)))
                        if future_idx > i + 1:
                            candidates.append((future_idx - 1, abs(timestamps[future_idx - 1] - target_ts)))
                        
                        if candidates:
                            best_idx, best_diff = min(candidates, key=lambda x: x[1])
                            if best_diff <= tolerance and best_idx > i:
                                fp.append(prices[best_idx])
                            else:
                                fp.append(np.nan)
                        else:
                            fp.append(np.nan)
                
                future_prices.extend(fp)
            
            price_col = f"price_next_{horizon}s"
            df[price_col] = future_prices
            
            # Return computation
            ret_col = f"ret_{horizon}s"
            df[ret_col] = (df[price_col] - df["price"]) / df["price"] * 100.0
            
            # ========================================
            # Symmetric labeling + neutral removal (key improvement)
            # ========================================
            # Dynamic threshold (ATR-based)
            if "atr_pct" in df.columns:
                df["_threshold"] = (df["atr_pct"].fillna(MIN_RET_THRESHOLD) * ATR_THRESHOLD_MULT).clip(lower=MIN_RET_THRESHOLD, upper=1.0)
            else:
                df["_threshold"] = MIN_RET_THRESHOLD
            
            # Symmetric label: up=1, down=0, neutral=NaN (excluded from training)
            col_name = f"target_{horizon}s"
            df[col_name] = np.where(
                df[ret_col] >= df["_threshold"], 1,
                np.where(df[ret_col] <= -df["_threshold"], 0, np.nan)
            )
        
        # Default target is 5 minutes (300s)
        if "target_300s" in df.columns:
            df["target"] = df["target_300s"]
            df["price_next"] = df.get("price_next_300s", np.nan)
            df["ret"] = df.get("ret_300s", np.nan)
        
        # Sample weight: based on |ret| (learn larger moves more)
        if "ret_300s" in df.columns:
            df["sample_weight_ret"] = df["ret_300s"].abs().clip(lower=0.1, upper=3.0) / 1.5
        
        # Statistics logging
        horizon_stats = {}
        for h in horizons:
            ret_col = f"ret_{h}s"
            target_col = f"target_{h}s"
            if target_col in df.columns:
                valid = df[target_col].notna().sum()
                pos = (df[target_col] == 1).sum()
                neg = (df[target_col] == 0).sum()
                horizon_stats[h] = {"valid": int(valid), "pos": int(pos), "neg": int(neg)}
        
        strategy_dist = df["strategy"].value_counts().to_dict()
        
        # Neutral removal (drop rows where target is NaN)
        df_valid = df.dropna(subset=["target"])
        df_valid.to_csv(self.dataset_path, index=False)
        
        return {
            "ok": True, 
            "rows": len(df_valid), 
            "rows_before_neutral_removal": len(df),
            "path": self.dataset_path, 
            "horizons": horizons,
            "horizon_stats": horizon_stats,
            "strategy_distribution": strategy_dist,
        }

    def train_model(self, use_time_split: bool = True, multi_horizon: bool = True) -> Dict[str, Any]:
        """
        Model training.
        - LightGBM first, RandomForest fallback if unavailable
        - Time-split validation (most recent 20% as test)
        - Uses the extended feature set
        - if multi_horizon=True, trains and saves a separate model per horizon
        """
        if not _ensure_ml_imports():
            return {"ok": False, "error": "pandas/sklearn not installed"}
        
        pd = _ml_modules["pd"]
        np = _ml_modules["np"]
        accuracy_score = _ml_modules["accuracy_score"]
        precision_score = _ml_modules["precision_score"]
        recall_score = _ml_modules["recall_score"]
        roc_auc_score = _ml_modules["roc_auc_score"]

        if not os.path.exists(self.dataset_path):
            return {"ok": False, "error": "dataset_not_found"}

        df = pd.read_csv(self.dataset_path)
        
        if "target" not in df.columns:
            return {"ok": False, "error": "target_column_missing"}
        
        # ========================================
        # Feature extraction (extended feature set)
        # ========================================
        feature_rows = []
        for _, row in df.iterrows():
            feat = extract_features_from_row(row.to_dict())
            feature_rows.append(feat)
        
        feature_df = pd.DataFrame(feature_rows)
        
        # Numeric conversion and missing-value handling
        for col in feature_df.columns:
            feature_df[col] = pd.to_numeric(feature_df[col], errors="coerce")
        
        # Drop columns that are entirely NaN
        valid_cols = [c for c in feature_df.columns if feature_df[c].notna().sum() > len(df) * 0.1]
        feature_df = feature_df[valid_cols]
        
        X = feature_df
        y = df["target"].astype(int)
        
        # Drop rows with missing values (LightGBM also tolerates NaN)
        valid_mask = X.notna().all(axis=1) & y.notna()
        X = X[valid_mask].reset_index(drop=True)
        y = y[valid_mask].reset_index(drop=True)
        
        if len(X) < 100:
            msg = f"[AI] Training skipped: Insufficient data ({len(X)} rows < 100)"
            print(msg)
            send_telegram(msg)
            return {"ok": False, "error": "insufficient_data", "rows": len(X)}

        # ========================================
        # Time-split (chronological split)
        # ========================================
        if use_time_split:
            # Sort by ts, then use the most recent 20% as test
            ts_col = df.loc[valid_mask, "ts"].reset_index(drop=True) if "ts" in df.columns else None
            if ts_col is not None:
                sorted_idx = ts_col.argsort()
                X = X.iloc[sorted_idx].reset_index(drop=True)
                y = y.iloc[sorted_idx].reset_index(drop=True)
            
            split_idx = int(len(X) * 0.8)
            X_train, X_test = X.iloc[:split_idx], X.iloc[split_idx:]
            y_train, y_test = y.iloc[:split_idx], y.iloc[split_idx:]
        else:
            from sklearn.model_selection import train_test_split
            X_train, X_test, y_train, y_test = train_test_split(
                X, y, test_size=0.2, random_state=42
            )
        
        feature_names = list(X.columns)
        
        # ========================================
        # Per-strategy sample weight computation (strategy column first)
        # ========================================
        sample_weights_train = None
        strategy_stats: Dict[str, int] = {}

        # First use the strategy column (extracted in extract_data)
        if "strategy" in df.columns:
            strategies = df.loc[valid_mask, "strategy"].reset_index(drop=True)
            if use_time_split and "ts" in df.columns:
                ts_col = df.loc[valid_mask, "ts"].reset_index(drop=True)
                sorted_idx = ts_col.argsort()
                strategies = strategies.iloc[sorted_idx].reset_index(drop=True)
            
            train_strategies = strategies.iloc[:len(X_train)]
            weights = []
            for s in train_strategies:
                strategy = normalize_strategy(str(s) if s else None)
                w = get_sample_weight(strategy=strategy)
                weights.append(w)
                strategy_stats[strategy] = strategy_stats.get(strategy, 0) + 1
            sample_weights_train = np.array(weights)
        else:
            # fallback: extract strategy from the reason column
            reason_col = None
            for col in ["reason", "signal", "tactic", "buy_reason"]:
                if col in df.columns:
                    reason_col = col
                    break
            
            if reason_col:
                reasons = df.loc[valid_mask, reason_col].reset_index(drop=True)
                if use_time_split and "ts" in df.columns:
                    ts_col = df.loc[valid_mask, "ts"].reset_index(drop=True)
                    sorted_idx = ts_col.argsort()
                    reasons = reasons.iloc[sorted_idx].reset_index(drop=True)
                
                train_reasons = reasons.iloc[:len(X_train)]
                weights = []
                for r in train_reasons:
                    strategy = extract_strategy_from_reason(str(r) if r else None)
                    w = get_sample_weight(strategy=strategy)
                    weights.append(w)
                    strategy_stats[strategy] = strategy_stats.get(strategy, 0) + 1
                sample_weights_train = np.array(weights)
        
        # ========================================
        # Multi-horizon training branch
        # ========================================
        has_multi_targets = all(f"target_{h}s" in df.columns for h in HORIZONS)
        
        if multi_horizon and has_multi_targets:
            return self._train_multi_horizon(
                df, X, y, valid_mask, feature_names, 
                sample_weights_train, strategy_stats, use_time_split
            )
        
        # ========================================
        # Single model training (LightGBM first)
        # ========================================
        model_type = "unknown"
        clf = None
        
        if _lgbm_available:
            lgb = _ml_modules["lgb"]
            
            params = {
                "objective": "binary",
                "metric": ["binary_logloss", "auc"],
                "boosting_type": "gbdt",
                "num_leaves": 31,
                "max_depth": 5,
                "learning_rate": 0.03,
                "min_child_samples": 30,
                "feature_fraction": 0.7,
                "bagging_fraction": 0.7,
                "bagging_freq": 3,
                "is_unbalance": True,
                "verbose": -1,
                "random_state": 42,
                "force_col_wise": True,
            }
            
            train_data = lgb.Dataset(
                X_train, label=y_train, 
                feature_name=feature_names,
                weight=sample_weights_train,
            )
            valid_data = lgb.Dataset(X_test, label=y_test, feature_name=feature_names, reference=train_data)
            
            clf = lgb.train(
                params,
                train_data,
                num_boost_round=300,
                valid_sets=[valid_data],
                callbacks=[
                    lgb.early_stopping(stopping_rounds=30),
                    lgb.log_evaluation(period=50),
                ],
            )
            model_type = "LightGBM"
            
            y_pred_proba = clf.predict(X_test)
            best_threshold = 0.5
            best_f1 = 0.0
            for thresh in [0.3, 0.35, 0.4, 0.45, 0.5]:
                pred = (y_pred_proba >= thresh).astype(int)
                try:
                    from sklearn.metrics import f1_score
                    f1 = f1_score(y_test, pred, zero_division=0)
                    if f1 > best_f1:
                        best_f1 = f1
                        best_threshold = thresh
                except (ImportError, AttributeError, TypeError) as exc:
                    logger.warning("[AI_TRAIN] ai_trainer fallback: %s", exc, exc_info=True)
            y_pred = (y_pred_proba >= best_threshold).astype(int)
        else:
            RandomForestClassifier = _ml_modules["RandomForestClassifier"]
            clf = RandomForestClassifier(
                n_estimators=200, 
                max_depth=6, 
                min_samples_leaf=50,
                class_weight="balanced",
                random_state=42,
                n_jobs=-1,
            )
            clf.fit(X_train, y_train, sample_weight=sample_weights_train)
            model_type = "RandomForest"
            
            y_pred_proba = clf.predict_proba(X_test)[:, 1]
            y_pred = clf.predict(X_test)
        
        # ========================================
        # Performance evaluation
        # ========================================
        acc = accuracy_score(y_test, y_pred)
        
        try:
            auc = roc_auc_score(y_test, y_pred_proba)
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
            logger.warning("[AITrainer] roc_auc_score failed", exc_info=True)
            auc = 0.5

        try:
            precision = precision_score(y_test, y_pred, zero_division=0)
            recall = recall_score(y_test, y_pred, zero_division=0)
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
            logger.warning("[AITrainer] precision/recall_score failed", exc_info=True)
            precision = 0.0
            recall = 0.0
        
        high_conf_mask = (y_pred_proba >= 0.6) | (y_pred_proba <= 0.4)
        high_conf_count = high_conf_mask.sum()
        high_conf_acc = 0.0
        high_conf_precision = 0.0
        
        if high_conf_count > 0:
            y_test_hc = np.array(y_test)[high_conf_mask]
            y_pred_hc = y_pred[high_conf_mask]
            high_conf_acc = accuracy_score(y_test_hc, y_pred_hc)
            try:
                high_conf_precision = precision_score(y_test_hc, y_pred_hc, zero_division=0)
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
                logger.warning("[AITrainer] high_conf_precision failed", exc_info=True)
                high_conf_precision = 0.0
        
        importances = {}
        if _lgbm_available and hasattr(clf, "feature_importance"):
            importance_vals = clf.feature_importance(importance_type="gain")
            for name, imp in zip(feature_names, importance_vals):
                importances[name] = float(imp)
        elif hasattr(clf, "feature_importances_"):
            for name, imp in zip(feature_names, clf.feature_importances_):
                importances[name] = float(imp)
        
        os.makedirs(self.data_dir, exist_ok=True)
        with open(self.model_path, "wb") as f:
            pickle.dump(clf, f)
        
        meta = {
            "ts": time.time(),
            "model_type": model_type,
            "accuracy": float(acc),
            "auc": float(auc),
            "precision": float(precision),
            "recall": float(recall),
            "high_conf_accuracy": float(high_conf_acc),
            "high_conf_precision": float(high_conf_precision),
            "high_conf_ratio": float(high_conf_count / len(y_test)) if len(y_test) > 0 else 0.0,
            "features": feature_names,
            "importance": importances,
            "rows": len(X),
            "train_rows": len(X_train),
            "test_rows": len(X_test),
            "split_method": "time_split" if use_time_split else "random_split",
            "class_balance": {
                "train_pos": int(y_train.sum()),
                "train_neg": int(len(y_train) - y_train.sum()),
                "test_pos": int(y_test.sum()),
                "test_neg": int(len(y_test) - y_test.sum()),
            },
            "strategy_distribution": strategy_stats,
        }
        
        try:
            from app.core.io_utils import safe_write_json
            safe_write_json(self.meta_path, meta)
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("[AI_TRAIN] ai_trainer fallback: %s", exc, exc_info=True)

        try:
            warn_tag = ""
            if len(X) < 1000:
                warn_tag = "⚠️ "
            if high_conf_acc < 0.55:
                warn_tag = "🔴 "
            
            msg = (
                f"{warn_tag}🤖 [AI] Model Trained ({model_type})\n"
                f"• Accuracy: {acc:.1%} | AUC: {auc:.3f}\n"
                f"• Precision: {precision:.1%} | Recall: {recall:.1%}\n"
                f"• HighConf Acc: {high_conf_acc:.1%} (n={high_conf_count})\n"
                f"• Data: {len(X)} rows (time-split)"
            )
            if importances:
                sorted_imp = sorted(importances.items(), key=lambda x: x[1], reverse=True)[:5]
                msg += "\n\n📊 Top Features:"
                for k, v in sorted_imp:
                    msg += f"\n- {k}: {v:.1f}"
            send_telegram(msg)
        except (KeyError, IndexError, AttributeError, TypeError) as exc:
            logger.warning("[AI_TRAIN] ai_trainer fallback: %s", exc, exc_info=True)

        return {
            "ok": True, 
            "accuracy": float(acc), 
            "auc": float(auc),
            "precision": float(precision),
            "recall": float(recall),
            "high_conf_accuracy": float(high_conf_acc),
            "model_type": model_type,
            "model_path": self.model_path,
            "features": feature_names,
            "meta": meta,
        }

    def _train_multi_horizon(
        self,
        df,
        X,
        y,
        valid_mask,
        feature_names: List[str],
        sample_weights_train,
        strategy_stats: Dict[str, int],
        use_time_split: bool,
    ) -> Dict[str, Any]:
        """Multi-horizon (1m/5m/15m) model training. Keeps X/y ordering in sync."""
        np = _ml_modules["np"]
        pd = _ml_modules["pd"]
        accuracy_score = _ml_modules["accuracy_score"]
        roc_auc_score = _ml_modules["roc_auc_score"]
        
        models = {}
        metrics = {}
        
        # ========================================
        # Key fix: align X and df to the same index
        # valid_mask is a boolean array; X already has valid_mask applied and reset
        # ========================================
        # Apply valid_mask to df too, then reset
        df_valid = df[valid_mask].reset_index(drop=True).copy()
        X_valid = X.copy()  # valid_mask already applied

        # Sort
        if use_time_split and "ts" in df_valid.columns:
            sorted_idx = df_valid["ts"].argsort().values
            df_valid = df_valid.iloc[sorted_idx].reset_index(drop=True)
            X_valid = X_valid.iloc[sorted_idx].reset_index(drop=True)
        
        # Sample weights
        sw_ret = None
        if "sample_weight_ret" in df_valid.columns:
            sw_ret = df_valid["sample_weight_ret"].reset_index(drop=True)
        
        model_type = "Unknown_MultiHorizon"  # Will be set in training loop
        
        for horizon in HORIZONS:
            target_col = f"target_{horizon}s"
            if target_col not in df_valid.columns:
                continue
            
            y_horizon = df_valid[target_col].reset_index(drop=True)
            
            # Valid labels only (exclude NaN)
            valid_y_mask = y_horizon.notna()
            X_h = X_valid[valid_y_mask].reset_index(drop=True)
            y_h = y_horizon[valid_y_mask].astype(int).reset_index(drop=True)
            
            if len(X_h) < 100:
                continue
            
            split_idx = int(len(X_h) * 0.8)
            X_train, X_test = X_h.iloc[:split_idx], X_h.iloc[split_idx:]
            y_train, y_test = y_h.iloc[:split_idx], y_h.iloc[split_idx:]
            
            # Combine weights: strategy weight x return-based weight
            combined_weights = None
            if sample_weights_train is not None and sw_ret is not None:
                sw_ret_valid = sw_ret[valid_y_mask].reset_index(drop=True)
                sw_strat_valid = sample_weights_train[:len(sw_ret_valid)] if len(sample_weights_train) >= len(sw_ret_valid) else None
                if sw_strat_valid is not None:
                    sw_train_ret = sw_ret_valid.iloc[:split_idx].values
                    sw_train_strat = sw_strat_valid[:split_idx]
                    combined_weights = sw_train_ret * sw_train_strat
            elif sw_ret is not None:
                sw_ret_valid = sw_ret[valid_y_mask].reset_index(drop=True)
                combined_weights = sw_ret_valid.iloc[:split_idx].values
            elif sample_weights_train is not None:
                combined_weights = sample_weights_train[:split_idx]
            
            if _lgbm_available:
                lgb = _ml_modules["lgb"]
                params = {
                    "objective": "binary",
                    "metric": ["binary_logloss", "auc"],
                    "boosting_type": "gbdt",
                    "num_leaves": 31,
                    "max_depth": 5,
                    "learning_rate": 0.03,
                    "min_child_samples": 30,
                    "feature_fraction": 0.7,
                    "bagging_fraction": 0.7,
                    "bagging_freq": 3,
                    "is_unbalance": True,
                    "verbose": -1,
                    "random_state": 42,
                }
                
                train_data = lgb.Dataset(X_train, label=y_train, weight=combined_weights)
                valid_data = lgb.Dataset(X_test, label=y_test, reference=train_data)
                
                clf = lgb.train(
                    params, train_data,
                    num_boost_round=300,
                    valid_sets=[valid_data],
                    callbacks=[lgb.early_stopping(stopping_rounds=30)],
                )
                
                y_pred_proba = clf.predict(X_test)
                models[horizon] = clf
                model_type = "LightGBM_MultiHorizon"
            elif _xgb_available:
                # XGBoost as LightGBM alternative
                xgb = _ml_modules["xgb"]
                
                # Calculate scale_pos_weight for imbalanced data
                n_pos = int(y_train.sum())
                n_neg = len(y_train) - n_pos
                scale_pos_weight = n_neg / n_pos if n_pos > 0 else 1.0
                
                clf = xgb.XGBClassifier(
                    n_estimators=300,
                    max_depth=5,
                    learning_rate=0.03,
                    subsample=0.7,
                    colsample_bytree=0.7,
                    scale_pos_weight=scale_pos_weight,
                    random_state=42,
                    use_label_encoder=False,
                    eval_metric="auc",
                    early_stopping_rounds=30,
                    verbosity=0,
                )
                clf.fit(
                    X_train, y_train,
                    sample_weight=combined_weights,
                    eval_set=[(X_test, y_test)],
                    verbose=False,
                )
                y_pred_proba = clf.predict_proba(X_test)[:, 1]
                models[horizon] = clf
                model_type = "XGBoost_MultiHorizon"
            else:
                # Fallback to GradientBoosting (better than RandomForest)
                GradientBoostingClassifier = _ml_modules["GradientBoostingClassifier"]
                clf = GradientBoostingClassifier(
                    n_estimators=200,
                    max_depth=5,
                    learning_rate=0.05,
                    min_samples_leaf=30,
                    subsample=0.7,
                    random_state=42,
                )
                clf.fit(X_train, y_train, sample_weight=combined_weights)
                y_pred_proba = clf.predict_proba(X_test)[:, 1]
                models[horizon] = clf
                model_type = "GradientBoosting_MultiHorizon"
            
            acc = accuracy_score(y_test, (y_pred_proba >= 0.5).astype(int))
            try:
                auc = roc_auc_score(y_test, y_pred_proba)
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
                logger.warning("[AITrainer] multi-horizon roc_auc_score failed", exc_info=True)
                auc = 0.5
            
            metrics[horizon] = {"accuracy": float(acc), "auc": float(auc), "rows": len(X_h)}
        
        if not models:
            return {"ok": False, "error": "no_models_trained"}
        
        # Feature Importance aggregation (averaged across all horizons)
        importances = {}
        for horizon, clf in models.items():
            try:
                if _lgbm_available and hasattr(clf, "feature_importance"):
                    imp_vals = clf.feature_importance(importance_type="gain")
                elif hasattr(clf, "feature_importances_"):
                    imp_vals = clf.feature_importances_
                else:
                    continue
                for name, val in zip(feature_names, imp_vals):
                    importances[name] = importances.get(name, 0.0) + float(val)
            except (KeyError, AttributeError, TypeError, ValueError) as exc:
                logger.warning("[AI_TRAIN] Feature Importance aggregation except-> continue: %s", exc, exc_info=True)
                continue
        # Average (divide by number of horizons)
        if importances and len(models) > 0:
            for k in importances:
                importances[k] /= len(models)
        
        # Save model (multi-model)
        model_data = {
            "models": models,
            "feature_names": feature_names,
            "horizons": HORIZONS,
            "weights": HORIZON_WEIGHTS,
            "is_multi_horizon": True,
        }
        
        os.makedirs(self.data_dir, exist_ok=True)
        with open(self.model_path, "wb") as f:
            pickle.dump(model_data, f)
        
        # Representative metric (based on 300s)
        main_metrics = metrics.get(300, list(metrics.values())[0] if metrics else {})
        
        meta = {
            "ts": time.time(),
            "model_type": model_type,
            "horizons": HORIZONS,
            "horizon_metrics": metrics,
            "features": feature_names,
            "importance": importances,
            "accuracy": main_metrics.get("accuracy", 0.0),
            "auc": main_metrics.get("auc", 0.5),
            "rows": len(X),
            "strategy_distribution": strategy_stats,
        }
        
        try:
            from app.core.io_utils import safe_write_json
            safe_write_json(self.meta_path, meta)
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("[AI_TRAIN] meta safe_write_json failed: %s", exc, exc_info=True)
        
        # Telegram notification
        try:
            msg = f"🤖 [AI] Multi-Horizon Model Trained\n"
            for h, m in metrics.items():
                msg += f"• {h}s: Acc={m['accuracy']:.1%} AUC={m['auc']:.3f} (n={m['rows']})\n"
            send_telegram(msg)
        except (KeyError, AttributeError, TypeError) as exc:
            logger.warning("[AI_TRAIN] Telegram notification failed: %s", exc, exc_info=True)
        
        return {
            "ok": True,
            "model_type": "MultiHorizon",
            "horizons": HORIZONS,
            "metrics": metrics,
            "accuracy": main_metrics.get("accuracy", 0.0),  # UI compatibility
            "auc": main_metrics.get("auc", 0.5),
            "model_path": self.model_path,
            "meta": meta,
        }

    def predict(self, features: Dict[str, float]) -> Dict[str, Any]:
        """
        Multi-horizon ensemble prediction.
        Combines each model's probability with weights to return the final ai_score.
        """
        if not _ensure_ml_imports():
            return {"ai_score": 0.5, "confidence": 0.0, "error": "ml_not_available"}
        
        np = _ml_modules["np"]
        
        if not os.path.exists(self.model_path):
            return {"ai_score": 0.5, "confidence": 0.0, "error": "no_model"}
        
        try:
            with open(self.model_path, "rb") as f:
                model_data = pickle.load(f)
        except (OSError, TypeError, ValueError):
            logger.warning("[AITrainer] predict: model load failed", exc_info=True)
            return {"ai_score": 0.5, "confidence": 0.0, "error": "load_failed"}
        
        # Check whether it is a multi-horizon model
        if isinstance(model_data, dict) and model_data.get("is_multi_horizon"):
            models = model_data["models"]
            feature_names = model_data["feature_names"]
            weights = model_data.get("weights", HORIZON_WEIGHTS)
            horizons = model_data.get("horizons", HORIZONS)
            
            # Build the feature vector
            X = np.array([[features.get(f, 0.0) for f in feature_names]])
            
            # Prediction per horizon
            probs = []
            horizon_probs = {}
            for i, horizon in enumerate(horizons):
                if horizon in models:
                    clf = models[horizon]
                    if _lgbm_available and hasattr(clf, "predict"):
                        prob = clf.predict(X)[0]
                    elif hasattr(clf, "predict_proba"):
                        prob = clf.predict_proba(X)[0, 1]
                    else:
                        continue
                    
                    w = weights[i] if i < len(weights) else 1.0 / len(horizons)
                    probs.append((prob, w))
                    horizon_probs[horizon] = float(prob)
            
            if not probs:
                return {"ai_score": 0.5, "confidence": 0.0, "error": "no_predictions"}
            
            # Weighted average
            total_weight = sum(w for _, w in probs)
            ensemble_score = sum(p * w for p, w in probs) / total_weight
            
            # Consensus-based confidence: high confidence when models agree on direction
            directions = [1 if p > 0.55 else (-1 if p < 0.45 else 0) for p, _ in probs]
            agreement = abs(sum(directions)) / len(directions) if directions else 0.0
            
            # Confidence adjustment: push score to extremes when agreement is high
            if agreement >= 0.8:
                if ensemble_score > 0.5:
                    ensemble_score = 0.5 + (ensemble_score - 0.5) * 1.3
                else:
                    ensemble_score = 0.5 - (0.5 - ensemble_score) * 1.3
                ensemble_score = max(0.1, min(0.9, ensemble_score))
            
            return {
                "ai_score": float(ensemble_score),
                "confidence": float(agreement),
                "horizon_probs": horizon_probs,
                "model_type": "multi_horizon",
            }
        
        else:
            # Legacy single model
            clf = model_data

            # Load feature_names from the metadata
            try:
                with open(self.meta_path, "r", encoding="utf-8") as f:
                    meta = json.loads(f.read())
                feature_names = meta.get("features", [])
            except (OSError, json.JSONDecodeError, KeyError, AttributeError, TypeError, ValueError):
                logger.warning("[AITrainer] predict: meta load failed", exc_info=True)
                return {"ai_score": 0.5, "confidence": 0.0, "error": "meta_load_failed"}
            
            if not feature_names:
                return {"ai_score": 0.5, "confidence": 0.0, "error": "no_feature_names"}
            
            X = np.array([[features.get(f, 0.0) for f in feature_names]])
            
            try:
                if _lgbm_available and hasattr(clf, "predict"):
                    prob = clf.predict(X)[0]
                elif hasattr(clf, "predict_proba"):
                    prob = clf.predict_proba(X)[0, 1]
                else:
                    return {"ai_score": 0.5, "confidence": 0.0, "error": "no_predict_method"}
            except (KeyError, IndexError, AttributeError, TypeError) as e:
                logger.warning("[AITrainer] predict: clf.predict failed: %s", e, exc_info=True)
                return {"ai_score": 0.5, "confidence": 0.0, "error": str(e)}
            
            return {
                "ai_score": float(prob),
                "confidence": 1.0 if abs(prob - 0.5) > 0.15 else 0.5,
                "model_type": "single",
            }

    def get_info(self) -> Dict[str, Any]:
        data = {}
        if os.path.exists(self.meta_path):
            try:
                with open(self.meta_path, "r", encoding="utf-8") as f:
                    data = json.loads(f.read())
            except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
                logger.warning("[AI_TRAIN] ai_trainer.get_info fallback: %s", exc, exc_info=True)
        
        try:
            data["retrain_interval_hours"] = float(os.getenv("OMA_AI_RETRAIN_INTERVAL_HOURS", "6.0"))
        except (KeyError, AttributeError, TypeError, ValueError):
            logger.warning("OMA_AI_RETRAIN_INTERVAL_HOURS env parse failed, using default 6.0", exc_info=True)
            data["retrain_interval_hours"] = 6.0
        return data
        
    def get_accuracy_history(self, window_hours: float = 24.0, bucket_minutes: int = 60) -> Dict[str, Any]:
        """Calculate AI accuracy over time buckets."""
        if not _ensure_ml_imports():
            return {"ok": False, "error": "pandas not installed"}
        
        pd = _ml_modules["pd"]
            
        pattern = os.path.join(self.ledger_dir, "trade_ledger.jsonl*")
        files = sorted(glob.glob(pattern))
        
        now = time.time()
        since = now - (window_hours * 3600)
        
        data = []
        for file in files:
            try:
                with open(file, "r", encoding="utf-8") as f:
                    for line in f:
                        if not line.strip(): continue
                        try:
                            rec = json.loads(line)
                            ts = float(rec.get("ts") or 0)
                            if ts < since: continue
                            
                            d = rec.get("data") or {}
                            score = d.get("ai_score")
                            price = d.get("price")
                            market = str(rec.get("market") or d.get("market") or "")
                            
                            if score is not None and price is not None and market:
                                data.append({
                                    "ts": ts,
                                    "market": market,
                                    "score": float(score),
                                    "price": float(price)
                                })
                        except (KeyError, TypeError, ValueError):
                            logger.warning("[AITrainer] get_accuracy_history: record parse failed", exc_info=True)
                            continue
            except (OSError, json.JSONDecodeError, KeyError, AttributeError, TypeError, ValueError) as exc:
                logger.warning("[AI_TRAIN] ai_trainer.get_accuracy_history fallback: %s", exc, exc_info=True)
            
        if not data:
            return {"ok": True, "history": []}
            
        df = pd.DataFrame(data)
        df = df.sort_values("ts")
        
        df["price_next"] = df.groupby("market")["price"].shift(-5)
        df["ret"] = (df["price_next"] - df["price"]) / df["price"]
        df = df.dropna(subset=["ret"])
        
        df["correct"] = ((df["score"] > 0.55) & (df["ret"] > 0.002)) | ((df["score"] < 0.45) & (df["ret"] <= 0))
        
        df["ts_bucket"] = pd.to_datetime(df["ts"], unit="s").dt.floor(f"{bucket_minutes}min")
        grouped = df.groupby("ts_bucket")["correct"].mean()
        
        high_conf_mask = (df["score"] >= 0.6) | (df["score"] <= 0.4)
        high_conf_df = df[high_conf_mask]
        high_conf_acc = 0.0
        if not high_conf_df.empty:
            high_conf_acc = high_conf_df["correct"].mean()

        history = [{"ts": int(ts.timestamp()), "acc": float(val)} for ts, val in grouped.items()]
        return {"ok": True, "history": history, "high_conf_accuracy": float(high_conf_acc), "high_conf_count": len(high_conf_df)}

    def check_and_retrain(self, threshold: float = 0.55, max_age_hours: Optional[float] = None) -> Dict[str, Any]:
        """Check recent accuracy and retrain if below threshold OR model is stale."""
        if not _ensure_ml_imports():
            return {"ok": False, "error": "pandas/sklearn not installed"}

        if max_age_hours is None:
            try:
                max_age_hours = float(os.getenv("OMA_AI_RETRAIN_INTERVAL_HOURS", "6.0"))
            except (TypeError, ValueError):
                logger.warning("[AITrainer] retrain_interval parse failed", exc_info=True)
                max_age_hours = 6.0

        info = self.get_info()
        last_ts = float(info.get("ts") or 0.0)
        age_hours = (time.time() - last_ts) / 3600.0
        is_stale = (age_hours >= max_age_hours)

        avg_acc = 1.0
        high_conf_acc = 1.0
        high_conf_count = 0

        hist = self.get_accuracy_history(window_hours=6, bucket_minutes=60)
        if hist.get("ok"):
            history = hist.get("history", [])
            if history:
                accuracies = [h["acc"] for h in history]
                if accuracies:
                    avg_acc = sum(accuracies) / len(accuracies)
                high_conf_acc = hist.get("high_conf_accuracy", 1.0)
                high_conf_count = hist.get("high_conf_count", 0)
        
        if is_stale or avg_acc < threshold or (high_conf_count >= 10 and high_conf_acc < threshold):
            res_ext = self.extract_data(days=7.0)
            if not res_ext.get("ok"):
                return {"ok": False, "step": "extract", "detail": res_ext}
                 
            res_train = self.train_model(use_time_split=True)
            return {
                "ok": True, 
                "triggered": True, 
                "reason": "stale" if is_stale else "low_accuracy",
                "avg_acc": avg_acc, 
                "high_conf_acc": high_conf_acc,
                "threshold": threshold,
                "train_result": res_train
            }
            
        return {"ok": True, "triggered": False, "avg_acc": avg_acc}


ai_trainer = AITrainer()
