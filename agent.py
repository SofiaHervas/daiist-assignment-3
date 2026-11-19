"""
Assignment 3: the agent (`main.py ask "question"` runs `python agent.py "question"`).

Built from session 10's 07-own-harness/harness.py: LangChain `create_agent` on
Gemini, the tools of server.py over MCP, AGENTS.md as system prompt, and
guardrails the model cannot skip because the harness runs them:

1. Approval: add_document runs only if the person at the keyboard types "y".
2. Call limit: at most 8 model calls per question (runaway loops).
3. Citation gate (my extra guardrail): before an answer is shown, the harness
   checks it in code. Every citation must match a passage that search really
   returned (same document, overlapping pages), and an answer that is not the
   refusal must cite at least one. Otherwise the answer is withheld. For an
   installer, an uncited or wrongly cited rule is worse than no answer.

evaluate.py imports `open_agent` and `ask`, which return the answer, the tool
trace, the gate verdict, tokens and seconds.
"""

import asyncio
import re
import sys
import time
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware, wrap_tool_call
from langchain_core.messages import AIMessage, ToolMessage
from langchain_google_genai import ChatGoogleGenerativeAI

from langchain.mcp import MCPAdapter  # langchain.mcp is in beta and warns on import

from api_key import load_api_key

ROOT = Path(__file__).resolve().parent
MODEL = "gemini-3.5-flash-lite"  # as in class: fast, cheap, generous free-tier quota
SERVER = ROOT / "server.py"
NEEDS_APPROVAL = {"add_document"}  # changes the index for every later question
MAX_MODEL_CALLS = 8
REFUSAL = "El corpus no cubre esta pregunta."

# [itc_bt_52, p. 31] or [afir, pp. 24-25]: the tags search puts before each passage.
CITATION = re.compile(r"\[([a-z0-9_]+), pp?\. (\d+)(?:-(\d+))?\]")
# In answers the model sometimes groups them: [itc_bt_52, pp. 31-32; rd_450_2022, p. 13].
BRACKET = re.compile(r"\[([^\[\]]+)\]")
REF = re.compile(r"([a-z0-9_]+), pp?\. (\d+)(?:-(\d+))?")
LOOKS_LIKE_CITATION = re.compile(r"^\s*[a-z][a-z0-9_-]*\s*(?:,|$)")


def citations(answer):
    """Every citation in the answer as (text, source, first page, last page).

    Reads one or several references per bracket. A bracket that looks like a
    citation but names no page (e.g. an internal id) comes back with source None,
    so the gate rejects it.
    """
    found = []
    for bracket in BRACKET.finditer(answer):
        body = bracket.group(1)
        if not (REF.search(body) or LOOKS_LIKE_CITATION.match(body)):
            continue  # an ordinary bracket, not a citation
        for ref in REF.finditer(body):
            found.append((f"[{ref.group(0)}]", ref.group(1), int(ref.group(2)), int(ref.group(3) or ref.group(2))))
        rest = REF.sub("", body).strip(" ,;")
        if rest:
            found.append((f"[{rest}]", None, 0, 0))
    return found


# 1. Tools: start server.py over stdio and wrap its tools as LangChain tools.
async def load_mcp_tools(stack):
    adapter = await stack.enter_async_context(MCPAdapter(SERVER))
    return await adapter.list_tools()


# 2. Approval before add_document, as ask_before_network in class.
def approval_guardrail(auto_approve):
    @wrap_tool_call
    async def ask_before_indexing(request, handler):
        call = request.tool_call
        if call["name"] in NEEDS_APPROVAL and not auto_approve:
            try:
                answer = input(f"  Allow {call['name']}({call['args']})? [y/N] ")
            except EOFError:  # no keyboard (e.g. run by check.py): no approval
                answer = ""
            if answer.strip().lower() != "y":
                return ToolMessage(
                    content="The user refused this tool call. Do not try to change the index another way.",
                    tool_call_id=call["id"],
                    status="error",
                )
        return await handler(request)

    return ask_before_indexing


def build_agent(tools, auto_approve=False):
    return create_agent(
        model=ChatGoogleGenerativeAI(model=MODEL),
        tools=tools,
        system_prompt=(ROOT / "AGENTS.md").read_text(encoding="utf-8"),
        middleware=[
            approval_guardrail(auto_approve),
            ModelCallLimitMiddleware(run_limit=MAX_MODEL_CALLS),  # hard stop for runaway loops
        ],
    )


