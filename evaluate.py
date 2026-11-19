"""
Assignment 3: evaluation (`uv run python main.py evaluate`).

Built from session 10's 07-own-harness/evaluate.py. Four stages, all saved in eval/:

1. Retrieval. Hit rate at k = 1, 3, 5 for each design (chunking "structure" or
   "fixed", search "dense" or "hybrid"), and the words the top k add to the
   prompt. A hit = one of the top k chunks comes from an expected document and
   overlaps an expected page. The questions are embedded once and saved in
   eval/query_vectors.json, so comparing designs costs no more embeddings.
   -> eval/retrieval.json

2. Agent answers on every question, through agent.py and server.py, exactly as a
   user would get them. Saved in eval/answers.json and reused while the question,
   AGENTS.md, the model and the search design stay the same, so running this again
   does not spend the daily quota twice. `python evaluate.py --fresh` reruns all.
   -> eval/answers.json, eval/answers_for_grading.csv (no verdicts: grade these
      yourself in eval/human_labels.csv before you read the judge's)

3. Checks. In code: are the citations valid, did it refuse when it should, did it
   cite an expected document. With an LLM judge and a rubric, given the reference
   answer and the retrieved passages as evidence: is it correct, is every claim
   supported. -> eval/results.csv

4. Agreement between the judge and eval/human_labels.csv, outcome counts, a first
   guess at the cause of each failure, and cost per question. -> eval/summary.json
"""

import asyncio
import csv
import hashlib
import json
import re
import statistics
import sys
import time
from pathlib import Path

from langchain_google_genai import ChatGoogleGenerativeAI
from pydantic import BaseModel, Field

import index
from agent import MODEL, REFUSAL, ask, citations, open_agent, retrieved_passages

ROOT = Path(__file__).resolve().parent
EVAL = ROOT / "eval"
QUESTIONS = EVAL / "questions.csv"
QUERY_VECTORS = EVAL / "query_vectors.json"
ANSWERS = EVAL / "answers.json"
VERDICTS = EVAL / "verdicts.json"
HUMAN_LABELS = EVAL / "human_labels.csv"

# The class used a stronger judge, but gemini-3.5-flash allows only 20 requests a day on the
# free tier, fewer than the 36 answers to grade. Flash-lite has the quota; being the same model
# as the agent, its verdicts are checked against human_labels.csv (agreement in summary.json).
JUDGE_MODEL = "gemini-3.5-flash-lite"
KS = [1, 3, 5]
DESIGNS = [(chunking, method) for chunking in index.DESIGNS for method in ("dense", "hybrid")]
CORPUS_ONLY = {"source": {"$in": [d["id"] for d in index.DOCUMENTS]}}  # documents added later don't count
PAUSE = 4  # seconds between agent questions and between judge calls: free-tier requests per minute


# ---------------------------------------------------------------- test set


def page_range(text):
    m = re.search(r"pp?\. (\d+)(?:-(\d+))?", text)
    return (int(m.group(1)), int(m.group(2) or m.group(1))) if m else None


def load_questions():
    """Rows of questions.csv, each with its expected passages as (source, first page, last page)."""
    with QUESTIONS.open(newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r["question"].strip()]
    for r in rows:
        sources = [s.strip() for s in r["expected_source"].split(";") if s.strip()]
        locations = [l.strip() for l in r["expected_location"].split(";") if l.strip()]
        r["kind"] = r["kind"].strip().lower()
        r["expected"] = [(s, *page_range(l)) for s, l in zip(sources, locations) if page_range(l)]
    return rows


def covers(hit, passage):
    source, first, last = passage
    return hit["source"] == source and hit["first_page"] <= last and first <= hit["last_page"]


# ---------------------------------------------------------------- 1. retrieval


def query_vectors(questions):
    """One vector per question, embedded once and saved: re-running the comparison is free."""
    saved = json.loads(QUERY_VECTORS.read_text()) if QUERY_VECTORS.exists() else {}
    missing = [q for q in questions if q not in saved]
    for start in range(0, len(missing), 100):
        batch = missing[start : start + 100]
        vectors = index.embed_texts([f"task: search result | query: {q}" for q in batch])
        saved.update(zip(batch, vectors))
        QUERY_VECTORS.write_text(json.dumps(saved))
    if missing:
        print(f"Embedded {len(missing)} new questions (saved in {QUERY_VECTORS.name}).")
    return saved


