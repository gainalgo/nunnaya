"""
Ledger validator - ensures data integrity
- BUY/SELL pair validation (unmatched trades)
- duplicate trade detection
- chronological order validation
- negative qty/price detection
- position mismatch detection
"""
import json
from pathlib import Path
from typing import Dict, List, Any, Set, Tuple, Optional
from collections import defaultdict
import logging

logger = logging.getLogger(__name__)

class LedgerValidator:
    """Trade Ledger validation"""
    
    def __init__(self, ledger_file: Path = Path("runtime/trade_ledger.jsonl")):
        self.ledger_file = ledger_file
        self.issues: List[str] = []
    
    def validate_all(self) -> Dict[str, Any]:
        """Validate the entire ledger"""
        self.issues.clear()
        
        results = {
            "ok": True,
            "issues": [],
            "summary": {
                "total_lines": 0,
                "unpaired_buys": 0,
                "unpaired_sells": 0,
                "duplicates": 0,
                "time_anomalies": 0,
                "negative_values": 0,
                "parse_errors": 0,
            }
        }
        
        if not self.ledger_file.exists():
            results["issues"].append("Ledger file not found")
            results["ok"] = False
            return results
        
        try:
            trades = self._load_trades()
            results["summary"]["total_lines"] = len(trades)
            
            # 1. BUY/SELL pair validation
            unpaired = self._check_trade_pairs(trades)
            results["summary"]["unpaired_buys"] = len(unpaired["buys"])
            results["summary"]["unpaired_sells"] = len(unpaired["sells"])

            # 2. duplicate trade detection
            duplicates = self._check_duplicates(trades)
            results["summary"]["duplicates"] = len(duplicates)

            # 3. chronological order validation
            time_issues = self._check_time_order(trades)
            results["summary"]["time_anomalies"] = len(time_issues)

            # 4. negative value validation
            negative_issues = self._check_negative_values(trades)
            results["summary"]["negative_values"] = len(negative_issues)
            
            results["issues"] = self.issues
            results["ok"] = len(self.issues) == 0
            
        except (KeyError, AttributeError, TypeError) as e:
            results["issues"].append(f"Validation error: {e}")
            results["ok"] = False
        
        return results
    
    def _load_trades(self) -> List[Dict[str, Any]]:
        """Load the ledger file"""
        trades = []
        parse_errors = 0
        
        with open(self.ledger_file, "r", encoding="utf-8", errors="ignore") as f:
            for i, line in enumerate(f, start=1):
                if not line.strip():
                    continue
                
                try:
                    data = json.loads(line)
                    if isinstance(data, dict):
                        data["_line_no"] = i
                        trades.append(data)
                except json.JSONDecodeError:
                    parse_errors += 1
                    if parse_errors <= 5:  # log at most 5
                        self.issues.append(f"Line {i}: JSON parse error")
        
        if parse_errors > 5:
            self.issues.append(f"... and {parse_errors - 5} more parse errors")
        
        return trades
    
    def _check_trade_pairs(self, trades: List[Dict[str, Any]]) -> Dict[str, List[str]]:
        """BUY/SELL pair validation"""
        # track BUY/SELL per market
        market_positions: Dict[str, float] = defaultdict(float)
        unpaired_buys: Set[str] = set()
        unpaired_sells: Set[str] = set()
        
        for trade in trades:
            event = trade.get("event", "")
            if event not in ["TRADE_BUY", "TRADE_SELL"]:
                continue
            
            market = trade.get("market", "")
            qty = float(trade.get("data", {}).get("qty", 0) or 0)
            
            if not market or qty <= 0:
                continue
            
            if event == "TRADE_BUY":
                market_positions[market] += qty
                if market_positions[market] > 0:
                    unpaired_buys.add(market)
            
            elif event == "TRADE_SELL":
                market_positions[market] -= qty
                if market_positions[market] < -0.0001:  # tolerance
                    unpaired_sells.add(market)
                    self.issues.append(
                        f"Unpaired SELL: {market} (position: {market_positions[market]:.8f})"
                    )
                elif abs(market_positions[market]) < 0.0001:
                    # position is flat
                    unpaired_buys.discard(market)

        # check remaining uncleared BUYs
        for market in unpaired_buys:
            if market_positions[market] > 0.0001:
                self.issues.append(
                    f"Unpaired BUY: {market} (remaining: {market_positions[market]:.8f})"
                )
        
        return {
            "buys": list(unpaired_buys),
            "sells": list(unpaired_sells)
        }
    
    def _check_duplicates(self, trades: List[Dict[str, Any]]) -> List[Tuple[int, int]]:
        """Duplicate trade detection (UUID-based)"""
        seen_uuids: Dict[str, int] = {}
        duplicates = []
        
        for trade in trades:
            event = trade.get("event", "")
            if event not in ["TRADE_BUY", "TRADE_SELL"]:
                continue
            
            uuid = trade.get("data", {}).get("uuid")
            if not uuid:
                continue
            
            line_no = trade.get("_line_no", 0)
            
            if uuid in seen_uuids:
                duplicates.append((seen_uuids[uuid], line_no))
                self.issues.append(
                    f"Duplicate UUID: {uuid} (lines {seen_uuids[uuid]}, {line_no})"
                )
            else:
                seen_uuids[uuid] = line_no
        
        return duplicates
    
    def _check_time_order(self, trades: List[Dict[str, Any]]) -> List[int]:
        """Chronological order validation"""
        prev_ts = 0.0
        time_issues = []
        
        for trade in trades:
            ts = float(trade.get("ts", 0) or 0)
            if ts < prev_ts - 1.0:  # allow 1s backwards (timestamp precision)
                line_no = trade.get("_line_no", 0)
                time_issues.append(line_no)
                self.issues.append(
                    f"Line {line_no}: Time went backwards ({prev_ts:.2f} → {ts:.2f})"
                )
            prev_ts = max(prev_ts, ts)
        
        return time_issues
    
    def _check_negative_values(self, trades: List[Dict[str, Any]]) -> List[int]:
        """Negative qty/price validation"""
        negative_issues = []
        
        for trade in trades:
            event = trade.get("event", "")
            if event not in ["TRADE_BUY", "TRADE_SELL"]:
                continue
            
            data = trade.get("data", {})
            qty = float(data.get("qty", 0) or 0)
            price = float(data.get("price", 0) or 0)
            value_usdt = float(data.get("value_usdt", 0) or 0)
            
            line_no = trade.get("_line_no", 0)
            
            if qty < 0:
                negative_issues.append(line_no)
                self.issues.append(f"Line {line_no}: Negative qty {qty}")
            
            if price < 0:
                negative_issues.append(line_no)
                self.issues.append(f"Line {line_no}: Negative price {price}")
            
            if value_usdt < 0:
                negative_issues.append(line_no)
                self.issues.append(f"Line {line_no}: Negative value_usdt {value_usdt}")
        
        return negative_issues

