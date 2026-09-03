# ========= Copyright 2026 @ Strukto.AI All Rights Reserved. =========
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ========= Copyright 2026 @ Strukto.AI All Rights Reserved. =========

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence

from mirage.policy.base import Policy
from mirage.policy.constants import (DEFAULT_ASK_REASON, DEFAULT_DENY_REASON,
                                     SCRIPT_EVAL_TIMEOUT_SECONDS)
from mirage.policy.types import (Action, Ask, CommandContext, Deny,
                                 ProfileScript, SessionScriptsQuery)
from mirage.runtime.base import Runtime
from mirage.runtime.errors import EvalError
from mirage.runtime.language import LanguageRuntime
from mirage.runtime.mixin import EvaluatorMixin
from mirage.runtime.resolver import MountResolver
from mirage.runtime.script import eval_with_ctx, script_engine
from mirage.runtime.types import DispatchFn, EvalValue

logger = logging.getLogger(__name__)


def script_context(profile: str, ctx: CommandContext,
                   mounts: Sequence[str]) -> dict[str, EvalValue]:
    """What a profile's script is told about one command: the
    ``CommandContext`` the coded hooks read, as plain data.

    The same facts on both hosts, JSON-shaped because the script runs
    inside a sandboxed engine that a live object cannot cross into.
    Paths are spelled as resolved virtual paths, so a script matches
    what the command will actually touch, not what was typed; the raw
    words are in ``argv`` for a script that wants them.

    Args:
        profile (str): the profile the script speaks for.
        ctx (CommandContext): the classified command, as the gate built
            it.
        mounts (Sequence[str]): the workspace's mount prefixes.
    """
    return {
        "profile": profile,
        "command": {
            "name": ctx.command,
            "argv": list(ctx.argv),
            "tokens": list(ctx.tokens),
            "program": list(ctx.program),
            "paths": [path.virtual for path in ctx.paths],
            "operands": [path.virtual for path in ctx.operands],
            "tool": ctx.tool,
            "walks": ctx.walks,
        },
        "session": {
            "id": ctx.session_id,
            "agent": ctx.agent_id,
            "cwd": ctx.cwd,
        },
        "mounts": list(mounts),
    }


def script_action(value: EvalValue) -> Deny | Ask | None:
    """The policy answer a script's last expression states.

    The vocabulary is the ``pre_command`` hook's own, spelled as data:
    ``None`` or ``'allow'`` is no opinion (the command runs unless
    another rule refuses it, and can never override one that does),
    ``'deny'`` / ``{'deny': reason}`` refuses, ``'ask'`` /
    ``{'ask': reason}`` takes the line to the approval door. The bare
    strings carry the document's default reasons, the same ones a rule
    stating no reason gets.

    Args:
        value (EvalValue): what the script evaluated to.

    Raises:
        ValueError: the value is none of those shapes. The message is a
            clause about "script", for the caller to prefix with whose
            script it is.
    """
    if value is None or value == "allow":
        return None
    if value == "deny":
        return Deny(DEFAULT_DENY_REASON)
    if value == "ask":
        return Ask(DEFAULT_ASK_REASON)
    if isinstance(value, Mapping) and len(value) == 1:
        verb, reason = next(iter(value.items()))
        if verb in ("deny", "ask") and isinstance(reason, str) and reason:
            return Deny(reason) if verb == "deny" else Ask(reason)
    raise ValueError(
        f"script must answer allow, deny or ask: None or 'allow', 'deny', "
        f"'ask', {{'deny': reason}} or {{'ask': reason}}; got {value!r}")