def retrieval_eval(rows):
    answerable = [r for r in rows if r["kind"] != "unanswerable"]
    vectors = query_vectors([r["question"] for r in rows])
    results = {}
    for chunking, method in DESIGNS:
        name = f"{chunking}+{method}"
        per_question = []
        for r in rows:
            hits = index.retrieve(r["question"], k=max(KS), design=chunking,
                                  query_vector=vectors[r["question"]], where=CORPUS_ONLY, method=method)
            first_hit = next((n for n, h in enumerate(hits, 1) if any(covers(h, p) for p in r["expected"])), None)
            all_found = all(any(covers(h, p) for h in hits) for p in r["expected"]) if r["expected"] else None
            per_question.append({
                "id": r["id"], "kind": r["kind"], "rank": first_hit, "all_passages_at_5": all_found,
                "top1_score": hits[0]["score"] if hits else None,
                "words": {k: sum(len(h["text"].split()) for h in hits[:k]) for k in KS},
            })
        scored = [p for p in per_question if p["kind"] != "unanswerable"]
        multi = [p for p in scored if p["kind"] == "multi"]
        results[name] = {
            "hit_rate": {k: round(sum(p["rank"] is not None and p["rank"] <= k for p in scored) / len(scored), 3) for k in KS},
            "multi_all_passages_at_5": round(sum(bool(p["all_passages_at_5"]) for p in multi) / len(multi), 3) if multi else None,
            "avg_words_added": {k: round(statistics.mean(p["words"][k] for p in per_question)) for k in KS},
            "per_question": per_question,
        }
    best = max(results, key=lambda n: (results[n]["hit_rate"][3], results[n]["hit_rate"][1], -results[n]["avg_words_added"][3]))

    print(f"\n=== Retrieval: hit rate on {len(answerable)} answerable questions")
    print(f"{'design':<20} {'@1':>6} {'@3':>6} {'@5':>6}   both passages@5 (multi)   words added @1/@3/@5")
    for name, r in results.items():
        h, w = r["hit_rate"], r["avg_words_added"]
        print(f"{name:<20} {h[1]:>6.0%} {h[3]:>6.0%} {h[5]:>6.0%}   {r['multi_all_passages_at_5'] or 0:>14.0%}"
              f"            {w[1]}/{w[3]}/{w[5]}")
    in_use = f"{index.CHOSEN_DESIGN}+{index.SEARCH_METHOD}"
    print(f"Best by hit rate at 3 (then at 1, then fewer words): {best}. In use by server.py: {in_use}.")
    if best != in_use:
        print("  To switch, set CHOSEN_DESIGN and SEARCH_METHOD in index.py and run evaluate again.")
    (EVAL / "retrieval.json").write_text(json.dumps({"best": best, "in_use": in_use, "designs": results}, indent=1))
    return results


# ---------------------------------------------------------------- 2. agent answers


def fingerprint(question):
    """Answers are reused only while nothing that shapes them has changed."""
    rules = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    key = "|".join([question, rules, MODEL, index.CHOSEN_DESIGN, index.SEARCH_METHOD])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


