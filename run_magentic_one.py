"""Compatibility entrypoint for the explicit workflow.

The previous standalone Magentic-One demo bypassed RunState and final
validation. Keep the filename for existing scripts, but route it through the
same framework-controlled entrypoint as ``main.py``.
"""

import asyncio

from main import main


if __name__ == "__main__":
    asyncio.run(main())
