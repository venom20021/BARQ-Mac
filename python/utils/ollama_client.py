"""
LLM client for conversational AI responses — with automatic cloud fallback.

Primary client connects to a local Ollama instance. If Ollama is unavailable,
automatically falls back to an OpenAI-compatible cloud API so AI features
keep working even when the local LLM is offline.
"""

import asyncio
import json as _json
from typing import AsyncIterable

import socket

import httpx

from config import get_settings

# Name Ollama is expected to be installed at on Windows / Linux
_OLLAMA_INSTALL_URL = "https://ollama.com/download/windows"


# ═══════════════════════════════════════════════════════════════════════
#  Error classes
# ═══════════════════════════════════════════════════════════════════════


class OllamaNotAvailableError(ConnectionError):
    """Raised when Ollama is unreachable or the model is missing."""
    def __init__(self, host: str, model: str):
        self.ollama_host = host
        self.ollama_model = model
        self.reason = self._diagnose()
        super().__init__(self.reason)

    def _diagnose(self) -> str:
        """Try to give a helpful diagnostic message."""
        host = self.ollama_host
        model = self.ollama_model

        # Parse host:port from URL
        try:
            clean_host = host.replace("http://", "").replace("https://", "")
            hostname, port_str = clean_host.split(":")
            port = int(port_str)
        except (ValueError, AttributeError):
            hostname = host
            port = 11434

        # Check if the port is actually open
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        try:
            result = sock.connect_ex((hostname, port))
            sock.close()
        except Exception:
            sock.close()
            result = -1

        if result != 0:
            return (
                f"Ollama is not running at {host}. "
                f"Install Ollama from https://ollama.com/download/windows, "
                f"then run: ollama serve  (or start Ollama from Start Menu)."
            )

        # Port is open — maybe model is missing
        return (
            f"Ollama is running at {host} but the model '{model}' was not found. "
            f"Pull it with: ollama pull {model}"
        )


class CloudLLMNotConfiguredError(ConnectionError):
    """Raised when cloud fallback is not configured."""
    def __init__(self):
        super().__init__(
            "Cloud LLM fallback is not configured. "
            "Set OPENAI_API_KEY in your .env file, or install Ollama locally."
        )


# ═══════════════════════════════════════════════════════════════════════
#  Cloud LLM fallback (OpenAI-compatible API)
# ═══════════════════════════════════════════════════════════════════════


class CloudLLMClient:
    """Client for OpenAI-compatible cloud LLM APIs.

    Works with OpenAI, OpenRouter, Groq, Together AI, and any other
    provider that implements the OpenAI chat completions format.

    Configure via .env:
        OPENAI_API_KEY=sk-...
        CLOUD_LLM_MODEL=gpt-4o-mini       (default)
        CLOUD_LLM_BASE_URL=https://api.openai.com/v1  (default)
    """

    def __init__(self, temperature: float = 0.7):
        settings = get_settings()
        self.api_key = settings.openai_api_key
        self.model = settings.cloud_llm_model
        self.base_url = settings.cloud_llm_base_url.rstrip("/")
        self.temperature = temperature
        # Enabled when CLOUD_LLM_ENABLED is true — even without an API key.
        # This supports local LM Studio instances (no auth required) as well
        # as cloud providers (OpenAI, Groq, etc. with an API key).
        self._enabled = settings.cloud_llm_enabled

    @property
    def enabled(self) -> bool:
        """Whether the cloud fallback is configured and ready."""
        return self._enabled

    async def chat(self, messages: list[dict]) -> str:
        """Non-streaming chat completion via cloud API."""
        if not self._enabled:
            raise CloudLLMNotConfiguredError()

        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "messages": messages,
                        "temperature": self.temperature,
                        "top_p": 0.9,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"]
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                raise CloudLLMNotConfiguredError() from e
            raise

    async def stream_chat(self, messages: list[dict]) -> AsyncIterable[str]:
        """Streaming chat completion via cloud API (SSE format)."""
        if not self._enabled:
            raise CloudLLMNotConfiguredError()

        try:
            async with httpx.AsyncClient(timeout=120) as client:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "messages": messages,
                        "stream": True,
                        "temperature": self.temperature,
                        "top_p": 0.9,
                    },
                ) as response:
                    try:
                        async for line in response.aiter_lines():
                            if not line.strip():
                                continue
                            # SSE format: "data: {...}"
                            if line.startswith("data: "):
                                payload = line[6:].strip()
                                if payload == "[DONE]":
                                    break
                                try:
                                    data = _json.loads(payload)
                                    delta = data.get("choices", [{}])[0].get("delta", {})
                                    token = delta.get("content", "")
                                    if token:
                                        yield token
                                except (_json.JSONDecodeError, KeyError, IndexError):
                                    continue
                    except (httpx.ReadError, httpx.RemoteProtocolError, httpx.StreamError,
                             httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout,
                             TimeoutError, ConnectionResetError, ConnectionAbortedError) as mid_err:
                        error_msg = str(mid_err)[:120]
                        print(f"[CloudLLM] Mid-stream error: {error_msg}")
                        yield "\n\n[Sorry, the cloud AI engine encountered a stream error. Please try again.]"
                        try:
                            from voice.evolution_logger import get_evolution_logger
                            get_evolution_logger().record(
                                "llm_error",
                                metadata={"source": "cloud", "error": error_msg, "phase": "stream"},
                            )
                        except Exception:
                            pass
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                raise CloudLLMNotConfiguredError() from e
            raise
        except httpx.ConnectError:
            raise ConnectionError(
                f"Could not reach cloud LLM at {self.base_url}. "
                f"Check your internet connection and CLOUD_LLM_BASE_URL setting."
            )

    async def is_available(self) -> bool:
        """Check if the cloud LLM API is configured (no connectivity test)."""
        return self._enabled


