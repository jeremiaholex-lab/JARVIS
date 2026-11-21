from jarvis.agent.routing import (
    needs_retry,
    promises_unkept_action,
    route_turn,
    tool_nudge_needed,
)


def test_routine_question_stays_local():
    r = route_turn("what is my name", cloud_available=True)
    assert r.engine == "local"
    assert "ladder" in r.reason


def test_explicit_phrase_escalates():
    assert route_turn("think hard about this", cloud_available=True).engine == "cloud"
    assert route_turn("use the cloud please", cloud_available=True).engine == "cloud"


def test_analytical_work_stays_on_the_ladder():
    """The complexity heuristic existed to dodge a weak local model. The
    chain now LEADS with a flagship, so analytical work belongs there."""
    r = route_turn("compare the trade-offs of these two architectures", cloud_available=True)
    assert r.engine == "local"


def test_long_form_stays_on_the_ladder():
    text = " ".join(f"word{i}" for i in range(70))
    assert route_turn(text, cloud_available=True).engine == "local"


def test_no_cloud_key_keeps_everything_local():
    r = route_turn("think hard and compare these designs", cloud_available=False)
    assert r.engine == "local"
    assert "ladder only" in r.reason


def test_force_cloud_respected_when_key_exists():
    assert route_turn("hi", force_cloud=True, cloud_available=True).engine == "cloud"
    assert route_turn("hi", force_cloud=True, cloud_available=False).engine == "local"


def test_confidence_check_flags_weak_answers():
    assert needs_retry("I don't know.", words_in_question=8)
    assert needs_retry("", words_in_question=8)
    assert not needs_retry("Sure.", words_in_question=2)
    assert not needs_retry(
        "Your JARVIS project has one open deadline due Friday.", words_in_question=8
    )


def test_correct_short_answers_are_not_escalated():
    """Regression: 'Four.' was being sent to the cloud as 'too weak'."""
    assert not needs_retry("Four.", words_in_question=5)
    assert not needs_retry("Paris.", words_in_question=6)
    assert not needs_retry("12 words, 3 lines.", words_in_question=7)


def test_confidence_check_handles_typographic_apostrophes():
    # Models emit U+2019; an ASCII-only pattern silently failed this in a live run.
    assert needs_retry("I couldn\u2019t find any information about that.", words_in_question=9)
    assert needs_retry("I\u2019m not sure what you mean.", words_in_question=7)


def test_honest_tool_backed_negatives_are_not_retried():
    """'Nothing in your index matches' is a real answer, not a weak one."""
    answer = "I couldn't find anything about that in your indexed documents."
    assert not needs_retry(answer, words_in_question=9, used_tools=True)
    assert needs_retry(answer, words_in_question=9, used_tools=False)


def test_model_speed_is_no_longer_a_routing_factor():
    """The slow-local override steered around an underpowered Ollama model;
    the hosted ladder removed it. The parameter is accepted and ignored."""
    for tps in (1.2, 18.0):
        r = route_turn("how do raindrops actually form", cloud_available=True,
                       local_tokens_per_second=tps)
        assert r.engine == "local", tps


def test_greetings_stay_local_even_on_a_slow_machine():
    r = route_turn("hi there", cloud_available=True, local_tokens_per_second=1.0)
    assert r.engine == "local"


def test_questions_about_the_users_own_data_stay_on_the_chain():
    """These escalated when the local brain could not use tools. Every tier
    of the chain gets the same tool schemas and semantic recall, so the
    dodge-without-looking failure the heuristic guarded against is gone."""
    for q in (
        "according to my project documents, what sandbox did we choose?",
        "what deadlines do I have on my projects this week",
        "is that in my notes anywhere",
        "look at my screen and tell me what error is showing",
        "what can you see right now",
    ):
        r = route_turn(q, cloud_available=True)
        assert r.engine == "local", q


def test_promises_to_act_are_detected():
    """Live finding: "I will look at your screen…" was the whole reply."""
    for promised in (
        "I will look at your screen and inform you.",
        "I\u2019ll check your documents now.",
        "Let me search your notes.",
        "Sure, I can read that file.",
    ):
        assert promises_unkept_action(promised), promised


def test_ordinary_answers_are_not_mistaken_for_promises():
    """The detector must not fire on reassurance that carries real content."""
    for fine in (
        "I can help with that later, the report is due Friday.",
        "The capital of France is Paris.",
        "I found 2 open deadlines.",
        "Ready when you are.",
        "I can do that whenever you like.",
        "I looked at your documents: nothing matches.",
    ):
        assert not promises_unkept_action(fine), fine


def test_nudge_covers_refusals_and_promises_but_not_completed_work():
    assert tool_nudge_needed("I couldn't find that in your documents.", used_tools=False)
    assert tool_nudge_needed("I will look at your screen.", used_tools=False)
    assert not tool_nudge_needed("I will look at your screen.", used_tools=True)
    assert not tool_nudge_needed("Your filing is overdue by 30h.", used_tools=True)
    assert not tool_nudge_needed("", used_tools=False)


def test_data_questions_stay_local_when_no_cloud_is_configured():
    r = route_turn("according to my documents what is the cap", cloud_available=False)
    assert r.engine == "local"
