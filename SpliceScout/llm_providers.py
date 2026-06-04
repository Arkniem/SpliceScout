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
DEFAULT_MODEL = {"anthropic": "claude-haiku-4-5", "openai": "gpt-4.1-mini",
                 "gemini": "gemini-2.5-flash"}
# suggested models per provider (the UI model box is EDITABLE — these are just autocomplete hints;
# type any exact model id your account has access to). First entry is the per-provider default.
MODELS = {
    "anthropic": ["claude-haiku-4-5", "claude-sonnet-4-6"],
    "openai": ["gpt-4.1-mini", "gpt-4.1", "gpt-4o", "gpt-4o-mini"],
    "gemini": ["gemini-2.5-flash", "gemini-3.5-flash", "gemini-2.5-pro",
               "gemma-3-27b-it", "gemma-4-31b-it"],
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


def make_client(provider, max_retries=8):
    """Create the async client for a provider (anthropic, openai, or gemini-via-openai-compat)."""
    if provider == "anthropic":
        from anthropic import AsyncAnthropic
        return AsyncAnthropic(max_retries=max_retries)
    from openai import AsyncOpenAI
    if provider == "gemini":
        return AsyncOpenAI(api_key=api_key("gemini"), base_url=GEMINI_OPENAI_BASE,
                           max_retries=max_retries)
    return AsyncOpenAI(max_retries=max_retries)   # OPENAI_API_KEY from env


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