class ScriptPolicy(Policy):
    """Each profile's script, enforced at the admission gate.

    The scripted twin of ``PermissionsPolicy``, registered right after
    it: where that policy evaluates the document's declarative rules,
    this one evaluates the profile's program, per command, with the
    same facts (``script_context``). It reads the session's script
    through the narrow ``SessionScriptsQuery`` by the session id the
    door put in the context, so a session whose profile states no
    script costs one lookup and nothing else.

    Every failure fails closed: a script that raised, timed out,
    answered with the wrong shape, or names an engine that cannot be
    built refuses the command with a reason naming the profile, and is
    logged. Silence on failure would run exactly the commands the
    script existed to judge.

    The facts name the paths; the engine can open them. It is wired to
    the workspace's files the way an agent's runtime is (``dispatch``
    and ``resolver``, attached before its first evaluation exactly as
    ``Runtimes`` attaches an agent's engine), so a script may read what
    an operand holds and answer for its content, not only its name. A
    read from a script clears the op door like any other.

    Engines are built lazily on the first command that needs one,
    shared per engine name, and closed by the workspace's own close.
    Evaluations are serialized: the engines are worker processes, and
    two concurrent evals on one would interleave.

    Args:
        sessions (SessionScriptsQuery): the session manager, answering
            ``script_of(session_id)``.
        mounts (Callable[[], Sequence[str]]): the workspace's mount
            prefixes, read per evaluation so a mount added after
            construction is visible to the script.
        dispatch (DispatchFn | None): the workspace's op dispatch, the
            door a script's ``open()`` reads the mounts through; None
            for a policy outside a workspace, whose scripts see no
            file.
        resolver (MountResolver | None): the live mount routing table
            the dispatch is attached with. Travels with ``dispatch``:
            one without the other is refused.

    Raises:
        ValueError: ``dispatch`` and ``resolver`` were not given
            together.
    """

    def __init__(self,
                 sessions: SessionScriptsQuery,
                 mounts: Callable[[], Sequence[str]],
                 dispatch: DispatchFn | None = None,
                 resolver: MountResolver | None = None) -> None:
        if (dispatch is None) != (resolver is None):
            raise ValueError(
                "a script policy's dispatch and resolver travel together")
        self._sessions = sessions
        self._mounts = mounts
        self._dispatch = dispatch
        self._resolver = resolver
        self._engines: dict[str, Runtime] = {}
        self._lock = asyncio.Lock()

    async def pre_command(self, ctx: CommandContext) -> Action | None:
        entry = self._sessions.script_of(ctx.session_id)
        if entry is None:
            return None
        try:
            value = await self._evaluate(entry, ctx)
        except asyncio.TimeoutError:
            return self._failed(
                entry, f"timed out after {SCRIPT_EVAL_TIMEOUT_SECONDS:g}s")
        except EvalError as exc:
            arm = "syntax error" if exc.syntax else "failed"
            return self._failed(entry, f"{arm}: {exc}")
        except ValueError as exc:
            # script_engine's refusal: the engine cannot be built.
            return self._failed(entry, str(exc), prefix="")
        try:
            return script_action(value)
        except ValueError as exc:
            return self._failed(entry, str(exc), prefix="")

    async def close(self) -> None:
        """Close every engine a script was evaluated on."""
        engines = list(self._engines.values())
        self._engines.clear()
        for engine in engines:
            await engine.close()

    async def _evaluate(self, entry: ProfileScript,
                        ctx: CommandContext) -> EvalValue:
        """One command through one profile's script, serialized.

        Args:
            entry (ProfileScript): the session's script.
            ctx (CommandContext): the classified command.
        """
        async with self._lock:
            engine = self._engines.get(entry.runtime)
            if engine is None:
                engine = script_engine(entry.script, entry.runtime)
                # Attached before the first eval, as Runtimes attaches
                # an agent's engine: the script's open() then reads the
                # mounts through the same door, and an unattached
                # engine sees no file.
                if (self._dispatch is not None and self._resolver is not None
                        and isinstance(engine, LanguageRuntime)):
                    engine.attach(self._dispatch, self._resolver)
                self._engines[entry.runtime] = engine
            # script_engine refuses anything that cannot evaluate, so
            # this narrows a fact already established.
            assert isinstance(engine, EvaluatorMixin)
            return await eval_with_ctx(
                entry.script.source,
                script_context(entry.profile, ctx, self._mounts()), engine,
                SCRIPT_EVAL_TIMEOUT_SECONDS)

    def _failed(self,
                entry: ProfileScript,
                detail: str,
                prefix: str = "script ") -> Deny:
        """The fail-closed refusal: one wording, logged.

        Args:
            entry (ProfileScript): the script that failed.
            detail (str): what went wrong, already a clause about
                "script" when ``prefix`` is empty.
            prefix (str): the words before ``detail``.
        """
        reason = f"profile {entry.profile!r} {prefix}{detail}"
        logger.error("%s", reason)
        return Deny(reason)
