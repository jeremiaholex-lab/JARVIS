"""JARVIS behavioral eval harness (roadmap #2).

Runs the case set in eval/cases.py against the shipping decision layers and
prints a per-category scorecard. Deterministic and offline by default: no
model, no network, finishes in about a second. `--live` additionally drives
the running orb over its websocket and asserts turn contracts.

Exit code: 0 = all pass, 1 = any failure. With a baseline file, regressions
(case passed before, fails now) are listed first — that is the whole point.

  python eval/run_eval.py
  python eval/run_eval.py --baseline eval/baseline.json --update-baseline
  python eval/run_eval.py --live [--url ws://127.0.0.1:8010/ws]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval.cases import CASES, LIVE_CASES  # noqa: E402
from jarvis.agent.routing import (  # noqa: E402
    looks_like_screen_request, needs_retry, route_turn, tool_nudge_needed,
)
from jarvis.security.tiers import PlannedAction, Tier, classify  # noqa: E402


# --------------------------------------------------------------- scripted turns
def _run_turn_case(utterance: str, spec: dict) -> None:
    """Drive the real run_turn with a scripted engine; assert the contract."""
    from jarvis.agent.turn import run_turn
    from jarvis.agent.tools import AgentTool
    from jarvis.security.tiers import Tier as _T

    script = list(spec.get("script", []))
    calls = []

    def call_engine(engine, msgs, tools, max_tokens=None):
        if not script:
            raise AssertionError(f"script exhausted on a {engine} call")
        resp = script.pop(0)
        calls.append({
            "engine": engine,
            "tools_offered": None if tools is None else [t["function"]["name"] for t in tools],
            "system": " || ".join(m.get("content", "") for m in msgs
                                  if m.get("role") == "system"),
        })
        if resp.get("engine") and resp["engine"] != engine:
            raise AssertionError(f"expected call on {resp['engine']!r}, got {engine!r}")
        if resp.get("sys_contains"):
            last = calls[-1]["system"]
            if resp["sys_contains"] not in last:
                raise AssertionError(f"system prompt lacks {resp['sys_contains']!r}: {last[:200]}")
        if resp.get("must_have_no_tools") and calls[-1]["tools_offered"] is not None:
            raise AssertionError("final round still offered tools")
        if resp.get("raise"):
            raise ConnectionError(resp["raise"])
        out = {}
        if "content" in resp:
            out["content"] = resp["content"]
        if "tool_calls" in resp:
            out["tool_calls"] = resp["tool_calls"]
        return out

    tool_names = spec.get("tools", [])
    impls = [AgentTool(n, "", {"type": "object", "properties": {}},
                       (lambda name: (lambda **kw: json.dumps({"ok": True, "result": f"canned-{name}"})))(n),
                       _T.READ)
             for n in tool_names]
    schemas = [{"type": "function",
                "function": {"name": n, "description": "test tool", "parameters": {"type": "object"}}}
               for n in tool_names]

    result = run_turn(
        utterance,
        system_prompt="You are a test assistant.",
        tools=schemas, tool_impls=impls, call_engine=call_engine,
        local_model="test-local", cloud_model="test-cloud",
        cloud_available=True, force_cloud=False,
        local_tokens_per_second=None,
        max_tool_rounds=spec.get("max_tool_rounds", 4),
    )
    exp = spec.get("expect", {})
    if calls and exp.get("no_local_calls"):
        if any(c["engine"] == "local" for c in calls):
            raise AssertionError("a local call happened but none was expected")
    if "engine_prefix" in exp and not result.engine.startswith(exp["engine_prefix"]):
        raise AssertionError(f"engine {result.engine!r} does not start with {exp['engine_prefix']!r}")
    if "answer" in exp and result.answer != exp["answer"]:
        raise AssertionError(f"answer {result.answer!r} != {exp['answer']!r}")
    if "answer_contains" in exp and exp["answer_contains"] not in result.answer:
        raise AssertionError(f"answer {result.answer!r} lacks {exp['answer_contains']!r}")
    if "tools_used" in exp:
        for name in exp["tools_used"]:
            if name not in result.tools_used:
                raise AssertionError(f"tool {name!r} not used; used={result.tools_used}")
    if script:
        raise AssertionError(f"{len(script)} scripted responses left unconsumed")


# --------------------------------------------------------------------- runner
def run_case(case: dict) -> None:
    kind, utterance, spec, *_note = case["check"]
    if kind == "route":
        kwargs = {k: v for k, v in spec.items()
                  if k in ("cloud_available", "force_cloud", "local_tokens_per_second")}
        route = route_turn(utterance, **kwargs)
        if route.engine != spec["expect"]:
            raise AssertionError(f"routed {route.engine!r} ({route.reason}), wanted {spec['expect']!r}")
        if spec.get("reason") and spec["reason"] not in route.reason:
            raise AssertionError(f"reason {route.reason!r} lacks {spec['reason']!r}")
    elif kind == "screen":
        got = looks_like_screen_request(utterance)
        if got != spec["expect"]:
            raise AssertionError(f"looks_like_screen_request -> {got}, wanted {spec['expect']}")
    elif kind == "keep":
        from jarvis.agent.user_model import explicit_statement
        got = explicit_statement(utterance)
        if got != spec["expect"]:
            raise AssertionError(f"explicit_statement -> {got!r}, wanted {spec['expect']!r}")
    elif kind == "research":
        from jarvis.agent.routing import looks_like_research_request
        got = looks_like_research_request(utterance)
        if got != spec["expect"]:
            raise AssertionError(f"looks_like_research_request -> {got}, wanted {spec['expect']}")
    elif kind == "nudge":
        got = tool_nudge_needed(utterance, used_tools=spec.get("used_tools", False))
        if got != spec["expect"]:
            raise AssertionError(f"tool_nudge_needed -> {got}, wanted {spec['expect']}")
    elif kind == "retry":
        got = needs_retry(utterance, words_in_question=spec.get("words", 6),
                          used_tools=spec.get("used_tools", False))
        if got != spec["expect"]:
            raise AssertionError(f"needs_retry -> {got}, wanted {spec['expect']}")
    elif kind == "tier":
        got = classify(PlannedAction(tool=utterance, params=spec.get("params", {})))
        want = Tier(spec["expect"])
        if got is not want:
            raise AssertionError(f"classify({utterance}) -> {got.value}, wanted {want.value}")
    elif kind == "recollect":
        from jarvis.agent.semantic import looks_like_recollection
        got = looks_like_recollection(utterance)
        if got != spec["expect"]:
            raise AssertionError(f"looks_like_recollection -> {got}, wanted {spec['expect']}")
    elif kind == "app":
        from jarvis.desktop.app_launch import prepare_open_app
        from jarvis.desktop.app_resolver import AppEntry
        index = [AppEntry(name, path) for name, path in spec.get("index", [])]
        got = prepare_open_app(utterance, index=index, aliases=spec.get("aliases"))
        expect = spec["expect"]
        if got.get("ok") != expect.get("ok"):
            raise AssertionError(f"prepare_open_app -> {got!r}, wanted ok={expect.get('ok')}")
        if expect.get("name") and got.get("name") != expect["name"]:
            raise AssertionError(f"resolved name {got.get('name')!r}, wanted {expect['name']!r}")
        if "candidates" in expect and got.get("candidates") != expect["candidates"]:
            raise AssertionError(f"candidates {got.get('candidates')!r}, wanted {expect['candidates']!r}")
    elif kind == "switch":
        from jarvis.agent.providers import parse_model_switch
        got = parse_model_switch(utterance)
        if got != spec["expect"]:
            raise AssertionError(f"parse_model_switch -> {got!r}, wanted {spec['expect']!r}")
    elif kind == "turn":
        _run_turn_case(utterance, spec)
    else:
        raise AssertionError(f"unknown check kind {kind!r}")


# ----------------------------------------------------------------------- live
def _drain_to_idle(conn, seconds: float) -> None:
    """Behave like the browser after a reply: the turn is not over when the
    text arrives — TTS is still streaming. Wait for tts_done, acknowledge
    playback, then wait for the orb to leave speaking. The orb rejects
    overlapping turns, so a case that skips this leaves the next nine errors."""
    deadline = time.time() + seconds
    acked = False
    while time.time() < deadline:
        try:
            m = json.loads(conn.recv(timeout=5))
        except Exception:  # noqa: BLE001 — quiet socket
            continue
        op = m.get("op")
        if op == "tts_done" and not acked:
            acked = True
            try:
                conn.send(json.dumps({"op": "playback_done", "id": m.get("id")}))
            except Exception:  # noqa: BLE001
                return
        if op == "state" and m.get("state") in ("idle", "listening"):
            return


def run_live(cases, url: str, timeout_s: float = 240.0):
    try:
        import websockets.sync.client as wsclient
    except ImportError:
        print("(websockets not installed; --live skipped)")
        return [("live-unavailable", "live", False, "websockets missing")]
    results = []
    try:
        conn = wsclient.connect(url, open_timeout=5)
    except OSError as exc:
        print(f"(orb not reachable at {url}: {exc.__class__.__name__}; --live skipped)")
        return []
    with conn:
        # Mute this eval session only (the flag is per connection): turns still
        # run and speak nothing, so a --live sweep cannot talk over the user.
        conn.send(json.dumps({"op": "mute", "on": True}))
        for index, case in enumerate(cases):
            sent = {"op": "chat", "text": case["utterance"]}
            if case["live"].get("force_cloud"):
                sent["force_cloud"] = True
            t0 = time.time()
            conn.send(json.dumps(sent))
            engine = None
            reply = None
            budget = timeout_s if index == 0 else timeout_s - 60  # first turn may cold-load the model
            while time.time() - t0 < budget:
                try:
                    msg = json.loads(conn.recv(timeout=budget))
                except Exception as exc:  # noqa: BLE001
                    results.append((case["id"], "live", False, f"ws error {exc!r}"))
                    _drain_to_idle(conn, 120)
                    break
                if msg.get("op") == "chat" and msg.get("role") == "assistant" \
                        and msg.get("engine") not in ("memory", "system"):
                    engine, reply = msg.get("engine", ""), msg.get("text", "")
                    break
                if msg.get("op") == "error":
                    results.append((case["id"], "live", False, f"server error: {msg.get('message')}"))
                    _drain_to_idle(conn, 120)
                    break
            else:
                results.append((case["id"], "live", False, "timed out waiting for reply"))
                _drain_to_idle(conn, 120)
                continue
            # Settle this connection's turn (see _drain_to_idle) before the
            # next send.
            _drain_to_idle(conn, 120)
            if engine is None:
                continue
            want = case["live"].get("expect_route_prefix")
            fail = None
            if want and not (engine or "").startswith(want):
                fail = f"engine {engine!r} not {want!r}-prefixed"
            elif len(reply or "") < case["live"].get("reply_min_chars", 1):
                fail = f"reply too short: {reply!r}"
            else:
                for phrase in case["live"].get("reply_not_contains", []):
                    if phrase.lower() in (reply or "").lower():
                        fail = f"reply contains {phrase!r}: {reply[:160]!r}"
                        break
            results.append((case["id"], "live", fail is None, fail or f"{round(time.time()-t0,1)}s"))
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", action="store_true", help="also drive the running orb")
    ap.add_argument("--url", default="ws://127.0.0.1:8010/ws")
    ap.add_argument("--json", metavar="PATH", help="write full results as JSON")
    ap.add_argument("--baseline", metavar="PATH", help="compare against a previous run")
    ap.add_argument("--update-baseline", action="store_true",
                    help="write the current results as the baseline")
    args = ap.parse_args()

    outcomes = OrderedDict()
    for case in CASES:
        t0 = time.perf_counter()
        try:
            run_case(case)
            outcomes[case["id"]] = {"cat": case["cat"], "pass": True,
                                    "ms": round((time.perf_counter() - t0) * 1000, 1)}
        except AssertionError as exc:
            outcomes[case["id"]] = {"cat": case["cat"], "pass": False, "why": str(exc)}
        except Exception as exc:  # noqa: BLE001 — a harness crash is still a FAIL line
            outcomes[case["id"]] = {"cat": case["cat"], "pass": False,
                                    "why": f"{exc.__class__.__name__}: {exc}"}

    if args.live:
        for cid, cat, ok, why in run_live(LIVE_CASES, args.url):
            outcomes[cid] = {"cat": cat, "pass": ok, "why": None if ok else why}

    by_cat = Counter()
    total = Counter()
    for cid, r in outcomes.items():
        total[r["cat"]] += 1
        by_cat[r["cat"]] += 1 if r["pass"] else 0

    width = max(len(c) for c in total)
    print("JARVIS behavioral eval")
    print("-" * (width + 22))
    for cat in sorted(total):
        ok, allc = by_cat[cat], total[cat]
        mark = "PASS" if ok == allc else "FAIL"
        print(f"  {cat:<{width}}  {ok:>2}/{allc:<2}  {mark}")
    failures = [(cid, r) for cid, r in outcomes.items() if not r["pass"]]
    if failures:
        print("\nfailures:")
        for cid, r in failures:
            print(f"  [{r['cat']}] {cid}: {r.get('why', '?')}")

    if args.baseline:
        bpath = Path(args.baseline)
        if bpath.exists():
            base = json.loads(bpath.read_text())
            base_pass = {k: v.get("pass") for k, v in base.items()}
            regress = [k for k, r in outcomes.items()
                       if not r["pass"] and base_pass.get(k)]
            fixed = [k for k, r in outcomes.items()
                     if r["pass"] and base_pass.get(k) is False]
            print(f"\nbaseline: {len(regress)} regression(s), {len(fixed)} newly fixed")
            for k in regress:
                print(f"  REGRESSION {k}: {outcomes[k].get('why')}")
            for k in fixed:
                print(f"  fixed      {k}")
        else:
            print(f"\n(no baseline at {bpath} yet — pass --update-baseline to create one)")
    if args.update_baseline:
        Path(args.baseline or (ROOT / "eval" / "baseline.json")).write_text(
            json.dumps(outcomes, indent=1, sort_keys=True))
        print("baseline updated")
    if args.json:
        Path(args.json).write_text(json.dumps(outcomes, indent=1, sort_keys=True))

    n, fails = len(outcomes), len(failures)
    print(f"\n{n - fails}/{n} passed" + ("" if args.live else "  (offline; --live adds the running orb)"))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