@asynccontextmanager
async def open_agent(auto_approve=False):
    """`async with open_agent() as agent:` keeps one MCP server running for many questions."""
    load_api_key()
    async with AsyncExitStack() as stack:
        yield build_agent(await load_mcp_tools(stack), auto_approve)


# 3. What happened: tool calls, results and the passages search returned.
def tool_trace(messages):
    lines = []
    for message in messages:
        if isinstance(message, AIMessage):
            lines += [f"call {c['name']}({c['args']})" for c in message.tool_calls]
        elif isinstance(message, ToolMessage):
            lines.append(f"result {message.text}")
    return lines


def retrieved_passages(trace):
    """(source, first page, last page) of every passage that search returned."""
    passages = set()
    for line in trace:
        if line.startswith("result "):
            for m in re.finditer(r"^\s*\d+\. " + CITATION.pattern, line[7:], re.MULTILINE):
                passages.add((m.group(1), int(m.group(2)), int(m.group(3) or m.group(2))))
    return passages


def check_citations(answer, trace):
    """The citation gate, in code. Returns (passed, reason, bad citations)."""
    passages = retrieved_passages(trace)
    if any(l.startswith("call add_document") for l in trace) and not any(l.startswith("call search") for l in trace):
        return True, "document added; no answer about the rules to check", []
    cited = citations(answer)
    bad = [
        tag for tag, source, first, last in cited
        if source is None or not any(source == s and first <= l and f <= last for s, f, l in passages)
    ]
    if bad:
        return False, "cites passages that search did not return", bad
    if answer.strip().startswith(REFUSAL):
        return True, "refusal", []
    if not cited:
        return False, "makes claims without any citation", []
    return True, "every citation matches a retrieved passage", []


def usage(messages):
    tokens = {"input": 0, "output": 0}
    for message in messages:
        meta = getattr(message, "usage_metadata", None) or {}
        tokens["input"] += meta.get("input_tokens", 0)
        tokens["output"] += meta.get("output_tokens", 0)
    return tokens


async def ask(agent, question):
    """Run one question. Returns a dict with the shown answer and everything evaluate.py needs."""
    started = time.perf_counter()
    try:
        result = await agent.ainvoke({"messages": [{"role": "user", "content": question}]})
        messages = result["messages"]
        raw = messages[-1].text.strip()
    except Exception as error:  # noqa: BLE001 - a failed run is a result to record, not a crash
        messages, raw = [], f"ERROR: {type(error).__name__}: {error}"
    trace = tool_trace(messages)
    passed, reason, bad = check_citations(raw, trace)
    shown = raw if passed else (
        f"Respuesta retenida por el control de citas: {reason}"
        + (f" ({', '.join(bad)})" if bad else "")
        + ". Consulta directamente la norma o reformula la pregunta."
    )
    return {
        "question": question,
        "answer": shown,
        "raw_answer": raw,
        "gate_passed": passed,
        "gate_reason": reason,
        "trace": trace,
        "searches": sum(line.startswith("call search") for line in trace),
        "tokens": usage(messages),
        "seconds": round(time.perf_counter() - started, 1),
    }


async def main(question):
    async with open_agent() as agent:
        r = await ask(agent, question)
    for line in r["trace"]:
        print(f"  {line[:300]}{'…' if len(line) > 300 else ''}", file=sys.stderr)
    print(r["answer"])
    print("\n(Orientación basada en la normativa indexada; en un proyecto real decide el instalador "
          "habilitado y la administración competente.)")
    if not r["gate_passed"] and r["raw_answer"] != r["answer"]:
        print(f"\n(withheld answer, for debugging: {r['raw_answer'][:500]})", file=sys.stderr)
    print(
        f"\n[{r['searches']} searches, {r['tokens']['input']} input + {r['tokens']['output']} output tokens, "
        f"{r['seconds']} s]"
    )


if __name__ == "__main__":
    asyncio.run(main(" ".join(sys.argv[1:]) or "¿Cuál es la altura mínima de los conectores de un punto de recarga?"))