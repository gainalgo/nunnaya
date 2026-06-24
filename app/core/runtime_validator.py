"""
Automatic validation and cleanup of runtime state
- Runs automatically on server startup
- Detects/removes invalid market formats
- Detects Context-OMA budget mismatches
- Cleans up zombie states
"""
import json
import re
from pathlib import Path
from typing import Dict, List, Tuple, Any
import logging

logger = logging.getLogger(__name__)


class RuntimeValidator:
    """Runtime state validation and automatic cleanup"""
    
    def __init__(self, runtime_dir: Path = Path("runtime")):
        self.runtime_dir = runtime_dir
        self.issues: List[str] = []
        self.fixes: List[str] = []
    
    def validate_all(self, auto_fix: bool = False) -> Dict[str, Any]:
        """Validate all runtime files"""
        self.issues.clear()
        self.fixes.clear()
        
        results = {
            "ok": True,
            "issues": [],
            "fixes": [],
            "summary": {}
        }
        
        # 1. Validate Context state
        context_issues = self._validate_context_state(auto_fix)
        results["summary"]["context"] = len(context_issues)

        # 2. Validate OMA state
        oma_issues = self._validate_oma_state(auto_fix)
        results["summary"]["oma"] = len(oma_issues)

        # 3. Validate LongHold config
        longhold_issues = self._validate_longhold_config(auto_fix)
        results["summary"]["longhold"] = len(longhold_issues)

        # 4. Validate Trade Ledger
        ledger_issues = self._validate_trade_ledger(auto_fix)
        results["summary"]["ledger"] = len(ledger_issues)
        
        results["issues"] = self.issues
        results["fixes"] = self.fixes
        results["ok"] = len(self.issues) == 0
        
        return results
    
    def _validate_context_state(self, auto_fix: bool) -> List[str]:
        """Validate Context state"""
        issues = []
        context_file = self.runtime_dir / "context_state.json"
        
        if not context_file.exists():
            return issues
        
        try:
            with open(context_file, "r", encoding="utf-8") as f:
                ctx = json.load(f)
            
            markets = ctx.get("oma", {})
            fixed_markets = {}
            
            for market, data in markets.items():
                # Validate market format (XXX/USDT or XXXUSDT)
                if not self._is_valid_market(market):
                    issues.append(f"Invalid market format: {market}")
                    self.issues.append(f"Context: invalid market format {market}")
                    continue

                # Validate negative budget
                budget = data.get("budget_usdt", 0)
                if budget < 0:
                    issues.append(f"{market}: negative budget {budget}")
                    self.issues.append(f"Context: {market} negative budget {budget}")
                    if auto_fix:
                        data["budget_usdt"] = 0
                        self.fixes.append(f"Context: {market} budget reset to 0")

                # Validate NaN/Inf values
                for key, value in data.items():
                    if isinstance(value, float):
                        if not self._is_finite(value):
                            issues.append(f"{market}.{key}: non-finite value {value}")
                            self.issues.append(f"Context: {market}.{key} = {value} (invalid)")
                            if auto_fix:
                                data[key] = 0.0
                                self.fixes.append(f"Context: {market}.{key} → 0.0")
                
                fixed_markets[market] = data
            
            # Auto-fix: save cleaned data
            if auto_fix and self.fixes:
                ctx["oma"] = fixed_markets
                backup_file = context_file.with_suffix(".json.bak")
                context_file.rename(backup_file)
                from app.core.io_utils import safe_write_json
                safe_write_json(str(context_file), ctx)
                logger.info(f"Context state fixed: {len(self.fixes)} changes")
        
        except (OSError, json.JSONDecodeError, KeyError, AttributeError, TypeError, ValueError) as e:
            issues.append(f"Context validation error: {e}")
            self.issues.append(f"Context file parse error: {e}")
        
        return issues
    
    def _validate_oma_state(self, auto_fix: bool) -> List[str]:
        """Validate OMA state"""
        issues = []
        oma_file = self.runtime_dir / "oma_state.json"
        
        if not oma_file.exists():
            return issues
        
        try:
            with open(oma_file, "r", encoding="utf-8") as f:
                oma = json.load(f)
            
            states = oma.get("states", {})
            fixed_states = {}
            
            for market, state in states.items():
                if not self._is_valid_market(market):
                    issues.append(f"OMA: Invalid market {market}")
                    self.issues.append(f"OMA: invalid market format {market}")
                    continue

                # Validate state value
                valid_states = ["ACTIVE", "WATCH", "RECOVERY", "DISABLED", "READY", "WARMING"]
                current_state = state.get("state")
                if current_state not in valid_states:
                    issues.append(f"OMA {market}: invalid state {current_state}")
                    self.issues.append(f"OMA: {market} invalid state {current_state}")
                    if auto_fix:
                        state["state"] = "DISABLED"
                        self.fixes.append(f"OMA: {market} → DISABLED")
                
                fixed_states[market] = state
            
            if auto_fix and self.fixes:
                oma["states"] = fixed_states
                backup_file = oma_file.with_suffix(".json.bak")
                oma_file.rename(backup_file)
                from app.core.io_utils import safe_write_json
                safe_write_json(str(oma_file), oma)
                logger.info(f"OMA state fixed: {len(self.fixes)} changes")
        
        except (OSError, json.JSONDecodeError, KeyError, AttributeError, TypeError, ValueError) as e:
            issues.append(f"OMA validation error: {e}")
            self.issues.append(f"OMA file parse error: {e}")
        
        return issues
    
    def _validate_longhold_config(self, auto_fix: bool) -> List[str]:
        """Validate LongHold config"""
        issues = []
        lh_file = self.runtime_dir / "longhold_config.json"
        
        if not lh_file.exists():
            return issues
        
        try:
            with open(lh_file, "r", encoding="utf-8") as f:
                lh = json.load(f)
            
            markets = lh.get("markets", {})
            fixed_markets = {}
            
            for market, cfg in markets.items():
                if not self._is_valid_market(market):
                    issues.append(f"LongHold: Invalid market {market}")
                    self.issues.append(f"LongHold: invalid market format {market}")
                    continue

                # Validate target_profit_pct range (0-1000%)
                target = cfg.get("target_profit_pct", 0)
                if target < 0 or target > 1000:
                    issues.append(f"LongHold {market}: invalid target {target}%")
                    self.issues.append(f"LongHold: {market} target profit out of range {target}%")
                    if auto_fix:
                        cfg["target_profit_pct"] = max(0, min(1000, target))
                        self.fixes.append(f"LongHold: {market} target profit clipped")
                
                fixed_markets[market] = cfg
            
            if auto_fix and self.fixes:
                lh["markets"] = fixed_markets
                backup_file = lh_file.with_suffix(".json.bak")
                lh_file.rename(backup_file)
                from app.core.io_utils import safe_write_json
                safe_write_json(str(lh_file), lh)
                logger.info(f"LongHold config fixed: {len(self.fixes)} changes")
        
        except (OSError, json.JSONDecodeError, KeyError, AttributeError, TypeError, ValueError) as e:
            issues.append(f"LongHold validation error: {e}")
            self.issues.append(f"LongHold file parse error: {e}")
        
        return issues
    
    def _validate_trade_ledger(self, auto_fix: bool) -> List[str]:
        """Validate Trade Ledger (last 100 lines)"""
        issues = []
        ledger_file = self.runtime_dir / "trade_ledger.jsonl"
        
        if not ledger_file.exists():
            return issues
        
        try:
            lines = ledger_file.read_text(encoding="utf-8", errors="ignore").split("\n")
            valid_lines = []
            parse_errors = 0
            
            for i, line in enumerate(lines[-100:], start=len(lines)-100):
                if not line.strip():
                    continue
                
                try:
                    data = json.loads(line)
                    # Validate required fields
                    if not isinstance(data, dict):
                        parse_errors += 1
                        issues.append(f"Ledger line {i}: not a dict")
                        continue
                    
                    valid_lines.append(line)
                except json.JSONDecodeError as e:
                    parse_errors += 1
                    issues.append(f"Ledger line {i}: JSON parse error")
                    self.issues.append(f"Ledger: line {i} parse failed")

            if parse_errors > 0:
                logger.warning("Trade ledger: %s parse errors in last 100 lines", parse_errors)
                self.issues.append(f"Ledger: {parse_errors} parse failures in last 100 lines")
        
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
            issues.append(f"Ledger validation error: {e}")
            self.issues.append(f"Ledger validation error: {e}")
        
        return issues
    
    @staticmethod
    def _is_valid_market(market: str) -> bool:
        """Validate market format (XXXUSDT, XXX/USDT, legacy format)"""
        if not market or not isinstance(market, str):
            return False

        # XXXUSDT linear perp format
        if re.match(r"^[A-Z0-9]{2,10}USDT$", market):
            return True

        # XXX/USDT ccxt format
        if re.match(r"^[A-Z0-9]{2,10}/USDT$", market):
            return True

        # Legacy format (backward compatible)
        if re.match(r"^[A-Z]{3,4}-[A-Z0-9]{2,10}$", market):
            return True

        return False
    
    @staticmethod
    def _is_finite(value: float) -> bool:
        """Validate that the value is a finite number"""
        import math
        return math.isfinite(value)


# Runs automatically on server startup
def validate_on_startup(auto_fix: bool = True) -> Dict[str, Any]:
    """Automatic validation & fix on server startup"""
    validator = RuntimeValidator()
    results = validator.validate_all(auto_fix=auto_fix)
    
    if results["issues"]:
        logger.warning(f"Runtime validation: {len(results['issues'])} issues found")
        for issue in results["issues"][:10]:  # log at most 10
            logger.warning("  - %s", issue)
    
    if results["fixes"]:
        logger.info(f"Runtime validation: {len(results['fixes'])} fixes applied")
        for fix in results["fixes"][:10]:
            logger.info("  - %s", fix)
    
    return results
