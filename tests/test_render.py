from tsbot import render
from tsbot.config import DEFAULT_FAILURE_TEMPLATE, DEFAULT_SUCCESS_TEMPLATE


def fields(**overrides):
    base = dict(
        jobid="42",
        command="python train.py",
        exit_code=1,
        status="failure",
        host="gpu-01",
        output_file="/tmp/ts-out-42",
    )
    base.update(overrides)
    return render.Fields(**base)


def test_format_exit_code_names_signal_deaths():
    assert render.format_exit_code(-1) == "killed or crashed (signal)"
    assert render.format_exit_code(3) == "exit 3"


def test_format_duration():
    assert render.format_duration(None) is None
    assert render.format_duration(12.34) == "12.3s"
    assert render.format_duration(192) == "3m12s"
    assert render.format_duration(3840) == "1h04m"


def test_interpolated_values_are_html_escaped():
    message, _ = render.render(
        DEFAULT_FAILURE_TEMPLATE,
        fields(command="grep '<a> & <b>' file"),
        log="if (a < b && c > d) fail;",
    )

    assert "&lt;a&gt; &amp; &lt;b&gt;" in message
    assert "a &lt; b &amp;&amp; c &gt; d" in message
    # The template's own markup must survive untouched.
    assert "<pre>" in message and "&lt;pre&gt;" not in message


def test_quotes_are_left_alone_for_readability():
    message, _ = render.render(DEFAULT_FAILURE_TEMPLATE, fields(), log='say "hi"')

    assert '"hi"' in message
    assert "&quot;" not in message


def test_label_and_duration_are_omitted_when_unset():
    message, _ = render.render(DEFAULT_SUCCESS_TEMPLATE, fields(exit_code=0))

    assert "Job <b>42</b> finished cleanly\n" in message
    assert "[" not in message


def test_label_and_duration_are_decorated_when_set():
    message, _ = render.render(
        DEFAULT_SUCCESS_TEMPLATE,
        fields(exit_code=0, label="train-v3", duration="3m12s"),
    )

    assert "[train-v3] Job <b>42</b> finished cleanly · 3m12s" in message


def test_detected_name_is_shown_when_present():
    message, _ = render.render(
        DEFAULT_FAILURE_TEMPLATE, fields(detected="RuntimeError"), log="boom"
    )

    assert "<i>detected: RuntimeError</i>" in message


def test_long_log_is_trimmed_from_the_front_to_fit():
    log = "\n".join(f"line {i:05d}" for i in range(2000))

    message, truncated = render.render(DEFAULT_FAILURE_TEMPLATE, fields(), log=log)

    assert truncated is True
    assert len(message) <= render.TELEGRAM_MAX_CHARS
    assert render.TRUNCATION_MARKER.strip() in message
    # The tail survives; the head is what gets dropped.
    assert "line 01999" in message
    assert "line 00000" not in message


def test_a_single_overlong_line_is_cut_mid_line():
    log = "x" * 20000

    message, truncated = render.render(DEFAULT_FAILURE_TEMPLATE, fields(), log=log)

    assert truncated is True
    assert len(message) <= render.TELEGRAM_MAX_CHARS


def test_escaping_never_pushes_the_message_over_the_limit():
    # Every character expands to "&amp;" (5x) once escaped.
    log = "&" * 5000

    message, truncated = render.render(DEFAULT_FAILURE_TEMPLATE, fields(), log=log)

    assert truncated is True
    assert len(message) <= render.TELEGRAM_MAX_CHARS
    # No entity was sliced in half.
    assert "&am" not in message.replace("&amp;", "")


def test_short_log_is_not_marked_truncated():
    message, truncated = render.render(
        DEFAULT_FAILURE_TEMPLATE, fields(), log="RuntimeError: nope"
    )

    assert truncated is False
    assert "RuntimeError: nope" in message


def test_unknown_placeholder_stays_visible_instead_of_raising():
    message, _ = render.render("job {jobid} {nonsense}", fields())

    assert message == "job 42 {nonsense}"
