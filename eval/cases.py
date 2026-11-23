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

    # ---------------------------------------------------- live-facts steering
    # The bug this pins: "top three technology headlines right now" was answered
    # from training data in 4 s, confidently and wrongly, without the search
    # tool being called. Own-data questions must NOT be stolen by search.
    {"id": "research-headlines", "cat": "research", "check": ("research",
        "what are the top three technology headlines right now", {"expect": True})},
    {"id": "research-latest-release", "cat": "research", "check": ("research",
        "what is the latest python version released", {"expect": True})},
    {"id": "research-weather", "cat": "research", "check": ("research",
        "will it rain this afternoon", {"expect": True},
        "'raining' lives in the pattern; a forecast is not in the weights")},
    {"id": "research-prices", "cat": "research", "check": ("research",
        "how much is bitcoin worth today", {"expect": True})},
    {"id": "research-scores", "cat": "research", "check": ("research",
        "who won the match last night", {"expect": True})},
    {"id": "research-own-data", "cat": "research", "check": ("research",
        "what are my deadlines today", {"expect": False},
        "the user's own schedule belongs to the project tools, not the web")},
    {"id": "research-own-files", "cat": "research", "check": ("research",
        "what is the latest change in my notes", {"expect": False})},
    {"id": "research-screen", "cat": "research", "check": ("research",
        "what's on my screen right now", {"expect": False},
        "two steers must never both claim one turn")},
    {"id": "research-self", "cat": "research", "check": ("research",
        "what can you do for me", {"expect": False})},
    {"id": "research-static-fact", "cat": "research", "check": ("research",
        "how does a transformer attention layer work", {"expect": False})},

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
    {"id": "switch-slash-35", "cat": "switch", "check": ("switch", "/3.5",
        {"expect": "gemini35"})},
    {"id": "switch-slash-lite", "cat": "switch", "check": ("switch", "/3.5 lite",
        {"expect": "gemini25"})},
    {"id": "switch-slash-lite-bare", "cat": "switch", "check": ("switch", "/lite",
        {"expect": "gemini25"})},
    {"id": "switch-slash-25-alias", "cat": "switch", "check": ("switch", "/2.5",
        {"expect": "gemini25"}, "retired name keeps addressing its slot behind the slash")},
    {"id": "switch-slash-agnes", "cat": "switch", "check": ("switch", "/agnes",
        {"expect": "agnes"})},
    {"id": "switch-slash-auto", "cat": "switch", "check": ("switch", "/auto",
        {"expect": "auto"})},
    {"id": "switch-slash-spoken", "cat": "switch", "check": ("switch", "slash 3.5 lite",
        {"expect": "gemini25"}, "the spoken form of /")},
    {"id": "switch-slash-wake", "cat": "switch", "check": ("switch", "hey jarvis /lite",
        {"expect": "gemini25"})},
    {"id": "switch-slash-help", "cat": "switch", "check": ("switch", "/help",
        {"expect": "help"})},
    {"id": "switch-no-slash-no-switch", "cat": "switch", "check": ("switch",
        "switch to gemini 3.5 lite", {"expect": None},
        "the natural-language path is retired: no slash, no switch")},
    {"id": "switch-bare-word-no-switch", "cat": "switch", "check": ("switch",
        "3.5 lite", {"expect": None})},
    {"id": "switch-sentence-with-slash-not-prefix", "cat": "switch", "check": ("switch",
        "what is /lite", {"expect": None}, "slash must be the PREFIX")},
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
    # The reported symptom, pinned end to end: the search package is installed,
    # so a turn that needs live facts must never blame a "missing dependency".
    # It passes whether or not DuckDuckGo answers, because an honest "the search
    # endpoint did not answer" contains none of these phrases.
    {"id": "live-search-no-dependency-excuse", "cat": "live",
     "utterance": "what is the top technology headline right now? one short sentence",
     "live": {"reply_min_chars": 20, "reply_not_contains": [
         "missing dependency", "not installed", "dependency", "web search tool isn't available",
         "due to a missing", "install project dependencies"]}},
]

