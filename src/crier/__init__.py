"""crier: a Zulip digest for the Recurse Center.

Zulip text only ever goes to RC's own LLM (llm.recurse.com) or a model on
this machine. See guard.py.
"""

__version__ = "0.1.0"
USER_AGENT = f"crier/{__version__}"
