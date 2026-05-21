"""Scheduler worker process entry point."""

import asyncio
import logging

from src.orchestrator.engine import OrchestrationEngine


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    engine = OrchestrationEngine()
    try:
        asyncio.run(engine.start())
    except KeyboardInterrupt:
        engine.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
