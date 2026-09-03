import asyncio

import pytest

from mirage.policy.constants import DEFAULT_ASK_REASON, DEFAULT_DENY_REASON
from mirage.policy.script import (ScriptPolicy, hook_call, script_action,
                                  script_context)
from mirage.policy.types import (Ask, CommandContext, Deny, DenyScope,
                                 ProfileScript)
from mirage.runtime.errors import EvalError
from mirage.runtime.language import LanguageRuntime
from mirage.runtime.mixin import EvaluatorMixin
from mirage.runtime.resolver import PrefixResolver
from mirage.runtime.types import (EvalResult, EvalValue, RunArgs, RunResult,
                                  ScriptSource)
from mirage.types import PathSpec

DENY_ANSWER = {"deny": "sealed"}


class FakeRegistry:
    """The one registry question a CommandContext carries."""

    def is_mount_root(self, path: str) -> bool:
        return False


class FakeEngine(EvaluatorMixin):
    """Stands in for a built engine, recording what it saw."""

    built: list["FakeEngine"] = []

    def __init__(self,
                 value: EvalValue = None,
                 error: Exception | None = None,
                 delay: float = 0.0) -> None:
        self.value = value
        self.error = error
        self.delay = delay
        self.seen: dict[str, EvalValue] = {}
        self.code = ""
        self.evals = 0
        self.closed = False
        FakeEngine.built.append(self)

    async def eval(self,
                   code: str,
                   *,
                   inputs: dict[str, EvalValue] | None = None,
                   session: str | None = None) -> EvalResult:
        self.code = code
        self.seen = dict(inputs or {})
        self.evals += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return EvalResult(value=self.value)

    async def close(self) -> None:
        self.closed = True


class FakeLanguageEngine(LanguageRuntime, EvaluatorMixin):
    """An interpreter engine, recording the doors it was attached to."""

    language = "python"
    name = "fake"

    def __init__(self) -> None:
        super().__init__()
        self.attached: tuple[object, object] | None = None

    def attach(self, dispatch, resolver) -> None:
        self.attached = (dispatch, resolver)

    async def run(self, args: RunArgs) -> RunResult:
        raise NotImplementedError

    async def eval(self,
                   code: str,
                   *,
                   inputs: dict[str, EvalValue] | None = None,
                   session: str | None = None) -> EvalResult:
        return EvalResult(value=None)

    async def close(self) -> None:
        pass


async def _door(op, path, **kwargs):
    raise AssertionError("the door is attached, never called here")


class OneScript:
    """A sessions query holding one script for every known session."""

    def __init__(self, entry: ProfileScript | None) -> None:
        self.entry = entry

    def script_of(self, session_id: str) -> ProfileScript | None:
        return self.entry


@pytest.fixture(autouse=True)
def _reset_built():
    FakeEngine.built = []


def _entry(runtime: str = "monty") -> ProfileScript:
    return ProfileScript(profile="release",
                         script=ScriptSource("SOURCE"),
                         runtime=runtime)


def _path(virtual: str) -> PathSpec:
    return PathSpec(virtual=virtual,
                    directory="/",
                    resource_path=virtual.lstrip("/"),
                    raw_path=virtual)


def _ctx(command: str = "cat", session_id: str = "s") -> CommandContext:
    return CommandContext(command=command,
                          paths=(_path("/repo/sealed/k"), ),
                          argv=("/repo/sealed/k", ),
                          cwd="/repo",
                          registry=FakeRegistry(),
                          operands=(_path("/repo/sealed/k"), ),
                          session_id=session_id,
                          agent_id="agent-1",
                          tokens=(command, "/repo/sealed/k"),
                          program=(command, ))


def _mounts() -> list[str]:
    return ["/repo/", "/scratch/"]


def _policy(value: EvalValue = None,
            error: Exception | None = None,
            delay: float = 0.0,
            entry: ProfileScript | None = None) -> ScriptPolicy:
    policy = ScriptPolicy(OneScript(entry if entry is not None else _entry()),
                          _mounts)
    policy._engines["monty"] = FakeEngine(value, error, delay)
    return policy


def test_script_context_is_the_command_context_as_data():
    ctx = script_context("release", _ctx(), _mounts())
    assert ctx == {
        "profile": "release",
        "command": {
            "name": "cat",
            "argv": ["/repo/sealed/k"],
            "tokens": ["cat", "/repo/sealed/k"],
            "program": ["cat"],
            "paths": ["/repo/sealed/k"],
            "operands": ["/repo/sealed/k"],
            "tool": True,
            "walks": False,
        },
        "session": {
            "id": "s",
            "agent": "agent-1",
            "cwd": "/repo",
        },
        "mounts": ["/repo/", "/scratch/"],
    }


@pytest.mark.parametrize("value", [None, "allow"])
def test_allow_and_silence_are_no_opinion(value):
    assert script_action(value) is None


def test_a_deny_answer_becomes_a_whole_command_deny():
    action = script_action({"deny": "sealed"})
    assert action == Deny("sealed", DenyScope.COMMAND)


def test_an_ask_answer_goes_to_the_approval_door():
    action = script_action({"ask": "sign-off"})
    assert action == Ask("sign-off")
    assert action.rule is None


def test_bare_verbs_carry_the_documents_default_reasons():
    assert script_action("deny") == Deny(DEFAULT_DENY_REASON)
    assert script_action("ask") == Ask(DEFAULT_ASK_REASON)


