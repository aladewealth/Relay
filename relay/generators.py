"""Pluggable reply generators. Selected by env:
  SUBQ_SAVANT=1                         -> SubQ Savant (chat/completions)
  LLM_BASE_URL (+LLM_API_KEY, LLM_MODEL) -> any OpenAI-compatible endpoint
  default                               -> local GGUF model on CPU via llama-cpp-python
  GENERATOR=none                        -> drafting disabled (every case goes to a human)
"""
import os
import threading
import time

import httpx

from . import config


class GeneratorError(Exception):
    pass


class Generator:
    name = "base"
    model = "none"

    def status(self):
        return {"provider": self.name, "model": self.model, "ready": True, "error": None}

    def generate(self, messages, max_tokens=320):
        """messages: OpenAI-style list. Returns text. Raises GeneratorError."""
        raise NotImplementedError


class OpenAICompatGenerator(Generator):
    name = "openai_compatible"

    def __init__(self, base_url, api_key=None, model=None, extra=None, timeout=120):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model or "gpt-4o-mini"
        self.extra = extra or {"temperature": 0.3}
        self.timeout = timeout

    def status(self):
        return {"provider": self.name, "model": self.model, "ready": True, "error": None, "base_url": self.base_url}

    def generate(self, messages, max_tokens=320):
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        body = {"model": self.model, "messages": messages, "max_tokens": max_tokens, **self.extra}
        try:
            r = httpx.post(f"{self.base_url}/chat/completions", json=body, headers=headers, timeout=self.timeout)
        except httpx.HTTPError as e:
            raise GeneratorError(f"network error: {e}") from e
        if r.status_code != 200:
            raise GeneratorError(f"{r.status_code}: {r.text[:300]}")
        content = r.json()["choices"][0]["message"].get("content")
        if not content:
            raise GeneratorError("empty completion")
        return content.strip()


class SavantGenerator(OpenAICompatGenerator):
    name = "subq_savant"

    def __init__(self):
        super().__init__(config.SUBQ_BASE_URL, config.subq_key(), env_model("subq-savant-1.4"),
                         # Savant: temperature fixed at 1; reasoning shares max_tokens, so budget generously
                         extra={"reasoning_effort": "low"})

    def generate(self, messages, max_tokens=320):
        return super().generate(messages, max_tokens=max(max_tokens * 6, 2000))


