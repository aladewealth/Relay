"""ASGI entry for Vercel, Render, Railway or any host: exports a top-level FastAPI `app`.

  Vercel:  zero-config, picks up `app` from this file.
  Render:  uvicorn app:app --host 0.0.0.0 --port $PORT
  Local:   python app.py
The Hugging Face Space entry (Gradio/ZeroGPU) lives in space_app.py.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from relay.api import create_app  # noqa: E402

app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
