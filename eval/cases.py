"""Behavioral case set for the JARVIS decision layers.

These are NOT unit tests of the regexes themselves — they are pinned
*behaviors* of whole utterances, the way a person would file a bug:
"asking X must route cloud", "saying Y must trigger the tool nudge".
They run offline against the shipping code (no model, no network, <1 s),
so a change to routing/vision/tiers/turn can be scored in one command.

Each case:
  id       stable, unique — appears in scorecards and baseline diffs
  cat      category shown in the scorecard
  check    one of: route | screen | tier | nudge | retry | turn
  live     optional dict for --live mode against the running orb
           (expected route engine prefix, tool subset it may use)

Run:  python eval/run_eval.py            (offline, deterministic)
      python eval/run_eval.py --live     (extra: drive the real orb)
"""
from __future__ import annotations

LONG_65 = " count the stars" * 35  # 105 words: over the 60-word long-form line

CASES = [
    # ---------------------------------------------------------------- routing
    # Routine conversation and things the local model can say from itself.
    {"id": "route-greeting", "cat": "routing", "check": ("route", "hey, how are you doing", {"expect": "local"})},
    {"id": "route-time", "cat": "routing", "check": ("route", "what's the time right now", {"expect": "local"})},
    {"id": "route-weather", "cat": "routing", "check": ("route", "what's the weather tomorrow", {"expect": "local"})},
    {"id": "route-joke", "cat": "routing", "check": ("route", "tell me a joke about compilers", {"expect": "local"})},
    {"id": "route-reminder", "cat": "routing", "check": ("route", "remind me to stretch every hour today", {"expect": "local"})},
    {"id": "route-thanks", "cat": "routing", "check": ("route", "thanks, you're awesome", {"expect": "local"})},
    {"id": "route-math", "cat": "routing", "check": ("route", "what is 12 times 7", {"expect": "local"})},
    {"id": "route-goodmorning", "cat": "routing", "check": ("route", "good morning jarvis", {"expect": "local"})},
    {"id": "route-explain", "cat": "routing", "check": ("route", "explain how a transformer model works", {"expect": "local"})},
    # Analytical / user-data / long-form: the chain leads with a flagship
    # model and every turn carries tools + recall, so these stay on the ladder.
    {"id": "route-analyze", "cat": "routing", "check": ("route",
        "can you analyze the pros and cons of migrating our database to postgres and write a summary",
        {"expect": "local"})},
    {"id": "route-weekly-summary", "cat": "routing", "check": ("route",
        "summarize everything I worked on this week across my projects and emails", {"expect": "local"})},
    {"id": "route-calendar", "cat": "routing", "check": ("route",
        "what do I have on my calendar tomorrow", {"expect": "local"})},
    {"id": "route-doc-search", "cat": "routing", "check": ("route",
        "search my documents for the lease agreement", {"expect": "local"})},
    {"id": "route-explicit-cloud", "cat": "routing", "check": ("route",
        "use the cloud for this one", {"expect": "cloud", "reason": "explicit escalation"})},
    {"id": "route-longform", "cat": "routing", "check": ("route", LONG_65.strip(), {"expect": "local"})},
    # Degraded-environment switches. Model speed is no longer a routing
    # factor: it existed to dodge a weak local model that has been removed.
    {"id": "route-slow-ignored", "cat": "routing", "check": ("route", "tell me a joke about compilers",
        {"expect": "local", "local_tokens_per_second": 2.1})},
    {"id": "route-slow-local-short", "cat": "routing", "check": ("route", "hi there",
        {"expect": "local", "local_tokens_per_second": 2.1},
        "two-word greetings are not worth a cloud round trip")},
    {"id": "route-no-key", "cat": "routing", "check": ("route", "what's the weather tomorrow",
        {"expect": "local", "cloud_available": False})},
    {"id": "route-force-cloud", "cat": "routing", "check": ("route", "what is 12 times 7",
        {"expect": "cloud", "force_cloud": True})},
    {"id": "route-force-cloud-no-key", "cat": "routing", "check": ("route", "what is 12 times 7",
        {"expect": "local", "force_cloud": True, "cloud_available": False})},

    # ------------------------------------------------------------ screen steer
    {"id": "screen-looking-at", "cat": "screen", "check": ("screen", "what am I looking at", {"expect": True})},
    {"id": "screen-on-screen", "cat": "screen", "check": ("screen", "what's on my screen", {"expect": True})},
    {"id": "screen-read-window", "cat": "screen", "check": ("screen", "read this window to me", {"expect": True})},
    {"id": "screen-see-window", "cat": "screen", "check": ("screen", "can you see this window?", {"expect": True})},
    {"id": "screen-which-window", "cat": "screen", "check": ("screen", "which window am I in", {"expect": True})},
    {"id": "screen-earlier-error", "cat": "screen", "check": ("screen", "did I see that error on my screen earlier", {"expect": True})},
    {"id": "screen-weather", "cat": "screen", "check": ("screen", "what's the weather", {"expect": False})},
    {"id": "screen-brightness", "cat": "screen", "check": ("screen", "turn up the screen brightness", {"expect": False},
        "hardware imperative: a screenshot answers nothing")},
    {"id": "screen-lower-display", "cat": "screen", "check": ("screen", "lower the display brightness", {"expect": False})},
    {"id": "screen-clean-monitor", "cat": "screen", "check": ("screen", "clean my monitor", {"expect": False})},
    {"id": "screen-window-settings", "cat": "screen", "check": ("screen", "open window settings", {"expect": False})},

    # ------------------------------------------------------------ tool nudge
    {"id": "nudge-cant-access", "cat": "nudge", "check": ("nudge", "I can't access your files",
        {"used_tools": False, "expect": True}, "the live-observed failure phrase")},
    {"id": "nudge-cannot-determine", "cat": "nudge", "check": ("nudge",
        "I cannot determine your building access code from public information",
        {"used_tools": False, "expect": True},
        "the dodge the local model actually produced on a recall turn")},
    {"id": "nudge-adverb-dodge", "cat": "nudge", "check": ("nudge",
        "I cannot directly access or retrieve building elevator access codes",
        {"used_tools": False, "expect": True},
        "a one-word adverb must not hide the same dodge")},
    {"id": "nudge-not-able", "cat": "nudge", "check": ("nudge", "I wasn't able to find that in your memory",
        {"used_tools": False, "expect": True})},
    {"id": "nudge-cannot-see", "cat": "nudge", "check": ("nudge", "I cannot see that document",
        {"used_tools": False, "expect": True})},
    {"id": "nudge-promise-search", "cat": "nudge", "check": ("nudge", "I'll search your documents now",
        {"used_tools": False, "expect": True})},
    {"id": "nudge-promise-check", "cat": "nudge", "check": ("nudge", "Sure, I will look at your calendar",
        {"used_tools": False, "expect": True})},
    {"id": "nudge-good-answer", "cat": "nudge", "check": ("nudge", "Your lease is in Documents/lease.pdf",
        {"used_tools": False, "expect": False})},
    {"id": "nudge-used-tools", "cat": "nudge", "check": ("nudge", "I can't access your files",
        {"used_tools": True, "expect": False},
        "a tool already ran; the finding is honest, not a dodge")},

    # ----------------------------------------------------------- conf. retry
    {"id": "retry-not-sure", "cat": "retry", "check": ("retry", "I'm not sure, perhaps Lyon?",
        {"words": 6, "expect": True})},
    {"id": "retry-short-correct", "cat": "retry", "check": ("retry", "Paris.",
        {"words": 6, "expect": False}, "brevity is not weakness (live finding)")},
    {"id": "retry-hedge-unanchored", "cat": "retry", "check": ("retry", "I think maybe it could possibly be around there",
        {"words": 6, "expect": False}, "mid-sentence hedges deliberately do not burn cloud quota")},
    {"id": "retry-two-word-q", "cat": "retry", "check": ("retry", "I'm not sure",
        {"words": 2, "expect": False})},
    {"id": "retry-tools-ran", "cat": "retry", "check": ("retry", "I don't have any calendar events tomorrow",
        {"words": 6, "used_tools": True, "expect": False})},

    # ------------------------------------------------------------------- tiers
    {"id": "tier-read-file", "cat": "tier", "check": ("tier", "read_file", {"params": {"path": "x"}, "expect": "read"})},
    {"id": "tier-file-edit", "cat": "tier", "check": ("tier", "jarvis_file_edit", {"expect": "destructive"})},
    {"id": "tier-shell-ls", "cat": "tier", "check": ("tier", "shell_exec", {"params": {"command": "ls"}, "expect": "write"})},
    {"id": "tier-shell-rm", "cat": "tier", "check": ("tier", "shell_exec", {"params": {"command": "rm -rf /tmp/x"}, "expect": "destructive"})},
    {"id": "tier-look-screen", "cat": "tier", "check": ("tier", "look_at_screen", {"expect": "read"})},
    {"id": "tier-recall-memory", "cat": "tier", "check": ("tier", "recall_memory", {"params": {"query": "x"}, "expect": "read"},
        "semantic recall reads the episode store; it writes nothing")},
    {"id": "tier-list-windows", "cat": "tier", "check": ("tier", "list_open_windows", {"expect": "read"})},
    {"id": "tier-projects-write", "cat": "tier", "check": ("tier", "jarvis_projects_write",
        {"params": {"action": "add_project"}, "expect": "write"})},
    {"id": "tier-projects-read", "cat": "tier", "check": ("tier", "jarvis_projects_read",
        {"params": {"view": "projects"}, "expect": "read"})},
    {"id": "tier-web-search", "cat": "tier", "check": ("tier", "web_search", {"params": {"q": "x"}, "expect": "read"})},
    {"id": "tier-browser-nav", "cat": "tier", "check": ("tier", "browser_navigate", {"params": {"url": "x"}, "expect": "read"})},
    {"id": "tier-sandbox", "cat": "tier", "check": ("tier", "sandbox_execute",
        {"params": {"code": "print(1)"}, "expect": "read"}, "isolated sandbox, no host access")},
    {"id": "tier-task-enqueue", "cat": "tier", "check": ("tier", "jarvis_task_enqueue", {"expect": "read"},
        "enqueues a bounded read-only task; the task tier is enforced separately")},
    {"id": "tier-unknown-default", "cat": "tier", "check": ("tier", "mystery_tool", {"expect": "write"},
        "unknown tools never default to free")},

    # --------------------------------------------------------- recollection
    {"id": "recollect-what-told", "cat": "recollect", "check": ("recollect",
        "what did I tell you about the lease", {"expect": True})},
    {"id": "recollect-remember", "cat": "recollect", "check": ("recollect",
        "do you remember my elevator code", {"expect": True})},
    {"id": "recollect-my-code", "cat": "recollect", "check": ("recollect",
        "what is my access code", {"expect": True})},
    {"id": "recollect-did-we-talk", "cat": "recollect", "check": ("recollect",
        "did we talk about the oracle deadline", {"expect": True})},
    {"id": "recollect-remind-what", "cat": "recollect", "check": ("recollect",
        "remind me what we decided last time", {"expect": True})},
    {"id": "recollect-tallest", "cat": "recollect", "check": ("recollect",
        "what is the tallest building", {"expect": False}, "no past reference: no recall spend")},
    {"id": "recollect-future-reminder", "cat": "recollect", "check": ("recollect",
        "remind me to call mom at five", {"expect": False})},
    {"id": "recollect-weather", "cat": "recollect", "check": ("recollect",
        "what's the weather", {"expect": False})},
    {"id": "recollect-brightness", "cat": "recollect", "check": ("recollect",
        "turn up the screen brightness", {"expect": False})},

    # ------------------------------------------------- scripted full turns
    {
        "id": "turn-simple-local", "cat": "turn",
        "check": ("turn", "hello there", {
            "script": [{"content": "Hello! What can I do for you?"}],
            "expect": {"engine_prefix": "local", "answer": "Hello! What can I do for you?"},
        }),
    },
    {
        "id": "turn-tool-roundtrip", "cat": "turn",
        "check": ("turn", "read my notes file", {
            "tools": ["read_file"],
            "script": [
                {"tool_calls": [{"id": "1", "name": "read_file", "arguments": "{\"path\": \"notes.txt\"}"}]},
                {"content": "Your notes say: buy oat milk."},
            ],
            "expect": {"tools_used": ["read_file"], "answer": "Your notes say: buy oat milk."},
        }),
    },
    {
        "id": "turn-local-fail-cloud", "cat": "turn",
        "check": ("turn", "what's the capital of France", {
            "script": [{"raise": "ConnectionError"}, {"engine": "cloud", "content": "Paris."}],
            "expect": {"engine_prefix": "cloud (fallback)", "answer": "Paris."},
        }),
    },
    {
        "id": "turn-nudge-loop", "cat": "turn",
        "check": ("turn", "where is my lease", {
            "tools": ["jarvis_rag_search"],
            "script": [
                {"content": "I can't access your files"},
                {"sys_contains": "Call the tool now",
                 "content": "It's in Documents/legal/lease.pdf."},
            ],
            "expect": {"answer": "It's in Documents/legal/lease.pdf."},
        }),
    },
    {
        "id": "turn-screen-steer", "cat": "turn",
        "check": ("turn", "what's on my screen", {
            "tools": ["look_at_screen", "list_open_windows"],
            "script": [
                {"sys_contains": "look_at_screen now", "content": "You have a VS Code window open."},
            ],
            "expect": {"answer_contains": "VS Code"},
        }),
    },
    {
        "id": "turn-tool-limit-no-tools-last", "cat": "turn",
        "check": ("turn", "check every file one by one", {
            "tools": ["read_file"],
            "script": [{"tool_calls": [{"id": str(k), "name": "read_file", "arguments": "{}"}]}
                       for k in range(3)]
                      + [{"must_have_no_tools": True, "content": "Here is what I found."}],
            "max_tool_rounds": 4,
            "expect": {"answer": "Here is what I found."},
        }),
    },
    {
        "id": "turn-explicit-cloud", "cat": "turn",
        "check": ("turn", "actually, use the cloud model for this one", {
            "script": [{"engine": "cloud", "content": "Deep answer served."}],
            "expect": {"engine_prefix": "cloud", "no_local_calls": True},
        }),
    },
    {
        "id": "turn-confidence-escalation", "cat": "turn",
        "check": ("turn", "what city is the prefecture of the Rhone", {
            "script": [
                {"engine": "local", "content": "I'm not sure, perhaps Lyon?"},
                {"engine": "cloud", "content": "Lyon."},
            ],
            "expect": {"engine_prefix": "cloud (escalated)", "answer": "Lyon."},
        }),
    },
    {
        "id": "turn-recall-tool", "cat": "turn",
        "check": ("turn", "what did I tell you about the lease", {
            "tools": ["recall_memory"],
            "script": [
                {"tool_calls": [{"id": "1", "name": "recall_memory",
                                 "arguments": "{\"query\": \"lease\"}"}]},
                {"content": "You said the lease renews on the first."},
            ],
            "expect": {"tools_used": ["recall_memory"],
                       "answer_contains": "renews on the first"},
        }),
    },
    # ------------------------------------------------------------ brain switch
    {"id": "switch-to-35", "cat": "switch", "check": ("switch", "switch to gemini 3.5",
        {"expect": "gemini35"})},
    {"id": "switch-to-25", "cat": "switch", "check": ("switch", "use gemini 2.5 instead",
        {"expect": "gemini25"})},
    {"id": "switch-to-agnes", "cat": "switch", "check": ("switch", "go back to the cloud model",
        {"expect": "agnes"})},
    {"id": "switch-to-auto", "cat": "switch", "check": ("switch", "switch to automatic",
        {"expect": "auto"})},
    {"id": "switch-negation", "cat": "switch", "check": ("switch", "don't use gemini 2.5",
        {"expect": None}, "a negation must never flip the brain")},
    {"id": "switch-question", "cat": "switch", "check": ("switch", "what is gemini 3.5",
        {"expect": None})},
    {"id": "switch-not-an-instruction", "cat": "switch", "check": ("switch",
        "tell me a joke about a robot", {"expect": None})},
]

# Optional live checks (only run with --live against the running orb).
LIVE_CASES = [
    {"id": "live-greeting", "cat": "live", "utterance": "hello, who are you",
     "live": {"expect_route_prefix": "local", "reply_min_chars": 10}},
    {"id": "live-calendar-route", "cat": "live", "utterance": "what do I have on my calendar tomorrow",
     "live": {"expect_route_prefix": "local", "reply_min_chars": 5}},
    {"id": "live-time", "cat": "live", "utterance": "what's the time right now",
     "live": {"expect_route_prefix": "local", "reply_min_chars": 3}},
    {"id": "live-force-cloud", "cat": "live", "utterance": "what is 12 times 7",
     "live": {"force_cloud": True, "expect_route_prefix": "cloud"}},
]
