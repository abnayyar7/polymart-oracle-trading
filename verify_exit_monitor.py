
import logging
import sys
import json
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

# Configure logging to stdout
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s")
logger = logging.getLogger("VERIFY")

# Mock the tools before importing exit_manager
sys.modules["tools.memory"] = MagicMock()
sys.modules["tools.execution_router"] = MagicMock()
sys.modules["tools.paper_trader"] = MagicMock()
sys.modules["tools.polymarket_tools"] = MagicMock()
sys.modules["tools.telegram"] = MagicMock()

from tools.exit_manager import ExitMonitor, log_shadow_exits, run_exit_checks

class MockOracle:
    def __init__(self, config):
        self.config = config
        self._pending_exits = {}

    def monitor_mock(self, open_positions, current_market_data):
        # 1. TTL Cleanup (Simplified)
        now = time.time()
        expired = [tid for tid, ts in self._pending_exits.items() if now - ts > 600]
        for tid in expired:
            logger.info("[MONITOR] TTL expired for %s, clearing cooldown.", tid)
            del self._pending_exits[tid]
            
        # 2. Check exits
        exit_actions = run_exit_checks(current_market_data, self.config)
        
        executed = []
        for action in exit_actions:
            trade_id = action["bet_id"]
            if trade_id in self._pending_exits:
                logger.info("[MONITOR] Skip repeat attempt for %s", trade_id)
                continue
                
            logger.info("[MONITOR] Attempting exit for %s", trade_id)
            self._pending_exits[trade_id] = time.time()
            executed.append(trade_id)
        return executed

def verify():
    logger.info("--- VERIFICATION START ---")
    
    config = {"betting": {"auto_exit": True, "exit_score_threshold": 70}}
    pos = {
        "trade_id": "T1", "market_id": "M1", "side": "YES", 
        "entry_price": 0.50, "exit_target_price": 0.85, "mode": "paper"
    }

    # Scenario 1: High score + Wide spread (0.20 > 0.15 hard guard)
    market_wide = {
        "condition_id": "M1", "yes_price": 0.91, "no_price": 0.09,
        "spread": 0.20, "days_to_resolution": 0.5
    }
    
    monitor = ExitMonitor(config)
    analysis = monitor.calculate_exit_score(pos, market_wide)
    logger.info(f"WIDE SPREAD: score={analysis['score']} should_exit={analysis['should_exit']} reasons={analysis['reasons']}")

    # Scenario 2: High score + Normal spread (0.05 < 0.15)
    market_normal = {
        "condition_id": "M1", "yes_price": 0.91, "no_price": 0.09,
        "spread": 0.05, "days_to_resolution": 0.5
    }
    analysis_ok = monitor.calculate_exit_score(pos, market_normal)
    logger.info(f"NORMAL SPREAD: score={analysis_ok['score']} should_exit={analysis_ok['should_exit']}")

    # Scenario 3: Cooldown & TTL
    oracle = MockOracle(config)
    
    with patch("tools.execution_router.get_current_mode", return_value="paper"), \
         patch("tools.memory.get_open_bets", return_value=[{**pos, "id": "T1"}]), \
         patch("time.time") as mock_time:
        
        start_t = 1000.0
        mock_time.return_value = start_t
        
        logger.info("First attempt (normal spread)...")
        executed = oracle.monitor_mock([pos], [market_normal])
        logger.info(f"Executed: {executed} | Pending: {list(oracle._pending_exits.keys())}")
        
        logger.info("Second attempt (same time, should skip)...")
        executed2 = oracle.monitor_mock([pos], [market_normal])
        logger.info(f"Executed: {executed2}")
        
        logger.info("Third attempt (after 11 minutes, should trigger TTL cleanup)...")
        mock_time.return_value = start_t + 660.0
        executed3 = oracle.monitor_mock([pos], [market_normal])
        logger.info(f"Executed: {executed3}")

    logger.info("--- VERIFICATION COMPLETE ---")

if __name__ == "__main__":
    verify()