class HoldingSyncValidator:
    """Context-Holding sync validation"""
    
    def __init__(self, system):
        self.system = system
        self.issues: List[str] = []
    
    def validate(self) -> Dict[str, Any]:
        """Context.position vs Bybit.balance validation"""
        self.issues.clear()
        
        results = {
            "ok": True,
            "issues": [],
            "mismatches": [],
            "total_checked": 0,
        }
        
        try:
            # validate ACTIVE markets only
            markets = self.system.get_markets()
            active_markets = [
                m for m in markets 
                if m.get("state") == "ACTIVE"
            ]
            
            results["total_checked"] = len(active_markets)
            
            for market_info in active_markets:
                market = market_info.get("market", "")
                if not market:
                    continue
                
                # Context position
                ctx = self.system.get_context(market)
                ctx_qty = float(ctx.get("position", {}).get("qty", 0) or 0)

                # Bybit balance
                try:
                    balance = self.system.query_client.get_balance(market)
                    exchange_qty = float(balance.get("balance", 0) or 0)
                except Exception as exc:
                    logger.warning("[ledger_validator] %s: %s", f"{market}: Failed to query balance", exc, exc_info=True)
                    self.issues.append(f"{market}: Failed to query balance ({exc})")
                    continue
                
                # tolerance: 0.0001 (4 decimal places)
                diff = abs(ctx_qty - exchange_qty)
                if diff > 0.0001:
                    mismatch = {
                        "market": market,
                        "context_qty": ctx_qty,
                        "exchange_qty": exchange_qty,
                        "diff": diff,
                    }
                    results["mismatches"].append(mismatch)
                    self.issues.append(
                        f"{market}: Position mismatch "
                        f"(Context: {ctx_qty:.8f}, Bybit: {exchange_qty:.8f}, "
                        f"Diff: {diff:.8f})"
                    )
            
            results["issues"] = self.issues
            results["ok"] = len(self.issues) == 0
            
        except Exception as e:
            results["issues"].append(f"Validation error: {e}")
            results["ok"] = False
        
        return results

def validate_ledger() -> Dict[str, Any]:
    """Validate the ledger (convenience function)"""
    validator = LedgerValidator()
    return validator.validate_all()

def validate_holding_sync(system) -> Dict[str, Any]:
    """Validate position sync (convenience function)"""
    validator = HoldingSyncValidator(system)
    return validator.validate()