# ═══════════════════════════════════════════════════════════════════════
#  Primary OllamaClient with automatic cloud fallback
# ═══════════════════════════════════════════════════════════════════════


class OllamaClient:
    """Client for local LLM API with automatic cloud fallback.

    Supports two backends:
    - Ollama format (default): uses /api/chat, /api/generate endpoints
    - OpenAI format (LM Studio, etc.): uses /v1/chat/completions endpoint

    When backend is "auto", probes the host on first use to detect the
    correct format. Falls back to cloud LLM (e.g. Groq) when the local
    server is unreachable.
    """

    def __init__(self, host: str | None = None, model: str | None = None, temperature: float = 0.7):
        settings = get_settings()
        self.host = (host or settings.ollama_host).rstrip("/")
        self.model = model or settings.ollama_model
        self.temperature = temperature
        self._backend_override = settings.llm_backend  # "auto", "ollama", "openai"
        self._backend: str | None = None  # Detected on first use when "auto"
        self._cloud = CloudLLMClient(temperature=temperature)  # Primary: LM Studio via CLOUD_LLM_BASE_URL
        # Secondary Groq fallback
        self._groq_fallback: CloudLLMClient | None = None
        self._init_groq_fallback(settings)
        self._fallback_reported = False  # only print fallback message once

    def _init_groq_fallback(self, settings) -> None:
        """Initialize a secondary Groq client for when LM Studio is unreachable."""
        fallback_url = settings.cloud_llm_fallback_base_url
        if fallback_url and settings.cloud_llm_fallback_enabled:
            self._groq_fallback = CloudLLMClient()
            # Override the cloud client's URL and model for Groq
            self._groq_fallback.base_url = fallback_url.rstrip("/")
            self._groq_fallback.model = settings.cloud_llm_fallback_model
            self._groq_fallback.api_key = settings.openai_api_key
            self._groq_fallback._enabled = bool(settings.openai_api_key)

    async def _ensure_backend_detected(self) -> None:
        """Probe the host to detect the API format.

        Sets self._backend to "ollama" or "openai".
        When llm_backend config is explicit, uses that directly.
        """
        if self._backend is not None:
            return

        # Use explicit override if set
        if self._backend_override in ("ollama", "openai"):
            self._backend = self._backend_override
            return

        # Auto-detect: probe both endpoints in parallel
        async def _probe_openai() -> bool:
            try:
                async with httpx.AsyncClient(timeout=2) as client:
                    resp = await client.get(f"{self.host}/v1/models")
                    return resp.status_code == 200
            except Exception:
                return False

        async def _probe_ollama() -> bool:
            try:
                async with httpx.AsyncClient(timeout=2) as client:
                    resp = await client.get(f"{self.host}/api/tags")
                    return resp.status_code == 200
            except Exception:
                return False

        openai_ok, ollama_ok = await asyncio.gather(
            _probe_openai(), _probe_ollama()
        )

        if openai_ok:
            self._backend = "openai"
        elif ollama_ok:
            self._backend = "ollama"
        else:
            self._backend = "ollama"  # Default

    async def chat(self, messages: list[dict]) -> str:
        """Send a conversation to the LLM and get a response.

        When backend is Ollama: tries local Ollama format first.
        When backend is OpenAI: talks directly to LM Studio / OpenAI-compatible server.
        Falls back to Groq (if configured) when the primary is unreachable.

        Args:
            messages: List of message dicts with 'role' and 'content' keys.

        Returns:
            Generated response text.

        Raises:
            CloudLLMNotConfiguredError: If all backends are unavailable.
        """
        await self._ensure_backend_detected()

        if self._backend == "openai":
            return await self._chat_openai_format(messages)

        # Ollama format
        try:
            return await self._ollama_chat(messages)
        except OllamaNotAvailableError:
            return await self._fallback_chat(messages)

    async def _chat_openai_format(self, messages: list[dict]) -> str:
        """Talk directly to an OpenAI-compatible server (LM Studio).

        Tier 1: Try primary cloud client (LM Studio via CLOUD_LLM_BASE_URL)
        Tier 2: Try Groq fallback if configured
        """
        # Tier 1: Primary cloud client (should be LM Studio)
        if self._cloud.enabled:
            try:
                return await self._cloud.chat(messages)
            except (ConnectionError, httpx.HTTPStatusError) as e:
                print(f"[Ollama] Primary cloud (LM Studio) unavailable: {e}")

        # Tier 2: Groq fallback
        if self._groq_fallback and self._groq_fallback.enabled:
            try:
                await self._report_fallback_once()
                return await self._groq_fallback.chat(messages)
            except Exception as e:
                print(f"[Ollama] Groq fallback also failed: {e}")

        raise CloudLLMNotConfiguredError()

    async def stream_chat(self, messages: list[dict]) -> AsyncIterable[str]:
        """Stream a conversation response token-by-token.

        When backend is Ollama: streams from local Ollama.
        When backend is OpenAI: streams directly from LM Studio.
        Falls back to Groq when the primary is unreachable.

        Args:
            messages: List of message dicts with 'role' and 'content' keys.

        Yields:
            Each text token as it arrives.

        Raises:
            CloudLLMNotConfiguredError: If all backends are unavailable.
        """
        await self._ensure_backend_detected()

        if self._backend == "openai":
            async for token in self._stream_openai_format(messages):
                yield token
            return

        # Ollama format
        try:
            async for token in self._ollama_stream_chat(messages):
                yield token
        except OllamaNotAvailableError:
            async for token in self._fallback_stream_chat(messages):
                yield token

    async def _stream_openai_format(self, messages: list[dict]) -> AsyncIterable[str]:
        """Stream from an OpenAI-compatible server with Groq fallback."""
        # Tier 1: Primary cloud client (LM Studio)
        if self._cloud.enabled:
            try:
                async for token in self._cloud.stream_chat(messages):
                    yield token
                return
            except (ConnectionError, httpx.HTTPStatusError) as e:
                print(f"[Ollama] Primary cloud stream unavailable: {e}")

        # Tier 2: Groq fallback
        if self._groq_fallback and self._groq_fallback.enabled:
            try:
                await self._report_fallback_once()
                async for token in self._groq_fallback.stream_chat(messages):
                    yield token
                return
            except Exception as e:
                print(f"[Ollama] Groq fallback stream error: {e}")

        raise CloudLLMNotConfiguredError()

    async def generate(self, prompt: str) -> str:
        """Simple single-prompt generation (no conversation history).

        Tries local Ollama/OpenAI first. Falls back to cloud LLM if configured.

        Args:
            prompt: The prompt text.

        Returns:
            Generated response text.
        """
        await self._ensure_backend_detected()

        if self._backend == "openai":
            messages = [{"role": "user", "content": prompt}]
            return await self._chat_openai_format(messages)

        try:
            return await self._ollama_generate(prompt)
        except OllamaNotAvailableError:
            messages = [{"role": "user", "content": prompt}]
            return await self._fallback_chat(messages)

    async def is_available(self) -> bool:
        """Check if any LLM backend is available (Ollama, LM Studio, or cloud)."""
        # Check local backend first
        await self._ensure_backend_detected()
        try:
            if self._backend == "openai":
                async with httpx.AsyncClient(timeout=2) as client:
                    resp = await client.get(f"{self.host}/v1/models")
                    if resp.status_code == 200:
                        return True
            else:
                async with httpx.AsyncClient(timeout=2) as client:
                    resp = await client.get(f"{self.host}/api/tags")
                    if resp.status_code == 200:
                        models = resp.json().get("models", [])
                        if any(m["name"].startswith(self.model) for m in models):
                            return True
        except Exception:
            pass
        # Fallback: check cloud or Groq
        if self._groq_fallback and self._groq_fallback.enabled:
            return True
        return await self._cloud.is_available()

    # ── Internal: Ollama methods ─────────────────────────────────────

    async def _ollama_chat(self, messages: list[dict]) -> str:
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"{self.host}/api/chat",
                    json={
                        "model": self.model,
                        "messages": messages,
                        "stream": False,
                        "options": {"temperature": self.temperature, "top_p": 0.9},
                    },
                )
                resp.raise_for_status()
                return resp.json()["message"]["content"]
        except (httpx.ConnectError, httpx.HTTPStatusError, KeyError, IndexError, TypeError, ValueError):
            raise OllamaNotAvailableError(self.host, self.model)

    async def _ollama_stream_chat(self, messages: list[dict]) -> AsyncIterable[str]:
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                async with client.stream(
                    "POST",
                    f"{self.host}/api/chat",
                    json={
                        "model": self.model,
                        "messages": messages,
                        "stream": True,
                        "options": {"temperature": self.temperature, "top_p": 0.9},
                    },
                ) as response:
                    response.raise_for_status()
                    try:
                        async for line in response.aiter_lines():
                            if not line.strip():
                                continue
                            try:
                                data = _json.loads(line)
                                if data.get("done"):
                                    break
                                token = data.get("message", {}).get("content", "")
                                if token:
                                    yield token
                            except (_json.JSONDecodeError, KeyError):
                                continue
                    except (httpx.ReadError, httpx.RemoteProtocolError, httpx.StreamError,
                             httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout,
                             TimeoutError, ConnectionResetError, ConnectionAbortedError) as mid_err:
                        error_msg = str(mid_err)[:120]
                        print(f"[Ollama] Mid-stream error in _ollama_stream_chat: {error_msg}")
                        # Yield a fallback error token so the caller doesn't hang with partial output
                        yield "\n\n[Sorry, the local AI engine encountered a stream error. Please try again.]"
                        # Log to evolution tracker
                        try:
                            from voice.evolution_logger import get_evolution_logger
                            get_evolution_logger().record(
                                "llm_error",
                                metadata={"source": "ollama", "error": error_msg, "phase": "stream"},
                            )
                        except Exception:
                            pass
        except (httpx.ConnectError, httpx.HTTPStatusError, KeyError, IndexError, TypeError, ValueError):
            raise OllamaNotAvailableError(self.host, self.model)

    async def _ollama_generate(self, prompt: str) -> str:
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"{self.host}/api/generate",
                    json={"model": self.model, "prompt": prompt, "stream": False},
                )
                resp.raise_for_status()
                return resp.json()["response"]
        except (httpx.ConnectError, httpx.HTTPStatusError, KeyError, IndexError, TypeError, ValueError):
            raise OllamaNotAvailableError(self.host, self.model)

    # ── Internal: Cloud fallback methods ────────────────────────────

    async def _report_fallback_once(self):
        if not self._fallback_reported:
            self._fallback_reported = True
            import logging
            logging.getLogger("barq").warning(
                "Ollama unavailable — using cloud LLM fallback (%s)", self._cloud.model
            )

    async def _fallback_chat(self, messages: list[dict]) -> str:
        if not self._cloud.enabled:
            raise CloudLLMNotConfiguredError()
        await self._report_fallback_once()
        return await self._cloud.chat(messages)

    async def _fallback_stream_chat(self, messages: list[dict]) -> AsyncIterable[str]:
        if not self._cloud.enabled:
            raise CloudLLMNotConfiguredError()
        await self._report_fallback_once()
        async for token in self._cloud.stream_chat(messages):
            yield token
