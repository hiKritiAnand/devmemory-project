from devmemory import compiler


def test_estimate_tokens_empty_string_is_zero():
    assert compiler.estimate_tokens("") == 0


def test_estimate_tokens_nonempty_never_zero():
    assert compiler.estimate_tokens("a") >= 1


def test_estimate_tokens_roughly_proportional_to_length():
    short = compiler.estimate_tokens("x" * 40)
    long = compiler.estimate_tokens("x" * 400)
    assert long > short


def test_compile_context_under_budget_includes_everything_no_marker():
    items = ["short one", "short two", "short three"]
    result = compiler.compile_context(items, budget_tokens=1000, render_item=lambda s: s)

    assert result.included_items == 3
    assert result.omitted_items == 0
    assert "omitted" not in result.text
    assert result.is_estimate is True


def test_compile_context_over_budget_triggers_marker_and_omitted_count():
    items = [f"item-{i}-" + "x" * 100 for i in range(50)]
    result = compiler.compile_context(items, budget_tokens=50, render_item=lambda s: s)

    assert result.omitted_items > 0
    assert "omitted" in result.text
    assert result.included_items + result.omitted_items == 50


def test_compile_context_always_includes_first_item_even_if_oversized():
    items = ["x" * 10000]
    result = compiler.compile_context(items, budget_tokens=1, render_item=lambda s: s)

    assert result.included_items == 1
    assert result.omitted_items == 0


def test_compile_context_dedup_by_default_key_is_rendered_text():
    items = ["same text", "same text", "different text"]
    result = compiler.compile_context(items, budget_tokens=1000, render_item=lambda s: s)

    assert result.included_items == 2


def test_compile_context_dedup_by_custom_key():
    items = [{"summary": "a", "why": "x"}, {"summary": "a", "why": "x"}, {"summary": "b", "why": "y"}]
    result = compiler.compile_context(
        items, budget_tokens=1000,
        render_item=lambda i: f"{i['summary']}: {i['why']}",
        dedup_key=lambda i: (i["summary"], i["why"]),
    )
    assert result.included_items == 2


def test_compile_context_empty_items_list():
    result = compiler.compile_context([], budget_tokens=1000, render_item=lambda s: s)
    assert result.included_items == 0
    assert result.omitted_items == 0
    assert result.text == ""
