"""External AI providers: user-managed OpenAI/Anthropic-compatible LLM connections.

Prism features that need an external LLM (today: Agentic Deploy's optional
candidate selector, see ``llm_d_bench/agentic/planner.py``) resolve one of
these saved connections by id instead of relying solely on process-wide
``AGENTIC_OPENAI_*`` environment variables. Environment variables remain a
supported zero-config fallback (see ``AIProvider.from_environment``-style
resolution in ``service.py``).

Any component that needs to actually call the external LLM should use
``llm_d_bench.ai_providers.client.get_client(provider_id)`` rather than
building its own HTTP request -- it returns a provider-type-aware client
(``OpenAICompatibleClient`` or ``AnthropicCompatibleClient``) behind a single
``complete()`` interface, so callers stay agnostic of the provider's auth
scheme and request/response shape.
"""
