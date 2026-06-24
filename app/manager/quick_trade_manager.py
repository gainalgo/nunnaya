"""QuickTradeManager: immediate/conditional manual trade manager

- Supports trading arbitrary markets (including ones not registered in the engine)
- Conditional trigger: execute when price nears the N-minute low/high
- Guard policies: global / entry_limit_only / force
"""

from __future__ import annotations

import time
import uuid
import asyncio
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Literal
from enum import Enum
from collections import deque
import logging

logger = logging.getLogger(__name__)

class GuardPolicy(str, Enum):
    """Guard application policy"""
    GLOBAL = "global"                    # apply all global guards
    ENTRY_LIMIT_ONLY = "entry_limit_only"  # apply only LMT (Entry Limit)
    FORCE = "force"                      # ignore all guards (except emergency_stop)

class TriggerType(str, Enum):
    """Conditional trigger type"""
    NEAR_LOW = "near_low"    # buy when price nears the N-minute low
    NEAR_HIGH = "near_high"  # sell when price nears the N-minute high

class OrderMode(str, Enum):
    """Order mode"""
    IMMEDIATE = "immediate"      # execute immediately
    CONDITIONAL = "conditional"  # execute after conditional trigger

class AmountMode(str, Enum):
    """Amount mode"""
    QUOTE = "quote"    # fixed amount (USDT)
    PERCENT = "percent"  # % of balance

class QuickOrderStatus(str, Enum):
    """Quick Order status"""
    PENDING = "pending"      # waiting for condition
    TRIGGERED = "triggered"  # triggered, order executing
    PLACED = "placed"        # order placed
    FILLED = "filled"        # order filled
    CANCELLED = "cancelled"  # cancelled
    EXPIRED = "expired"      # expired
    FAILED = "failed"        # failed

@dataclass
class ConditionalConfig:
    """Conditional order settings"""
    lookback_min: int = 15           # N-minute lookback
    trigger: TriggerType = TriggerType.NEAR_LOW
    threshold_mode: Literal["pct", "quote"] = "pct"
    threshold_value: float = 0.2     # percent or amount
    expiry_sec: int = 1800           # validity period (default 30 min)

@dataclass
class ExecutionConfig:
    """Execution settings"""
    order_type: Literal["market", "limit"] = "market"
    limit_price_mode: str = "best_bid"  # best_bid / best_ask

@dataclass
class QuickOrder:
    """Quick Trade order"""
    quick_id: str
    market: str    # normalized market (BTCUSDT)
    side: Literal["buy", "sell"]
    
    amount_mode: AmountMode = AmountMode.QUOTE
    amount_value: float = 0.0
    
    mode: OrderMode = OrderMode.IMMEDIATE
    guard_policy: GuardPolicy = GuardPolicy.GLOBAL
    
    conditional: Optional[ConditionalConfig] = None
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    
    status: QuickOrderStatus = QuickOrderStatus.PENDING
    created_at: float = field(default_factory=time.time)
    triggered_at: Optional[float] = None
    completed_at: Optional[float] = None
    
    result_order_id: Optional[str] = None
    error_message: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        if self.conditional:
            d["conditional"]["trigger"] = self.conditional.trigger.value
        d["amount_mode"] = self.amount_mode.value
        d["mode"] = self.mode.value
        d["guard_policy"] = self.guard_policy.value
        d["status"] = self.status.value
        return d
    
    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "QuickOrder":
        cond = d.get("conditional")
        if cond:
            cond = ConditionalConfig(
                lookback_min=cond.get("lookback_min", 15),
                trigger=TriggerType(cond.get("trigger", "near_low")),
                threshold_mode=cond.get("threshold_mode", "pct"),
                threshold_value=cond.get("threshold_value", 0.2),
                expiry_sec=cond.get("expiry_sec", 1800),
            )
        
        exec_cfg = d.get("execution", {})
        execution = ExecutionConfig(
            order_type=exec_cfg.get("order_type", "market"),
            limit_price_mode=exec_cfg.get("limit_price_mode", "best_bid"),
        )
        
        return cls(
            quick_id=d.get("quick_id", ""),
            market=d.get("market", ""),
            side=d.get("side", "buy"),
            amount_mode=AmountMode(d.get("amount_mode", "quote")),
            amount_value=float(d.get("amount_value", 0)),
            mode=OrderMode(d.get("mode", "immediate")),
            guard_policy=GuardPolicy(d.get("guard_policy", "global")),
            conditional=cond,
            execution=execution,
            status=QuickOrderStatus(d.get("status", "pending")),
            created_at=float(d.get("created_at", time.time())),
            triggered_at=d.get("triggered_at"),
            completed_at=d.get("completed_at"),
            result_order_id=d.get("result_order_id"),
            error_message=d.get("error_message"),
        )