async def run_agent(rows, fresh=False):
    saved = {} if fresh or not ANSWERS.exists() else json.loads(ANSWERS.read_text())
    todo = [r for r in rows if saved.get(r["id"], {}).get("fingerprint") != fingerprint(r["question"])
            or saved[r["id"]]["raw_answer"].startswith("ERROR")]
    print(f"\n=== Agent: {len(rows) - len(todo)} answers reused, {len(todo)} to run")
    if todo:
        async with open_agent(auto_approve=True) as agent:
            for n, r in enumerate(todo, start=1):
                result = await ask(agent, r["question"])
                saved[r["id"]] = {"id": r["id"], "fingerprint": fingerprint(r["question"]), **result}
                ANSWERS.write_text(json.dumps(saved, indent=1, ensure_ascii=False))  # save as we go
                print(f"  {n}/{len(todo)} {r['id']}: {result['seconds']} s, {result['searches']} searches, "
                      f"gate {'ok' if result['gate_passed'] else 'WITHHELD'}")
                time.sleep(PAUSE)

    with (EVAL / "answers_for_grading.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "kind", "question", "reference_answer", "answer_shown", "answer_before_gate", "cited"])
        for r in rows:
            a = saved[r["id"]]
            cited = sorted({c[0] for c in citations(a["raw_answer"])})
            writer.writerow([r["id"], r["kind"], r["question"], r["reference_answer"], a["answer"], a["raw_answer"], " ".join(cited)])
    return saved


# ---------------------------------------------------------------- 3. checks in code and by the judge


def code_checks(row, answer):
    raw = answer["raw_answer"]
    refused = raw.startswith(REFUSAL) or not answer["gate_passed"]  # a withheld answer reaches the user as no answer
    retrieved = retrieved_passages(answer["trace"])
    cited_sources = {c[1] for c in citations(raw) if c[1]}
    expected_sources = {p[0] for p in row["expected"]}
    return {
        "error": raw.startswith("ERROR"),
        "gate_passed": answer["gate_passed"],
        "refused": refused,
        "refusal_ok": refused == (row["kind"] == "unanswerable"),
        "citations_valid": answer["gate_passed"] or answer["gate_reason"] != "cites passages that search did not return",
        "has_citation": bool(cited_sources),
        "cites_expected_source": bool(cited_sources & expected_sources) if expected_sources else None,
        "expected_retrieved": any(
            s == es and f <= el and ef <= l for (s, f, l) in retrieved for (es, ef, el) in row["expected"]
        ) if row["expected"] else None,
    }


class Verdict(BaseModel):
    reasoning: str = Field(description="Two to four sentences checking the answer against each criterion, before deciding.")
    correct: bool = Field(description="Criterion 1.")
    supported: bool = Field(description="Criterion 2.")
    unsupported_claims: list[str] = Field(description="Each claim in the answer that the passages do not support; empty if none.")


JUDGE_PROMPT = """You grade an assistant that answers questions about Spanish and EU regulations on electric-vehicle charging, using passages retrieved from those regulations. A wrong answer can lead to an unsafe installation, so be strict.

Criteria:
1. Correct. For an answerable question: the answer gives the key facts of the reference answer (numbers, units, conditions, who must do what), with no fact that contradicts it. Extra facts are fine if they are true according to the passages. Missing a key fact fails. For an unanswerable question: the answer says the documents do not cover it and gives no substantive answer; any attempt to answer from general knowledge fails. A refusal to an answerable question fails.
2. Supported. Every factual claim in the answer appears in the retrieved passages below. A claim that is not in them fails, even if it is true. A pure refusal is supported.

Question ({kind}): {question}
Reference answer: {reference}
Retrieved passages (everything the assistant saw, recorded by the harness):
{passages}

Answer to grade:
{answer}"""


async def judge_all(rows, answers):
    saved = json.loads(VERDICTS.read_text()) if VERDICTS.exists() else {}
    judge = ChatGoogleGenerativeAI(model=JUDGE_MODEL, temperature=0).with_structured_output(Verdict)
    rubric = hashlib.sha256(JUDGE_PROMPT.encode()).hexdigest()[:8]
    todo = []
    for r in rows:
        a = answers[r["id"]]
        key = hashlib.sha256((JUDGE_MODEL + rubric + a["raw_answer"] + r["reference_answer"]).encode()).hexdigest()[:16]
        if saved.get(r["id"], {}).get("key") != key:
            todo.append((r, a, key))
    print(f"\n=== Judge: {len(rows) - len(todo)} verdicts reused, {len(todo)} to grade")
    for r, a, key in todo:
        passages = "\n\n".join(line[7:] for line in a["trace"] if line.startswith("result ")) or "(no search was made)"
        prompt = JUDGE_PROMPT.format(kind=r["kind"], question=r["question"], reference=r["reference_answer"],
                                     passages=passages, answer=a["raw_answer"])
        try:
            verdict = await judge.ainvoke(prompt)
            saved[r["id"]] = {"key": key, **verdict.model_dump()}
        except Exception as error:  # noqa: BLE001 - record it and go on; it is retried next run
            if "PerDay" in str(error):
                print(f"  daily request limit of {JUDGE_MODEL} reached at {r['id']}; the verdicts so far are saved,"
                      " run evaluate again tomorrow to grade the rest.")
                break
            print(f"  judge failed on {r['id']}: {type(error).__name__}: {str(error)[:200]}")
        VERDICTS.write_text(json.dumps(saved, indent=1, ensure_ascii=False))
        time.sleep(PAUSE)
    return saved


# ---------------------------------------------------------------- 4. outcomes, failures, agreement, cost


def outcome(row, checks, verdict):
    """What the user got, in the categories of the brief."""
    if row["kind"] == "unanswerable":
        return "correct refusal" if checks["refused"] else "made up"
    if not checks["gate_passed"]:
        return "withheld by citation gate"
    if checks["refused"]:
        return "wrong refusal"
    if not verdict:
        return "not graded"
    if verdict["correct"] and verdict["supported"]:
        return "correct"
    if verdict["correct"]:
        return "correct but unsupported"
    return "wrong"


def likely_cause(row, checks, verdict, result):
    """A first guess at the cause of a failure; check each one by hand (bad test question is yours to spot)."""
    if result in ("correct", "correct refusal", "not graded"):
        return ""
    if result == "made up":
        return "unsupported claim"
    if checks["expected_retrieved"] is False:
        return "retrieval miss"
    if result == "withheld by citation gate":
        return "unsupported claim"  # no citation, or one the search never returned
    if result == "wrong refusal":
        return "wrong refusal"
    if result == "correct but unsupported" or (verdict and not verdict["supported"]):
        return "unsupported claim"
    return "wrong answer from the right passage"


def agreement(rows, verdicts):
    if not HUMAN_LABELS.exists():
        return None
    text = HUMAN_LABELS.read_text(encoding="utf-8-sig")  # -sig: Excel and Numbers add a hidden mark at the start
    lines = [line for line in text.splitlines() if line.strip()]  # a blank first line would hide the header
    if not lines:
        return {"graded": 0}
    delimiter = ";" if lines[0].count(";") > lines[0].count(",") else ","  # Spanish Excel uses ;
    label_rows = [{(k or "").strip().lower(): (v or "") for k, v in r.items()} for r in csv.DictReader(lines, delimiter=delimiter)]
    labels = {r["id"].strip(): r for r in label_rows if r.get("id", "").strip()}
    if not labels:
        print(f"  WARNING: could not read any row from {HUMAN_LABELS.name}; its first line should be "
              f"'id,correct,supported,notes' but is {lines[0][:60]!r}")
        return {"graded": 0}
    pairs = [(labels[r["id"]], verdicts[r["id"]]) for r in rows if r["id"] in labels and r["id"] in verdicts]
    if not pairs:
        return {"graded": 0}
    out = {"graded": len(pairs)}
    for field in ("correct", "supported"):
        same = [int(h[field].strip() or 0) == int(v[field]) for h, v in pairs]
        out[f"{field}_agreement"] = round(sum(same) / len(same), 3)
    out["disagreements"] = [
        {"id": h["id"], "human": {"correct": h["correct"], "supported": h["supported"]},
         "judge": {"correct": int(v["correct"]), "supported": int(v["supported"])}, "judge_reasoning": v["reasoning"]}
        for h, v in pairs
        if int(h["correct"].strip() or 0) != int(v["correct"]) or int(h["supported"].strip() or 0) != int(v["supported"])
    ]
    return out


def report(rows, answers, verdicts):
    table = []
    for r in rows:
        a, v = answers[r["id"]], verdicts.get(r["id"])
        c = code_checks(r, a)
        result = outcome(r, c, v)
        table.append({
            "id": r["id"], "kind": r["kind"], "outcome": result, "likely_cause": likely_cause(r, c, v, result),
            **{k: (int(x) if isinstance(x, bool) else x) for k, x in c.items()},
            "judge_correct": int(v["correct"]) if v else "", "judge_supported": int(v["supported"]) if v else "",
            "judge_reasoning": v["reasoning"] if v else "",
            "unsupported_claims": " | ".join(v["unsupported_claims"]) if v else "",
            "gate": "ok" if a["gate_passed"] else f"withheld: {a['gate_reason']}",
            "searches": a["searches"], "input_tokens": a["tokens"]["input"],
            "output_tokens": a["tokens"]["output"], "seconds": a["seconds"],
        })
    with (EVAL / "results.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(table[0]))
        writer.writeheader()
        writer.writerows(table)

    counts = {}
    for t in table:
        counts[t["outcome"]] = counts.get(t["outcome"], 0) + 1
    causes = {}
    for t in table:
        if t["likely_cause"]:
            causes[t["likely_cause"]] = causes.get(t["likely_cause"], 0) + 1
    ok = [t for t in table if not t["error"]]
    summary = {
        "questions": len(table),
        "outcomes": counts,
        "likely_causes": causes,
        "code_checks": {
            "refusal_ok": sum(t["refusal_ok"] for t in table),
            "answers_with_invalid_citations_before_gate": sum(not t["citations_valid"] for t in table),
            "withheld_by_gate": sum(t["gate"] != "ok" for t in table),
            "cites_expected_source": sum(bool(t["cites_expected_source"]) for t in table if t["kind"] != "unanswerable"),
            "errors": sum(t["error"] for t in table),
        },
        "agreement_with_human_labels": agreement(rows, verdicts),
        "cost_per_question": {
            "input_tokens_mean": round(statistics.mean(t["input_tokens"] for t in ok)) if ok else None,
            "output_tokens_mean": round(statistics.mean(t["output_tokens"] for t in ok)) if ok else None,
            "seconds_mean": round(statistics.mean(t["seconds"] for t in ok), 1) if ok else None,
            "seconds_median": round(statistics.median(t["seconds"] for t in ok), 1) if ok else None,
            "searches_mean": round(statistics.mean(t["searches"] for t in ok), 2) if ok else None,
        },
        "model": MODEL, "judge_model": JUDGE_MODEL,
        "design": f"{index.CHOSEN_DESIGN}+{index.SEARCH_METHOD}",
    }
    (EVAL / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False))

    print("\n=== End to end")
    for name, n in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {name:<26} {n}")
    cc = summary["code_checks"]
    print(f"  refusal decision right in code: {cc['refusal_ok']}/{len(table)}; withheld by the citation gate: "
          f"{cc['withheld_by_gate']}; errors: {cc['errors']}")
    if causes:
        print("  likely causes of failures: " + ", ".join(f"{k} {v}" for k, v in causes.items()))
    ag = summary["agreement_with_human_labels"]
    if ag and ag.get("graded"):
        print(f"  judge vs you on {ag['graded']} answers: correct {ag['correct_agreement']:.0%}, "
              f"supported {ag['supported_agreement']:.0%}, {len(ag['disagreements'])} disagreements")
    else:
        print("  no human labels yet: grade eval/answers_for_grading.csv into eval/human_labels.csv")
    cost = summary["cost_per_question"]
    print(f"  per question: {cost['input_tokens_mean']} input + {cost['output_tokens_mean']} output tokens, "
          f"{cost['seconds_mean']} s mean, {cost['searches_mean']} searches")
    print("Saved eval/retrieval.json, answers.json, answers_for_grading.csv, verdicts.json, results.csv, summary.json")


async def main(fresh=False):
    index.load_api_key()
    rows = load_questions()
    retrieval_eval(rows)
    answers = await run_agent(rows, fresh)
    verdicts = await judge_all(rows, answers)
    report(rows, answers, verdicts)


if __name__ == "__main__":
    asyncio.run(main(fresh="--fresh" in sys.argv))