def usable_cpus():
    """CPUs this container may actually use (cgroup quota / affinity), not the host's core count."""
    n = None
    try:
        quota, period = open("/sys/fs/cgroup/cpu.max").read().split()[:2]
        if quota != "max":
            n = max(1, int(int(quota) / int(period)))
    except Exception:  # noqa: BLE001
        pass
    if n is None:
        try:
            q = int(open("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read())
            p = int(open("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read())
            if q > 0:
                n = max(1, q // p)
        except Exception:  # noqa: BLE001
            pass
    try:
        aff = len(os.sched_getaffinity(0))
    except Exception:  # noqa: BLE001
        aff = os.cpu_count() or 2
    n = min(n or aff, aff)
    return max(1, min(n, 4))  # shared/ZeroGPU hosts over-report cores; more threads than real cores thrashes


def cpu_info():
    info = {"os_cpu_count": os.cpu_count()}
    try:
        info["affinity"] = len(os.sched_getaffinity(0))
    except Exception:  # noqa: BLE001
        pass
    for f in ("/sys/fs/cgroup/cpu.max", "/sys/fs/cgroup/cpu/cpu.cfs_quota_us"):
        try:
            info[f.rsplit("/", 1)[-1]] = open(f).read().strip()
        except Exception:  # noqa: BLE001
            pass
    try:
        info["loadavg"] = open("/proc/loadavg").read().split()[:3]
    except Exception:  # noqa: BLE001
        pass
    try:
        txt = open("/proc/cpuinfo").read()
        model = next((l.split(":", 1)[1].strip() for l in txt.splitlines() if l.startswith("model name")), None)
        flags = next((l.split(":", 1)[1].split() for l in txt.splitlines() if l.startswith("flags")), [])
        info["cpu_model"] = model
        info["simd"] = [f for f in ("avx", "avx2", "fma", "f16c", "avx512f", "avx512_vnni") if f in flags]
    except Exception:  # noqa: BLE001
        pass
    info["usable_cpus"] = usable_cpus()
    return info


def env_model(default):
    return config.env("LLM_MODEL", default)


class LocalLlamaGenerator(Generator):
    """Small instruct model on CPU (default Qwen2.5-1.5B-Instruct Q4_K_M GGUF). Loads in a background thread."""
    name = "local_llama_cpp"

    def __init__(self):
        self.repo = config.env("LOCAL_MODEL_REPO", "Qwen/Qwen2.5-1.5B-Instruct-GGUF")
        self.file = config.env("LOCAL_MODEL_FILE", "qwen2.5-1.5b-instruct-q4_k_m.gguf")
        self.model = f"{self.repo}/{self.file}"
        self.n_threads = int(config.env("LOCAL_MODEL_THREADS", usable_cpus()))
        self._llm = None
        self._err = None
        self._loading = False
        self._load_ms = None
        self._lock = threading.Lock()
        self._gen_lock = threading.Lock()
        self.warm_system = None  # set to the (constant) system prompt to pre-fill the KV cache after load
        self._warm_ms = None

    def start_loading(self):
        with self._lock:
            if self._llm is not None or self._loading:
                return
            self._loading = True
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self):
        t0 = time.perf_counter()
        try:
            from huggingface_hub import hf_hub_download
            from llama_cpp import Llama
            path = config.env("LOCAL_MODEL_PATH") or hf_hub_download(self.repo, self.file)
            llm = Llama(model_path=path, n_ctx=4096, n_threads=self.n_threads, n_batch=256, verbose=False,
                        use_mmap=config.env("LOCAL_MODEL_MMAP", "0") == "1")  # read fully into RAM: lazy mmap paging is very slow on Space storage
            self._load_ms = int((time.perf_counter() - t0) * 1000)
            if self.warm_system:  # pre-fill the KV cache with the constant system prompt (rules + KB)
                w0 = time.perf_counter()
                with self._gen_lock:
                    llm.create_chat_completion(messages=[{"role": "system", "content": self.warm_system},
                                                               {"role": "user", "content": "ok"}], max_tokens=1)
                self._warm_ms = int((time.perf_counter() - w0) * 1000)
            self._llm = llm
        except Exception as e:  # noqa: BLE001
            self._err = f"{type(e).__name__}: {e}"
        finally:
            self._loading = False

    def status(self):
        ready = self._llm is not None
        return {"provider": self.name, "model": self.model, "ready": ready, "loading": self._loading,
                "error": self._err, "load_ms": self._load_ms, "warm_ms": self._warm_ms, "threads": self.n_threads,
                "warming": self._loading and self._load_ms is not None}

    def generate(self, messages, max_tokens=280):
        if self._llm is None:
            if not self._loading and self._err is None:
                self.start_loading()
            # wait up to 10 min for first load (download + mmap)
            deadline = time.time() + 600
            while self._llm is None and self._err is None and time.time() < deadline:
                time.sleep(1)
            if self._llm is None:
                raise GeneratorError(f"local model not available: {self._err or 'still loading'}")
        with self._gen_lock:  # llama.cpp context is not thread-safe
            try:
                out = self._llm.create_chat_completion(messages=messages, max_tokens=max_tokens, temperature=0.3,
                                                       top_p=0.9, repeat_penalty=1.1)
            except Exception as e:  # noqa: BLE001
                raise GeneratorError(f"local generation failed: {e}") from e
        text = (out["choices"][0]["message"].get("content") or "").strip()
        if not text:
            raise GeneratorError("empty completion")
        return text


class DisabledGenerator(Generator):
    name = "disabled"

    def status(self):
        return {"provider": self.name, "model": "none", "ready": False, "error": "drafting disabled (GENERATOR=none)"}

    def generate(self, messages, max_tokens=320):
        raise GeneratorError("drafting disabled")


_instance = None
_inst_lock = threading.Lock()


def make_generator():
    choice = (config.env("GENERATOR") or "").lower()
    if choice == "none":
        return DisabledGenerator()
    if config.env("SUBQ_SAVANT") == "1" or choice == "subq_savant":
        return SavantGenerator()
    if config.env("LLM_BASE_URL") or choice == "openai":
        return OpenAICompatGenerator(config.env("LLM_BASE_URL", "https://api.openai.com/v1"),
                                     config.env("LLM_API_KEY"), config.env("LLM_MODEL"))
    return LocalLlamaGenerator()


def get_generator():
    global _instance
    with _inst_lock:
        if _instance is None:
            _instance = make_generator()
        return _instance


def set_generator(g):
    """For tests / custom providers."""
    global _instance
    with _inst_lock:
        _instance = g