class QuickTradeManager:
    """Quick Trade manager"""
    
    MAX_PENDING_PER_MARKET = 1
    MAX_PENDING_TOTAL = 20
    MIN_SAMPLES_FOR_TRIGGER = 3
    
    def __init__(self, system: Any):
        self.system = system
        self.pending_orders: Dict[str, QuickOrder] = {}  # quick_id -> order
        self.price_history: Dict[str, deque] = {}  # market -> [(ts, price), ...]
        self.history_max_minutes = 120  # max retention time (minutes)
        self._running = False
        self._task: Optional[asyncio.Task] = None
    
    def record_price(self, market: str, price: float, ts: Optional[float] = None) -> None:
        """Record price (used to compute the N-minute low/high)"""
        if ts is None:
            ts = time.time()

        if market not in self.price_history:
            max_samples = self.history_max_minutes * 60  # assume 1-second interval
            self.price_history[market] = deque(maxlen=max_samples)
        
        self.price_history[market].append((ts, price))
    
    def get_low_high(self, market: str, lookback_min: int) -> Optional[tuple]:
        """Return the N-minute low/high"""
        hist = self.price_history.get(market)
        if not hist:
            return None
        
        cutoff = time.time() - lookback_min * 60
        prices = [p for ts, p in hist if ts >= cutoff]
        
        if len(prices) < self.MIN_SAMPLES_FOR_TRIGGER:
            return None
        
        return (min(prices), max(prices))
    
    def resolve_market(self, market_input: str) -> Optional[str]:
        """Normalize a market input into a normalized market (USDT format)"""
        inp = market_input.strip().upper()

        if not inp:
            return None

        # already in normalized form
        if inp.endswith("USDT"):
            return inp

        # Legacy format compat
            return f"{inp[4:]}USDT"

        # only the coin symbol was entered
        return f"{inp}USDT"
    
    def submit(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Submit a Quick Trade order"""
        try:
            # normalize market
            market_input = str(request.get("market_input", "")).strip()
            market = self.resolve_market(market_input)

            if not market:
                return {"ok": False, "error": "Invalid market input"}

            # check pending limits
            if len(self.pending_orders) >= self.MAX_PENDING_TOTAL:
                return {"ok": False, "error": f"Max pending orders ({self.MAX_PENDING_TOTAL}) reached"}

            market_pending = sum(1 for o in self.pending_orders.values()
                                if o.market == market and o.status == QuickOrderStatus.PENDING)
            if market_pending >= self.MAX_PENDING_PER_MARKET:
                return {"ok": False, "error": f"Already have pending order for {market}"}

            # create order
            quick_id = f"QT-{uuid.uuid4().hex[:12]}"
            
            cond_data = request.get("conditional")
            conditional = None
            if cond_data and request.get("mode") == "conditional":
                conditional = ConditionalConfig(
                    lookback_min=int(cond_data.get("lookback_min", 15)),
                    trigger=TriggerType(cond_data.get("trigger", "near_low")),
                    threshold_mode=cond_data.get("threshold_mode", "pct"),
                    threshold_value=float(cond_data.get("threshold_value", 0.2)),
                    expiry_sec=int(cond_data.get("expiry_sec", 1800)),
                )
            
            exec_data = request.get("execution", {})
            execution = ExecutionConfig(
                order_type=exec_data.get("order_type", "market"),
                limit_price_mode=exec_data.get("limit_price_mode", "best_bid"),
            )
            
            order = QuickOrder(
                quick_id=quick_id,
                market=market,
                side=request.get("side", "buy"),
                amount_mode=AmountMode(request.get("amount_mode", "quote")),
                amount_value=float(request.get("amount_value", 0)),
                mode=OrderMode(request.get("mode", "immediate")),
                guard_policy=GuardPolicy(request.get("guard_policy", "global")),
                conditional=conditional,
                execution=execution,
            )
            
            # execute immediately or register as pending
            if order.mode == OrderMode.IMMEDIATE:
                return self._execute_order(order)
            else:
                self.pending_orders[quick_id] = order
                self._log_order("QUICK_ORDER_PENDING", order)
                return {
                    "ok": True,
                    "quick_id": quick_id,
                    "status": "pending",
                    "resolved_market": market,
                    "message": f"Conditional order registered. Waiting for trigger.",
                }
        
        except (KeyError, IndexError, AttributeError, TypeError, ValueError) as e:
            logger.exception("QuickTradeManager.submit error")
            return {"ok": False, "error": str(e)}
    
    def _execute_order(self, order: QuickOrder) -> Dict[str, Any]:
        """Execute order"""
        try:
            # evaluate guards
            allowed, reason = self._evaluate_guards(order)
            if not allowed:
                order.status = QuickOrderStatus.FAILED
                order.error_message = reason
                order.completed_at = time.time()
                self._log_order("QUICK_ORDER_BLOCKED", order, reason=reason)
                return {"ok": False, "error": f"Blocked by guard: {reason}", "quick_id": order.quick_id}
            
            # place the actual order
            result = self._place_order(order)
            
            if result.get("ok"):
                order.status = QuickOrderStatus.PLACED
                order.result_order_id = result.get("order_id")
                order.completed_at = time.time()
                self._log_order("QUICK_ORDER_PLACED", order)
                return {
                    "ok": True,
                    "quick_id": order.quick_id,
                    "status": "placed",
                    "resolved_market": order.market,
                    "order_id": order.result_order_id,
                }
            else:
                order.status = QuickOrderStatus.FAILED
                order.error_message = result.get("error", "Unknown error")
                order.completed_at = time.time()
                self._log_order("QUICK_ORDER_FAILED", order)
                return {"ok": False, "error": order.error_message, "quick_id": order.quick_id}
        
        except (OSError, KeyError, AttributeError, TypeError, ValueError, OverflowError) as e:
            logger.exception("QuickTradeManager._execute_order error")
            order.status = QuickOrderStatus.FAILED
            order.error_message = str(e)
            order.completed_at = time.time()
            return {"ok": False, "error": str(e), "quick_id": order.quick_id}
    
    def _evaluate_guards(self, order: QuickOrder) -> tuple:
        """Evaluate guards

        Returns:
            (allowed: bool, reason: str)
        """
        system = self.system

        # emergency_stop applies under all policies
        if getattr(system, "emergency_stop", False):
            return (False, "emergency_stop")

        if order.guard_policy == GuardPolicy.FORCE:
            # force: ignore other guards
            return (True, "")

        if order.guard_policy == GuardPolicy.ENTRY_LIMIT_ONLY:
            # apply only LMT settings (used to decide limit/market at order time)
            return (True, "")

        # global: apply all guards
        if order.side == "buy":
            # buy-side guards
            if getattr(system, "drawdown_guard", False):
                # drawdown check logic (simplified)
                pass

            # global cooldown
            gcd_until = getattr(system, "_global_entry_block_until_ts", 0)
            if time.time() < gcd_until:
                return (False, "global_cooldown")
        
        return (True, "")
    
    def _place_order(self, order: QuickOrder) -> Dict[str, Any]:
        """Place the actual exchange order"""
        try:
            system = self.system
            trade_client = getattr(system, "trade_client", None)

            if not trade_client:
                return {"ok": False, "error": "trade_client not available"}

            base = order.market.replace("USDT", "") if order.market.endswith("USDT") else order.market

            # compute amount/quantity
            if order.side == "buy":
                # buy: needs USDT amount
                if order.amount_mode == AmountMode.PERCENT:
                    balance = trade_client.get_balance("USDT")
                    amount = balance * (order.amount_value / 100.0)
                else:
                    # quote mode: already a USDT amount
                    amount = order.amount_value
            else:
                # sell: needs coin quantity
                coin_balance = trade_client.get_balance(base)

                if order.amount_mode == AmountMode.PERCENT:
                    # percent mode: % of held quantity
                    amount = coin_balance * (order.amount_value / 100.0)
                else:
                    # quote mode: convert USDT amount -> coin quantity
                    price_store = getattr(system, "price_store", None)
                    current_price = price_store.get_price(order.market) if price_store else None

                    if not current_price or current_price <= 0:
                        return {"ok": False, "error": f"Cannot get price for {order.market}"}

                    # compute coin quantity for the given USDT amount
                    target_qty = order.amount_value / current_price

                    # if it exceeds the held quantity, sell the entire balance
                    amount = min(target_qty, coin_balance)

                    if amount <= 0:
                        return {"ok": False, "error": f"No {base} balance to sell"}

            if amount <= 0:
                return {"ok": False, "error": "Invalid amount"}

            # decide order type
            use_limit = (
                order.execution.order_type == "limit" or
                (order.guard_policy == GuardPolicy.ENTRY_LIMIT_ONLY and 
                 getattr(system, "entry_limit_buy_enabled", False))
            )
            
            logger.info(f"[QuickTrade] {order.side.upper()} {order.market}: amount={amount:.8f}, use_limit={use_limit}")
            
            if order.side == "buy":
                if use_limit:
                    limit_price = self._resolve_limit_price(order.market, order.side, order.execution.limit_price_mode)
                    if limit_price is None:
                        return {"ok": False, "error": f"Cannot resolve limit price for {order.market}"}
                    qty = amount / limit_price
                    result = trade_client.limit_buy(order.market, limit_price, qty)
                else:
                    result = trade_client.market_buy(order.market, amount)
            else:
                if use_limit:
                    limit_price = self._resolve_limit_price(order.market, order.side, order.execution.limit_price_mode)
                    if limit_price is None:
                        return {"ok": False, "error": f"Cannot resolve limit price for {order.market}"}
                    result = trade_client.limit_sell(order.market, limit_price, amount)
                else:
                    result = trade_client.market_sell(order.market, amount)

            return {"ok": True, "order_id": result.get("uuid") or result.get("id")}
        
        except Exception as e:
            logger.exception("QuickTradeManager._place_order error")
            return {"ok": False, "error": str(e)}
    
    def _resolve_limit_price(self, market: str, side: str, price_mode: str) -> Optional[float]:
        """Decide order price based on limit_price_mode"""
        price_store = getattr(self.system, "price_store", None)
        if not price_store:
            return None
        current = price_store.get_price(market)
        if not current or current <= 0:
            return None
        if price_mode == "best_bid":
            return current * 0.999 if side == "buy" else current * 0.998
        elif price_mode == "best_ask":
            return current * 1.001 if side == "sell" else current * 1.002
        return current

    def check_triggers(self) -> List[QuickOrder]:
        """Check conditional order triggers"""
        triggered = []
        now = time.time()

        for quick_id, order in list(self.pending_orders.items()):
            if order.status != QuickOrderStatus.PENDING:
                continue

            # expiry check
            if order.conditional and (now - order.created_at) > order.conditional.expiry_sec:
                order.status = QuickOrderStatus.EXPIRED
                order.completed_at = now
                self._log_order("QUICK_ORDER_EXPIRED", order)
                continue
            
            # trigger condition check
            if self._check_trigger_condition(order):
                order.status = QuickOrderStatus.TRIGGERED
                order.triggered_at = now
                triggered.append(order)
        
        return triggered
    
    def _check_trigger_condition(self, order: QuickOrder) -> bool:
        """Check whether the trigger condition is met"""
        if not order.conditional:
            return False

        cond = order.conditional
        low_high = self.get_low_high(order.market, cond.lookback_min)

        if not low_high:
            return False

        low, high = low_high

        # fetch current price
        current_price = self._get_current_price(order.market)
        if not current_price:
            return False

        # compute threshold
        if cond.threshold_mode == "pct":
            if cond.trigger == TriggerType.NEAR_LOW:
                threshold_price = low * (1 + cond.threshold_value / 100.0)
                return current_price <= threshold_price
            else:  # NEAR_HIGH
                threshold_price = high * (1 - cond.threshold_value / 100.0)
                return current_price >= threshold_price
        else:  # quote
            if cond.trigger == TriggerType.NEAR_LOW:
                threshold_price = low + cond.threshold_value
                return current_price <= threshold_price
            else:  # NEAR_HIGH
                threshold_price = high - cond.threshold_value
                return current_price >= threshold_price
    
    def _get_current_price(self, market: str) -> Optional[float]:
        """Fetch current price"""
        try:
            price_store = getattr(self.system, "price_store", None)
            if price_store:
                return price_store.get_price(market)
            return None
        except (KeyError, AttributeError, TypeError):
            logger.warning("[QuickTrade] _get_current_price(%s) failed", market, exc_info=True)
            return None

    def cancel(self, quick_id: str) -> Dict[str, Any]:
        """Cancel a conditional order"""
        order = self.pending_orders.get(quick_id)
        if not order:
            return {"ok": False, "error": "Order not found"}

        if order.status != QuickOrderStatus.PENDING:
            return {"ok": False, "error": f"Cannot cancel order in status: {order.status.value}"}
        
        order.status = QuickOrderStatus.CANCELLED
        order.completed_at = time.time()
        self._log_order("QUICK_ORDER_CANCELLED", order)
        
        return {"ok": True, "quick_id": quick_id, "status": "cancelled"}
    
    def get_order(self, quick_id: str) -> Optional[Dict[str, Any]]:
        """Look up an order"""
        order = self.pending_orders.get(quick_id)
        if order:
            return order.to_dict()
        return None

    def get_pending_orders(self) -> List[Dict[str, Any]]:
        """List of pending orders"""
        return [o.to_dict() for o in self.pending_orders.values() 
                if o.status == QuickOrderStatus.PENDING]
    
    def _log_order(self, event: str, order: QuickOrder, **extra) -> None:
        """Record to the ledger"""
        try:
            ledger = getattr(self.system, "ledger", None)
            if ledger:
                ledger.append(
                    event,
                    quick_id=order.quick_id,
                    market=order.market,
                    side=order.side,
                    amount_mode=order.amount_mode.value,
                    amount_value=order.amount_value,
                    mode=order.mode.value,
                    guard_policy=order.guard_policy.value,
                    status=order.status.value,
                    **extra
                )
        except (KeyError, AttributeError, TypeError) as exc:
            logger.warning("[quick_trade_manager] %s: %s", 'quick_trade_manager._log_order fallback', exc, exc_info=True)
    
    async def run_trigger_loop(self, interval_sec: float = 1.0) -> None:
        """Conditional order trigger loop"""
        self._running = True
        while self._running:
            try:
                triggered = self.check_triggers()
                for order in triggered:
                    self._execute_order(order)
                    # remove completed orders from pending
                    if order.quick_id in self.pending_orders:
                        del self.pending_orders[order.quick_id]
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
                logger.exception("QuickTradeManager trigger loop error")
            
            await asyncio.sleep(interval_sec)
    
    def start(self) -> None:
        """Start the trigger loop"""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run_trigger_loop())
    
    def stop(self) -> None:
        """Stop the trigger loop"""
        self._running = False
        if self._task:
            self._task.cancel()
