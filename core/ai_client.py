"""Gemini provider boundary with bounded retries and sanitized failures."""

import os
import random
import socket
import time
from datetime import datetime, timezone

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import google.generativeai as genai
except ImportError:
    genai = None

_API_KEY = os.getenv("GEMINI_API_KEY")
_MODEL_NAME = "gemini-3.6-flash"
_configured = False


class AIClientError(Exception):
    """Sanitized provider failure with machine-readable classification."""

    def __init__(self, message, error_type="provider_error", attempts=1):
        super().__init__(message)
        self.error_type = error_type
        self.attempts = attempts


class AIResponse(str):
    """String-compatible response carrying provider metadata."""

    def __new__(cls, text, metadata=None):
        value = super().__new__(cls, text)
        value.metadata = metadata or {}
        return value


def _ensure_configured():
    global _configured
    if _configured:
        return
    if not _API_KEY or not _API_KEY.strip() or _API_KEY == "your_key_here":
        raise AIClientError("GEMINI_API_KEY is missing. Set it in the project .env file.", "configuration")
    if genai is None:
        raise AIClientError("Gemini client dependency is not installed. Install requirements.txt.", "configuration")
    try:
        genai.configure(api_key=_API_KEY)
    except Exception:
        raise AIClientError("Gemini client configuration failed.", "configuration") from None
    _configured = True


def is_configured() -> bool:
    return bool(_API_KEY and _API_KEY.strip() and _API_KEY != "your_key_here")


def _classify_provider_error(error):
    message = str(error).lower()
    code = getattr(error, "code", None) or getattr(error, "status_code", None)
    error_class = type(error).__name__.lower()
    if (isinstance(error, (TimeoutError, socket.timeout)) or code == 504
            or "timeout" in error_class or "deadline" in error_class
            or "timeout" in message or "timed out" in message or "deadline exceeded" in message):
        return "timeout"
    if code == 429 or "429" in message or "rate limit" in message or "quota" in message or "resource_exhausted" in message:
        return "rate_limit"
    if code in (401, 403) or any(word in message for word in ("api key", "permission", "unauthorized", "forbidden")):
        return "authentication"
    return "provider_error"


def generate_response(prompt: str, temperature: float = 0.2, max_retries: int = 2,
                      timeout: float = 30.0) -> str:
    """Generate response text; returned value remains compatible with ``str`` callers."""
    _ensure_configured()
    if not isinstance(prompt, str) or not prompt.strip():
        raise AIClientError("Cannot send an empty prompt to the AI model.", "invalid_request")
    if not isinstance(max_retries, int) or isinstance(max_retries, bool) or max_retries < 0:
        raise ValueError("max_retries must be a non-negative integer.")
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError("timeout must be greater than zero.")

    try:
        model = genai.GenerativeModel(_MODEL_NAME)
    except Exception:
        raise AIClientError("Gemini model initialization failed.", "provider_error") from None
    for attempt in range(max_retries + 1):
        try:
            response = model.generate_content(
                prompt,
                generation_config=genai.types.GenerationConfig(temperature=temperature),
                request_options={"timeout": timeout},
            )
            response_text = getattr(response, "text", None) if response else None
            if not isinstance(response_text, str) or not response_text.strip():
                raise AIClientError("The AI model returned an empty response.", "empty_response", attempt + 1)
            return AIResponse(response_text, {
                "provider": "gemini", "model": _MODEL_NAME,
                "temperature": temperature, "attempts": attempt + 1,
                "timeout_seconds": timeout,
                "completed_at": datetime.now(timezone.utc).isoformat(),
            })
        except AIClientError:
            raise
        except Exception as exc:
            error_type = _classify_provider_error(exc)
            if error_type == "authentication":
                raise AIClientError("Gemini rejected the credentials or request permission.", error_type, attempt + 1) from None
            if attempt < max_retries:
                time.sleep(min(8.0, (2 ** attempt) + random.random()))
                continue
            messages = {
                "timeout": "Gemini request timed out.",
                "rate_limit": "Gemini rate limit or quota was reached.",
                "provider_error": "Gemini request failed after retries.",
            }
            raise AIClientError(messages.get(error_type, "Gemini request failed."), error_type, attempt + 1) from None
    raise AIClientError("Gemini request failed.", "provider_error", max_retries + 1)
