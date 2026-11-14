"""Provider-agnostic LLM access with a fallback chain, caching and robust JSON parsing.

Providers are tried in the configured order; if one is rate-limited or down the
next one takes over. With no API keys configured, SafeLLM simply reports
"unavailable" and every agent falls back to deterministic rule-based logic,
so the whole system still runs.
"""
import hashlib
import json
import logging
import re
import time

log = logging.getLogger("llm")


def parse_json_loose(text: str):
    """Parse JSON out of an LLM answer, tolerating code fences and prose."""
    if not text:
        return None
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    match = re.search(r"[\[{].*[\]}]", text, re.S)
    if match:
        try:
            return json.loads(match.group(0))
        except Exception:
            return None
    return None


class SafeLLM:
    """Wraps one or more chat models; retries, falls back and caches."""

    def __init__(self, providers: list[tuple[str, object]]):
        self.providers = providers  # list of (name, langchain chat model)
        self._cache: dict[str, str] = {}
        self.calls = 0

    def available(self) -> bool:
        return bool(self.providers)

    def provider_names(self) -> list[str]:
        return [name for name, _ in self.providers]

    def invoke(self, prompt: str) -> str | None:
        if not self.providers:
            return None
        key = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        if key in self._cache:
            return self._cache[key]
        for name, model in self.providers:
            for attempt in range(2):
                try:
                    self.calls += 1
                    response = model.invoke(prompt)
                    text = getattr(response, "content", str(response))
                    if isinstance(text, list):  # some providers return content blocks
                        text = "".join(
                            block.get("text", "") for block in text if isinstance(block, dict)
                        )
                    self._cache[key] = text
                    return text
                except Exception as exc:  # rate limit, network, quota...
                    log.warning("LLM provider %s failed (attempt %d): %s", name, attempt + 1, exc)
                    time.sleep(1.5 * (attempt + 1))
        log.error("All LLM providers failed; falling back to rule-based mode")
        return None

    def invoke_json(self, prompt: str):
        return parse_json_loose(self.invoke(prompt))


def build_llm(settings) -> SafeLLM:
    """Build the provider chain from configuration. Lazy imports keep the
    system runnable even if only some provider SDKs are installed."""
    providers: list[tuple[str, object]] = []
    for provider in settings.provider_order:
        try:
            if provider == "gemini" and settings.google_api_key:
                from langchain_google_genai import ChatGoogleGenerativeAI

                providers.append((
                    "gemini",
                    ChatGoogleGenerativeAI(
                        model=settings.gemini_model,
                        google_api_key=settings.google_api_key,
                        temperature=0.2,
                    ),
                ))
            elif provider == "groq" and settings.groq_api_key:
                from langchain_openai import ChatOpenAI

                providers.append((
                    "groq",
                    ChatOpenAI(
                        model=settings.groq_model,
                        base_url="https://api.groq.com/openai/v1",
                        api_key=settings.groq_api_key,
                        temperature=0.2,
                    ),
                ))
            elif provider == "openrouter" and settings.openrouter_api_key:
                from langchain_openai import ChatOpenAI

                providers.append((
                    "openrouter",
                    ChatOpenAI(
                        model=settings.openrouter_model,
                        base_url="https://openrouter.ai/api/v1",
                        api_key=settings.openrouter_api_key,
                        temperature=0.2,
                    ),
                ))
        except Exception as exc:
            log.warning("Could not initialise LLM provider %s: %s", provider, exc)
    if providers:
        log.info("LLM chain active: %s", " -> ".join(n for n, _ in providers))
    else:
        log.info("No LLM keys configured - running in rule-based mode")
    return SafeLLM(providers)
