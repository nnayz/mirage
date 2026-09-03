from mirage.agents.io_text import decode, io_to_str, with_refusal_bytes
from mirage.io.types import IOResult
from mirage.types import Refusal


def test_decode_none_returns_empty():
    assert decode(None) == ""


def test_decode_bytes():
    assert decode(b"hello") == "hello"


def test_decode_invalid_utf8_replaces():
    assert decode(b"\xff") == "�"


def test_io_to_str_stdout_only():
    assert io_to_str(IOResult(stdout=b"out")) == "out"


def test_io_to_str_stderr_only():
    assert io_to_str(IOResult(stderr=b"err")) == "err"


def test_io_to_str_combines_stdout_and_stderr():
    assert io_to_str(IOResult(stdout=b"out", stderr=b"err")) == "out\nerr"


async def _stream():
    yield b"streamed"


def test_io_to_str_ignores_unmaterialized_stream():
    assert io_to_str(IOResult(stdout=_stream())) == ""


def test_io_to_str_appends_the_refusal_after_stderr():
    io = IOResult(stderr=b"rm: Permission denied\n",
                  exit_code=126,
                  refusal=Refusal(kind="deny",
                                  reason="no deletes",
                                  policy="RulePolicy"))
    assert io_to_str(
        io) == "rm: Permission denied\npolicy denied: no deletes\n"


def test_io_to_str_starts_a_line_for_the_refusal_when_needed():
    pending = Refusal(kind="pending", reason="sign-off", ask_id="a1")
    assert io_to_str(IOResult(
        stdout=b"partial",
        refusal=pending)) == "partial\nrequires approval: sign-off (ask a1)\n"
    assert io_to_str(
        IOResult(refusal=pending)) == "requires approval: sign-off (ask a1)\n"


def test_io_to_str_leaves_an_operand_refusal_alone():
    # An operand-scoped refusal already carries its reason on the
    # stderr line, GNU-style; repeating it would say nothing new.
    io = IOResult(stderr=b"rm: cannot remove 'x': keys\n",
                  exit_code=1,
                  refusal=Refusal(kind="deny",
                                  reason="cannot remove 'x': keys",
                                  policy="RulePolicy",
                                  scope="operand"))
    assert io_to_str(io) == "rm: cannot remove 'x': keys\n"


def test_with_refusal_bytes_appends_after_stderr():
    denied = Refusal(kind="deny", reason="no deletes", policy="RulePolicy")
    assert with_refusal_bytes(
        b"rm: Permission denied\n",
        denied) == b"rm: Permission denied\npolicy denied: no deletes\n"


def test_with_refusal_bytes_starts_a_line_when_needed():
    pending = Refusal(kind="pending", reason="sign-off", ask_id="a1")
    assert with_refusal_bytes(
        b"partial",
        pending) == b"partial\nrequires approval: sign-off (ask a1)\n"
    assert with_refusal_bytes(
        b"", pending) == b"requires approval: sign-off (ask a1)\n"


def test_with_refusal_bytes_leaves_the_bytes_alone_otherwise():
    # No record, or an operand-scoped one whose line already names the
    # reason: the bytes pass through untouched, invalid UTF-8 included.
    assert with_refusal_bytes(b"\xff raw", None) == b"\xff raw"
    operand = Refusal(kind="deny",
                      reason="cannot remove 'x': keys",
                      policy="RulePolicy",
                      scope="operand")
    assert with_refusal_bytes(b"rm: cannot remove 'x': keys\n",
                              operand) == b"rm: cannot remove 'x': keys\n"
