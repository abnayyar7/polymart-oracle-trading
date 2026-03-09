
import logging
import sys
from pathlib import Path

# Setup logging to console to see the output
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s")
logger = logging.getLogger("TEST")

# Add the current directory to sys.path to allow importing tools
sys.path.append(str(Path.cwd()))

try:
    from tools.paper_trader import check_and_resolve_positions
    logger.info("Running check_and_resolve_positions()...")
    results = check_and_resolve_positions()
    logger.info(f"Scan complete. Resolved {len(results)} positions.")
except Exception as e:
    logger.error(f"Test failed: {e}")
