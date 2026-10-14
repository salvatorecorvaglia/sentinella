"""Serving Sentinella over HTTP and WebSockets.

The shared FastAPI app factory (``api``), the WebSocket fan-out
(``broadcast``) and the background-thread runner the TUI uses (``runner``).
Kept out of ``sentinella.core`` so collection and the one-shot commands don't
import FastAPI or uvicorn.
"""

__all__: list[str] = []
