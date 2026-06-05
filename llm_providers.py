# -*- coding: utf-8 -*-
"""
Provider-agnostic structured LLM calls for the AI cleaning passes + the cell-line matcher.

Three providers behind one async `classify()`:
  - anthropic : AsyncAnthropic, forced tool-use (+ prompt caching)
  - openai    : AsyncOpenAI chat.completions, forced function-call
  - gemini    : the SAME AsyncOpenAI SDK pointed at Google's OpenAI-compatible endpoint

Each call takes a `tool` = {name, description, input_schema} whose input_schema is an object with a
`results` array, and returns (results_list, usage_dict). The OpenAI/Gemini path defensively falls
back to parsing JSON from the message content if a model returns the object inline instead of as a
tool call.

API keys (env): ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY (or GOOGLE_API_KEY).
"""
import json
import os
import re

GEMINI_OPENAI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai/"

PROVIDERS = ("anthropic", "openai", "gemini")
PROVIDER_LABEL = {"anthropic": "Anthropic (Claude)", "openai": "OpenAI (ChatGPT)",
                  "gemini": "Google Gemini"}
KEY_ENV = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY"}
DEFAULT_MODEL = {"anthropic": "claude-haiku-4-5", "openai": "gpt-5.4-nano",
                 "gemini": "gemma-4-31b-it"}
# suggested models per provider (the UI model box is EDITABLE — these are just autocomplete hints;
# type any exact model id your account has access to). First entry is the per-provider default.
MODELS = {
    "anthropic": ["claude-haiku-4-5", "claude-sonnet-4-6"],
    "openai": ["gpt-5.4-nano", "gpt-4.1-mini", "gpt-4.1", "gpt-4o", "gpt-4o-mini"],
    "gemini": ["gemma-4-31b-it", "gemini-2.5-flash", "gemini-3.5-flash", "gemini-2.5-pro",
               "gemma-3-27b-it"],
}


def normalize_provider(p):
    p = (p or "anthropic").strip().lower()
    if p in ("chatgpt", "gpt"):
        p = "openai"
    if p in ("google", "google-gemini"):
        p = "gemini"
    return p if p in PROVIDERS else "anthropic"


def resolve_model(provider, model):
    return (model or "").strip() or DEFAULT_MODEL.get(provider, DEFAULT_MODEL["anthropic"])


def api_key(provider):
    if provider == "gemini":
        return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""
    return os.environ.get(KEY_ENV.get(provider, ""), "") or ""


def have_key(provider):
    return bool(api_key(provider))


def make_client(provider, max_retries=8, timeout=None, base_url=None):
    """Create the async client for a provider (anthropic, openai, or gemini-via-openai-compat).

    `timeout` (seconds), when set, caps each request — used by the AI preflight so a bad/slow
    endpoint fails fast instead of blocking on the SDK's long default.
    `base_url`, when set, points the provider at a CUSTOM OpenAI-compatible endpoint — e.g. running
    an OpenAI-format model (MiMo, Qwen, a local vLLM/LM-Studio server, OpenRouter, …) through the
    'openai' provider. Blank/None => the provider's default endpoint (api.openai.com for 'openai';
    the Gemini OpenAI-compat URL for 'gemini')."""
    extra = {} if timeout is None else {"timeout": timeout}
    base_url = (base_url or "").strip() or None
    if provider == "anthropic":
        from anthropic import AsyncAnthropic
        if base_url:
            extra["base_url"] = base_url
        return AsyncAnthropic(max_retries=max_retries, **extra)
    from openai import AsyncOpenAI
    if provider == "gemini":
        return AsyncOpenAI(api_key=api_key("gemini"), base_url=base_url or GEMINI_OPENAI_BASE,
                           max_retries=max_retries, **extra)
    if base_url:                                  # custom OpenAI-compatible host (MiMo etc.)
        extra["base_url"] = base_url
    return AsyncOpenAI(max_retries=max_retries, **extra)   # OPENAI_API_KEY from env


async def close_client(client):
    try:
        await client.close()
    except Exception:
        pass