# Explicit-keep grammar, appended by the memory-persistence fix: these are the
# phrasings the pattern used to miss, so the fact stayed only in the episode
# log and a later asking got "I cannot determine that".
CASES += [
    {"id": "keep-do-not-lose-it", "cat": "keep", "check": ("keep",
        "my building elevator access code is 9 3 1 4, do not lose it",
        {"expect": "my building elevator access code is 9 3 1 4"},
        "the owner's real sentence, said on 10 Nov, never pinned")},
    {"id": "keep-remember-that", "cat": "keep", "check": ("keep",
        "remember that my wifi password is hunter2", {"expect": "my wifi password is hunter2"})},
    {"id": "keep-future-reference", "cat": "keep", "check": ("keep",
        "my passport number is X1234567 for future reference",
        {"expect": "my passport number is X1234567"})},
    {"id": "keep-note-this-down", "cat": "keep", "check": ("keep",
        "my sister's birthday is 3 March - note this down",
        {"expect": "my sister's birthday is 3 March"})},
    {"id": "keep-reminder-is-not-a-fact", "cat": "keep", "check": ("keep",
        "don't forget to take the bins out", {"expect": ""},
        "a reminder belongs to the reminder path, not the user model")},
    {"id": "keep-question-is-not-a-fact", "cat": "keep", "check": ("keep",
        "what is my elevator code? do not lose it", {"expect": ""})},
]

# --------------------------------------------------------------- app launching
# Pinned behaviors of spoken app names against a fake Start menu: exact and
# alias resolution, ambiguity failing closed with candidates, typo tolerance,
# and path-like inputs passing through to the launch policy. Offline and
# deterministic — the index is supplied by the case, never scanned from the OS.
_CASES_APP = [
    {"id": "app-exact-name", "cat": "app", "check": ("app", "spotify",
        {"index": [["Spotify", "C:/Menu/Spotify.lnk"]], "aliases": {}, "expect": {"ok": True, "name": "Spotify"}})},
    {"id": "app-alias-browser", "cat": "app", "check": ("app", "browser",
        {"index": [["Google Chrome", "C:/Menu/Chrome.lnk"]],
         "expect": {"ok": True, "name": "Google Chrome"}})},
    {"id": "app-alias-code", "cat": "app", "check": ("app", "code",
        {"index": [["Visual Studio Code", "C:/Menu/VS Code.lnk"]],
         "expect": {"ok": True, "name": "Visual Studio Code"}})},
    {"id": "app-alias-settles-ambiguity", "cat": "app", "check": ("app", "chrome",
        {"index": [["Google Chrome", "C:/Menu/Chrome.lnk"],
                   ["Chrome Remote Desktop", "C:/Menu/CRD.lnk"]],
         "expect": {"ok": True, "name": "Google Chrome"}},
        "the default alias settles the two-Chrome case; no guess, no card")},
    {"id": "app-ambiguous-no-alias", "cat": "app", "check": ("app", "chrome",
        {"index": [["Google Chrome", "C:/Menu/Chrome.lnk"],
                   ["Chrome Remote Desktop", "C:/Menu/CRD.lnk"]],
         "aliases": {},
         "expect": {"ok": False, "candidates": ["Chrome Remote Desktop", "Google Chrome"]}},
        "without an alias, two matches fail closed with candidates")},
    {"id": "app-typo-tolerant", "cat": "app", "check": ("app", "spotfy",
        {"index": [["Spotify", "C:/Menu/Spotify.lnk"], ["Notepad", "C:/Menu/Notepad.lnk"]],
         "aliases": {}, "expect": {"ok": True, "name": "Spotify"}},
        "a one-letter typo still finds the app")},
    {"id": "app-unknown-suggests", "cat": "app", "check": ("app", "chrome edge browser",
        {"index": [["Google Chrome", "C:/Menu/Chrome.lnk"], ["Notepad", "C:/Menu/Notepad.lnk"]],
         "aliases": {}, "expect": {"ok": False, "candidates": ["Google Chrome"]}},
        "a miss names what IS installed so one round trip corrects it")},
    {"id": "app-unknown-gibberish-no-suggestion", "cat": "app", "check": ("app", "zzqqxx",
        {"index": [["Spotify", "C:/Menu/Spotify.lnk"], ["Notepad", "C:/Menu/Notepad.lnk"]],
         "aliases": {}, "expect": {"ok": False, "candidates": []}},
        "gibberish invents no near-match suggestion")},
    {"id": "app-path-passes-through", "cat": "app", "check": ("app", "C:/Docs/report.pdf",
        {"index": [["Spotify", "C:/Menu/Spotify.lnk"]],
         "expect": {"ok": True}},
        "path-like input skips resolution; the launch policy judges it")},
    {"id": "app-exe-passes-through", "cat": "app", "check": ("app", "notepad.exe",
        {"index": [["Spotify", "C:/Menu/Spotify.lnk"]],
         "expect": {"ok": True}},
        "an .exe name is not resolved; the launch policy blocks it")},
]

CASES += _CASES_APP