@pytest.mark.parametrize("value", [
    [1, 2],
    7,
    "nope",
    {},
    {
        "deny": ""
    },
    {
        "deny": 3
    },
    {
        "allow": True
    },
    {
        "deny": "a",
        "ask": "b"
    },
])
def test_anything_else_is_refused(value):
    with pytest.raises(ValueError, match="must answer allow, deny or ask"):
        script_action(value)


@pytest.mark.asyncio
async def test_a_session_without_a_script_is_not_judged():
    policy = ScriptPolicy(OneScript(None), _mounts)
    assert await policy.pre_command(_ctx()) is None
    assert FakeEngine.built == []


@pytest.mark.asyncio
async def test_the_policy_is_shown_the_commands_facts_and_its_hook_is_called():
    policy = _policy(None)
    assert await policy.pre_command(_ctx()) is None
    engine = FakeEngine.built[0]
    # The program whole, then the call to the hook it defines, so its
    # return is the value the evaluator hands back.
    assert engine.code == "SOURCE\n\npre_command(ctx)\n"
    assert engine.seen == {"ctx": script_context("release", _ctx(), _mounts())}


def test_the_hook_is_called_in_the_programs_own_language():
    assert hook_call(ScriptSource("x")) == "pre_command(ctx)"
    assert hook_call(ScriptSource("x", language="js")) == "preCommand(ctx)"


@pytest.mark.asyncio
async def test_a_deny_it_computed_refuses_the_command():
    policy = _policy(DENY_ANSWER)
    assert await policy.pre_command(_ctx()) == Deny("sealed")


@pytest.mark.asyncio
async def test_an_ask_it_computed_reaches_the_door():
    policy = _policy({"ask": "sign-off"})
    assert await policy.pre_command(_ctx()) == Ask("sign-off")


@pytest.mark.asyncio
async def test_the_engine_is_built_once_and_reused():
    policy = _policy(None)
    await policy.pre_command(_ctx("cat"))
    await policy.pre_command(_ctx("ls"))
    assert len(FakeEngine.built) == 1
    assert FakeEngine.built[0].evals == 2


@pytest.mark.asyncio
async def test_close_closes_the_engines():
    policy = _policy(None)
    await policy.pre_command(_ctx())
    await policy.close()
    assert FakeEngine.built[0].closed


@pytest.mark.asyncio
async def test_a_script_that_raised_fails_closed():
    # Silence on failure would run exactly the commands the script
    # existed to judge, so every failure arm refuses instead.
    policy = _policy(error=EvalError("boom"))
    action = await policy.pre_command(_ctx())
    assert action == Deny("profile 'release' policy failed: boom")


@pytest.mark.asyncio
async def test_a_syntax_error_is_named_as_one():
    policy = _policy(error=EvalError("bad token", syntax=True))
    action = await policy.pre_command(_ctx())
    assert isinstance(action, Deny)
    assert "profile 'release' policy syntax error" in action.reason


@pytest.mark.asyncio
async def test_a_script_that_timed_out_fails_closed(monkeypatch):
    monkeypatch.setattr("mirage.policy.script.SCRIPT_EVAL_TIMEOUT_SECONDS",
                        0.01)
    policy = _policy(None, delay=0.2)
    action = await policy.pre_command(_ctx())
    assert isinstance(action, Deny)
    assert "profile 'release' policy timed out" in action.reason


@pytest.mark.asyncio
async def test_a_wrong_answer_shape_fails_closed():
    policy = _policy([1, 2])
    action = await policy.pre_command(_ctx())
    assert isinstance(action, Deny)
    assert "profile 'release' policy must answer" in action.reason


def _no_engine(script: ScriptSource, runtime: str) -> FakeEngine:
    raise ValueError(f"script names runtime {runtime!r}: nope")


@pytest.mark.asyncio
async def test_an_engine_it_cannot_build_fails_closed(monkeypatch):
    monkeypatch.setattr("mirage.policy.script.script_engine", _no_engine)
    policy = ScriptPolicy(OneScript(_entry("ghost")), _mounts)
    action = await policy.pre_command(_ctx())
    assert isinstance(action, Deny)
    assert action.reason == "profile 'release' policy names runtime " \
                            "'ghost': nope"


@pytest.mark.asyncio
async def test_a_wired_policy_attaches_the_doors_to_the_engine(monkeypatch):
    # The facts name the path; the engine opens it. Attached before the
    # first evaluation, as Runtimes attaches an agent's engine, so the
    # script's open() reads the mounts through the same door.
    engine = FakeLanguageEngine()
    monkeypatch.setattr("mirage.policy.script.script_engine",
                        lambda script, runtime: engine)
    resolver = PrefixResolver(lambda: ["/repo/"])
    policy = ScriptPolicy(OneScript(_entry()),
                          _mounts,
                          dispatch=_door,
                          resolver=resolver)
    assert await policy.pre_command(_ctx()) is None
    assert engine.attached == (_door, resolver)


@pytest.mark.asyncio
async def test_a_bare_policy_attaches_nothing(monkeypatch):
    # Outside a workspace there is no door to hand over, and the
    # engine is left as built: its scripts see no file.
    engine = FakeLanguageEngine()
    monkeypatch.setattr("mirage.policy.script.script_engine",
                        lambda script, runtime: engine)
    policy = ScriptPolicy(OneScript(_entry()), _mounts)
    assert await policy.pre_command(_ctx()) is None
    assert engine.attached is None


def test_dispatch_and_resolver_travel_together():
    with pytest.raises(ValueError, match="travel together"):
        ScriptPolicy(OneScript(None), _mounts, dispatch=_door)
    with pytest.raises(ValueError, match="travel together"):
        ScriptPolicy(OneScript(None),
                     _mounts,
                     resolver=PrefixResolver(lambda: []))
