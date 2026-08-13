import gzip

from tsbot import logscan

TRACEBACK = """\
epoch 1 loss 0.4
epoch 2 loss 0.3
Traceback (most recent call last):
  File "train.py", line 61, in main
    loss.backward()
RuntimeError: CUDA out of memory.
"""


def test_read_tail_returns_whole_small_file(tmp_path):
    path = tmp_path / "out"
    path.write_text("hello\nworld\n")

    assert logscan.read_tail(path, 65536) == "hello\nworld\n"


def test_read_tail_drops_the_partial_first_line(tmp_path):
    path = tmp_path / "out"
    path.write_text("aaaaaaaaaa\nbbbbbbbbbb\ncccccccccc\n")

    # A byte budget that lands mid-way through the second line.
    tail = logscan.read_tail(path, 18)

    assert tail == "cccccccccc\n"


def test_read_tail_handles_gzipped_output(tmp_path):
    path = tmp_path / "out.gz"
    with gzip.open(path, "wb") as handle:
        handle.write(b"compressed\nlines\n")

    assert logscan.read_tail(path, 65536) == "compressed\nlines\n"


def test_read_tail_bounds_gzipped_output(tmp_path):
    path = tmp_path / "out.gz"
    with gzip.open(path, "wb") as handle:
        handle.write(b"".join(b"line %04d\n" % i for i in range(10000)))

    tail = logscan.read_tail(path, 100)

    assert len(tail) <= 100
    assert tail.endswith("line 9999\n")


def test_read_tail_returns_none_for_missing_file(tmp_path):
    assert logscan.read_tail(tmp_path / "nope", 1024) is None


def test_error_source_prefers_separate_stderr(tmp_path):
    stdout = tmp_path / "out"
    stdout.write_text("progress\n")
    stderr = tmp_path / "out.e"
    stderr.write_text("boom\n")

    assert logscan.error_source(stdout) == stderr


def test_error_source_ignores_empty_stderr(tmp_path):
    stdout = tmp_path / "out"
    stdout.write_text("progress\n")
    (tmp_path / "out.e").write_text("")

    assert logscan.error_source(stdout) == stdout


def test_error_source_returns_none_when_output_discarded(tmp_path):
    # Job queued with `ts -n`: no output file was ever created.
    assert logscan.error_source(tmp_path / "gone") is None


def test_extract_error_finds_the_traceback():
    result = logscan.extract_error(TRACEBACK, 30)

    assert result is not None
    excerpt, detected = result
    assert detected == "RuntimeError"
    assert excerpt.startswith("Traceback (most recent call last):")
    assert "epoch 1" not in excerpt


def test_extract_error_uses_the_last_traceback():
    text = TRACEBACK + "retrying\n" + TRACEBACK.replace("RuntimeError", "ValueError")

    result = logscan.extract_error(text, 30)

    assert result is not None
    excerpt, detected = result
    assert detected == "ValueError"
    assert excerpt.count("Traceback (most recent call last):") == 1


def test_extract_error_falls_back_to_generic_markers():
    result = logscan.extract_error("building\nERROR: linker failed\n", 30)

    assert result is not None
    excerpt, detected = result
    assert detected.upper() == "ERROR"
    assert "linker failed" in excerpt


def test_extract_error_returns_none_when_nothing_matches():
    assert logscan.extract_error("all fine\ndone\n", 30) is None


def test_extract_error_clamps_to_max_lines():
    frames = "".join(f'  File "t.py", line {i}\n' for i in range(200))
    text = "Traceback (most recent call last):\n" + frames + "RuntimeError: nope\n"

    result = logscan.extract_error(text, 10)

    assert result is not None
    excerpt, _ = result
    assert len(excerpt.split("\n")) <= 10
    # The clamp keeps the end, where the actual exception lives.
    assert "RuntimeError: nope" in excerpt


def test_last_lines():
    assert logscan.last_lines("a\nb\nc\nd\n", 2) == "c\nd"