def _extract_results(text):
    """Defensive: pull a `results` array out of free-text/JSON (handles ```json fences)."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t).strip()
    try:
        obj = json.loads(t)
    except Exception:
        m = re.search(r"\{.*\}", t, re.S)
        obj = json.loads(m.group(0)) if m else {}
    if isinstance(obj, dict):
        return obj.get("results", [])
    return obj if isinstance(obj, list) else []


async def classify(client, provider, model, system_text, user_obj, tool, max_tokens=8000):
    """Run one structured batch. Returns (results_list, usage_dict)."""
    name = tool["name"]
    user_content = json.dumps(user_obj, ensure_ascii=False)

    if provider == "anthropic":
        system = [{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}]
        async with client.messages.stream(
            model=model, max_tokens=max_tokens, system=system,
            tools=[dict(tool, **{"strict": True})],
            tool_choice={"type": "tool", "name": name},
            messages=[{"role": "user", "content": user_content}],
        ) as stream:
            msg = await stream.get_final_message()
        tool_input = next((b.input for b in msg.content if b.type == "tool_use"), None)
        results = (tool_input or {}).get("results", [])
        u = msg.usage
        usage = {"input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
                 "cache_read": getattr(u, "cache_read_input_tokens", 0),
                 "cache_creation": getattr(u, "cache_creation_input_tokens", 0)}
        return results, usage

    # ---- openai / gemini (OpenAI-compatible) ----
    func = {"type": "function", "function": {
        "name": name, "description": tool.get("description", ""),
        "parameters": tool["input_schema"]}}
    kwargs = dict(
        model=model, max_tokens=max_tokens,
        messages=[{"role": "system", "content": system_text},
                  {"role": "user", "content": user_content}],
        tools=[func],
        tool_choice=({"type": "function", "function": {"name": name}}
                     if provider == "openai" else "required"),
    )
    resp = await client.chat.completions.create(**kwargs)
    choice = resp.choices[0].message
    results = []
    tcs = getattr(choice, "tool_calls", None)
    if tcs:
        try:
            results = json.loads(tcs[0].function.arguments).get("results", [])
        except Exception:
            results = []
    if not results and getattr(choice, "content", None):
        try:
            results = _extract_results(choice.content)
        except Exception:
            results = []
    u = getattr(resp, "usage", None)
    usage = {"input_tokens": getattr(u, "prompt_tokens", 0) if u else 0,
             "output_tokens": getattr(u, "completion_tokens", 0) if u else 0,
             "cache_read": 0, "cache_creation": 0}
    return results, usage


def classify_ai_error(exc):
    """Turn an AI-provider exception into an actionable diagnosis {category, title, detail}.
    Categories: auth | model | perm | rate | network | unknown. Used to tell the user whether to fix
    the API key, the model name, the provider, etc. — or turn AI off."""
    msg = str(exc or "")
    low = msg.lower()
    status = (getattr(exc, "status_code", None)
              or getattr(getattr(exc, "response", None), "status_code", None))

    def D(category, title, detail):
        return {"category": category, "title": title, "detail": detail}

    if status == 401 or any(s in low for s in ("api key", "api_key", "unauthorized", "authenticat",
                                               "invalid x-api-key", "no auth credentials",
                                               "invalid_api_key")):
        return D("auth", "API key rejected",
                 "The API key was missing or invalid for this provider. Paste a valid key, switch "
                 "provider, or turn off AI cleaning.")
    if status == 404 or any(s in low for s in ("not found", "does not exist", "is not a valid model",
                                               "unknown model", "no such model", "model_not_found")):
        return D("model", "Model not found",
                 "That model name isn't valid for this provider/account. Fix the model id, switch "
                 "provider, or turn off AI cleaning.")
    if status == 403 or "permission" in low or ("access" in low and "denied" in low):
        return D("perm", "Access denied for this model",
                 "Your account can't access this model. Use a different model/provider, or turn off "
                 "AI cleaning.")
    if status == 429 or any(s in low for s in ("rate limit", "ratelimit", "quota",
                                               "resource_exhausted", "too many requests")):
        return D("rate", "Rate limit / quota exceeded",
                 "The provider is rate-limiting this key (often a free tier). Lower concurrency, wait, "
                 "switch provider, or turn off AI cleaning.")
    if any(s in low for s in ("timed out", "timeout", "connection", "network", "temporary failure",
                              "getaddrinfo", "ssl")):
        return D("network", "Couldn't reach the provider",
                 "Network error contacting the AI provider. Check connectivity / base URL, switch "
                 "provider, or turn off AI cleaning.")
    return D("unknown", "AI provider call failed",
             (msg[:200] or "The AI provider call failed.")
             + "  Fix the provider / model / key, or turn off AI cleaning.")
