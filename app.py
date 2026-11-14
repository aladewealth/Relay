"""Hugging Face Space entry (Gradio SDK).

Free Gradio hosting for this account runs on ZeroGPU hardware (cpu-basic Gradio now needs PRO), so
this follows the ZeroGPU pattern: launch a Gradio app, then put Relay's FastAPI routes in front of it.
  /            static dashboard (vanilla JS)
  /api/*       Relay REST API (FastAPI) · /api/docs OpenAPI docs
  /gradio_api  Gradio API for the small console (`run` endpoint)
Inference (SubQ over HTTPS, llama.cpp on CPU) never touches the GPU.

Locally / anywhere else: `python app.py --uvicorn` serves the plain FastAPI app (with the console at /gradio).
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:  # ZeroGPU hardware expects at least one GPU-decorated function; Relay itself runs on CPU.
    import spaces

    @spaces.GPU(duration=10)
    def _gpu_probe():
        return True
except Exception:  # not on ZeroGPU
    pass

import gradio as gr  # noqa: E402

from relay import pipeline  # noqa: E402
from relay.api import _warm_generator, create_app  # noqa: E402

api_app = create_app()


def run_pipeline(name, text):
    r = pipeline.process_message(name or "Gradio user", None, "gradio", text, wait_draft=True)
    c = r["analysis"]["classification"]
    summary = {"category": c["category"]["value"], "priority": c["priority"]["label"],
               "department": r["routing"]["department"], "escalated": r["escalated"],
               "reasons": r["escalation_reasons"], "source": r["source"], "latency_ms": r["latency_ms"]}
    return summary, (r["draft"] or {}).get("text") or "(no draft: see dashboard)"


with gr.Blocks(title="Relay console") as demo:
    gr.Markdown("# Relay console\nSends a message through the live pipeline. The full dashboard is at [/](/).")
    n = gr.Textbox(label="Customer name", value="Gradio user")
    t = gr.Textbox(label="Message", lines=4)
    out = gr.JSON(label="Routing")
    draft = gr.Textbox(label="AI draft", lines=8)
    gr.Button("Run pipeline").click(run_pipeline, [n, t], [out, draft], api_name="run")

if __name__ == "__main__":
    if "--uvicorn" in sys.argv:
        import uvicorn
        uvicorn.run(gr.mount_gradio_app(api_app, demo, path="/gradio"), host="0.0.0.0",
                    port=int(os.environ.get("PORT", 7860)))
    else:
        demo.queue(default_concurrency_limit=2)
        demo.launch(server_name="0.0.0.0", prevent_thread_lock=True, ssr_mode=False)
        # Relay's routes (/, /static, /api/*) go in front of Gradio's; Gradio keeps /gradio_api/*.
        router = demo.server_app.router
        for route in reversed(api_app.router.routes):
            router.routes.insert(0, route)
        _warm_generator()  # sub-app lifespans don't run when routes are grafted, so warm explicitly
        print("[relay] routes mounted; dashboard at /", flush=True)
        demo.block_thread()
