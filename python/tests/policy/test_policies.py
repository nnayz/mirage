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

import errno

import pytest

from mirage.policy import (Action, Ask, CommandContext, CommandRule, Deny,
                           DenyScope, ExecuteResultContext, MountRootPolicy,
                           OpsContext, OpsResultContext, Pending, Policies,
                           Policy, PolicyError, describe_refusal,
                           post_execute_gate, post_ops_gate, pre_ops_gate,
                           refusal_of, render_deny, render_pending)
from mirage.policy.rule import RulePolicy
from mirage.resource.ram import RAMResource
from mirage.types import Limit, MountMode, PathSpec, Producer, Refusal
from mirage.workspace.mount import MountRegistry


class DenyWeird(Policy):

    async def pre_command(self, ctx: CommandContext) -> Action | None:
        if ctx.command == "weird":
            return Deny("nope")
        return None


class Raising(Policy):

    async def pre_command(self, ctx: CommandContext) -> Action | None:
        raise RuntimeError("boom")


class IllegalReturn(Policy):

    async def pre_command(self, ctx: CommandContext) -> Action | None:
        return "not an action"  # type: ignore[return-value]


class Silent(Policy):
    pass


class AskRm(Policy):

    async def pre_command(self, ctx: CommandContext) -> Action | None:
        if ctx.command == "rm":
            return Ask("sign-off")
        return None


class AskAll(Policy):

    async def pre_command(self, ctx: CommandContext) -> Action | None:
        return Ask("second opinion")


class DenyRm(Policy):

    async def pre_command(self, ctx: CommandContext) -> Action | None:
        if ctx.command == "rm":
            return Deny("no")
        return None


class AskOnOps(Policy):

    async def pre_ops(self, ctx: OpsContext) -> Action | None:
        return Ask("cannot wait here")


def _registry() -> MountRegistry:
    registry = MountRegistry()
    registry.mount("/data", RAMResource(), MountMode.WRITE)
    return registry


def _path(virtual: str) -> PathSpec:
    return PathSpec(virtual=virtual,
                    directory=virtual,
                    resource_path="",
                    raw_path=virtual,
                    resolved=True)


def _ctx(command: str,
         paths: list[PathSpec] | None = None,
         registry: MountRegistry | None = None) -> CommandContext:
    return CommandContext(command=command,
                          paths=tuple(paths or []),
                          argv=(),
                          cwd="/",
                          registry=registry or _registry())


@pytest.mark.asyncio
async def test_policies_carry_no_rules_by_default():
    assert await Policies().pre_command(_ctx("rm", [_path("/data")])) is None


@pytest.mark.asyncio
async def test_registry_seeds_the_mount_root_policy():
    registry = _registry()
    deny = await registry.policies.pre_command(
        _ctx("rm", [_path("/data")], registry))
    assert deny is not None
    assert "Device or resource busy" in deny.reason


@pytest.mark.asyncio
async def test_builtin_runs_first_then_user_policies_in_order():
    policies = Policies([MountRootPolicy()])
    policies.add(RulePolicy(CommandRule(reason="user rule",
                                        commands=("rm", ))))
    # Both match `rm /data`; the built-in GNU message wins by order.
    deny = await policies.pre_command(_ctx("rm", [_path("/data")]))
    assert deny is not None
    assert "Device or resource busy" in deny.reason
    # Only the user rule matches `rm /data/x`.
    deny = await policies.pre_command(_ctx("rm", [_path("/data/x")]))
    assert deny is not None
    assert deny == Deny("user rule", policy="RulePolicy")
    # The command plane renders a whole-command refusal at 126 and an
    # operand one in the GNU voice at 1, whoever produced it.
    assert render_deny("rm", deny) == (b"rm: Permission denied\n", 126)
    assert render_deny("rm",
                       Deny("cannot remove 'x'",
                            DenyScope.OPERAND)) == (b"rm: cannot remove 'x'\n",
                                                    1)
    assert render_deny("tar",
                       Deny("x: Cannot open",
                            DenyScope.OPERAND)) == (b"tar: x: Cannot open\n",
                                                    2)


@pytest.mark.asyncio
async def test_policy_instances_and_unoverridden_hooks():
    policies = Policies()
    policies.add(Silent())
    policies.add(DenyWeird())
    deny = await policies.pre_command(_ctx("weird"))
    assert deny == Deny("nope", policy="DenyWeird")
    assert await policies.pre_command(_ctx("normal")) is None


@pytest.mark.asyncio
async def test_a_raising_policy_fails_closed():
    policies = Policies()
    policies.add(Raising())
    deny = await policies.pre_command(_ctx("ls"))
    assert deny is not None
    assert deny.scope is DenyScope.COMMAND
    assert deny.reason == "Raising failed"
    assert deny.policy == "Raising"
    assert deny.failed is True


@pytest.mark.asyncio
async def test_an_illegal_return_raises_policy_error():
    policies = Policies()
    policies.add(IllegalReturn())
    with pytest.raises(PolicyError, match="IllegalReturn"):
        await policies.pre_command(_ctx("ls"))


class DenyReadOps(Policy):

    async def pre_ops(self, ctx: OpsContext) -> Action | None:
        if ctx.op == "read":
            return Deny("no reads")
        return None


class DenyBigResults(Policy):

    async def post_ops(self, ctx: OpsResultContext) -> Action | None:
        if isinstance(ctx.result, bytes) and len(ctx.result) > 8:
            return Deny("result too large")
        return None


@pytest.mark.asyncio
async def test_pre_ops_first_deny_wins_and_wants_gates():
    policies = Policies()
    assert not policies.wants("pre_ops")
    policies.add(DenyReadOps())
    assert policies.wants("pre_ops")
    assert not policies.wants("post_ops")
    ctx = OpsContext(op="read",
                     path=_path("/data/x"),
                     write=False,
                     prefix="/data/")
    deny = await policies.pre_ops(ctx)
    assert deny == Deny("no reads", policy="DenyReadOps")
    write_ctx = OpsContext(op="write",
                           path=_path("/data/x"),
                           write=True,
                           prefix="/data/")
    assert await policies.pre_ops(write_ctx) is None


@pytest.mark.asyncio
async def test_pre_ops_gate_raises_eacces():
    policies = Policies()
    policies.add(DenyReadOps())
    with pytest.raises(PermissionError) as excinfo:
        await pre_ops_gate(policies, "read", _path("/data/x"), False, "/data/")
    assert excinfo.value.errno == errno.EACCES
    assert excinfo.value.filename == "/data/x"
    assert "no reads" in str(excinfo.value)
    # No opinion on writes: the gate passes silently.
    await pre_ops_gate(policies, "write", _path("/data/x"), True, "/data/")


@pytest.mark.asyncio
async def test_post_ops_gate_suppresses_the_result():
    policies = Policies()
    policies.add(DenyBigResults())
    await post_ops_gate(policies, "read", _path("/data/x"), False, "/data/",
                        b"tiny")
    with pytest.raises(PermissionError) as excinfo:
        await post_ops_gate(policies, "read", _path("/data/x"), False,
                            "/data/", b"a long secret payload")
    # A post deny suppresses the result of an op that already ran; the
    # door's OpReport, stamped before this gate fires, is what keeps
    # the accounting of the completed op.
    assert excinfo.value.errno == errno.EACCES


class CapFour(Policy):

    async def post_ops(self, ctx: OpsResultContext) -> Action | None:
        return Limit(max_bytes=4)


class CapTwo(Policy):

    async def post_ops(self, ctx: OpsResultContext) -> Action | None:
        return Limit(max_bytes=2)


class LimitOnPre(Policy):

    async def pre_command(self, ctx: CommandContext) -> Action | None:
        return Limit(max_bytes=1)


class CapLines(Policy):

    async def post_execute(self, ctx: ExecuteResultContext) -> Action | None:
        return Limit(max_lines=2)


def _ops_result_ctx() -> OpsResultContext:
    return OpsResultContext(op="read",
                            path=_path("/data/x"),
                            write=False,
                            prefix="/data/",
                            result=b"payload")


@pytest.mark.asyncio
async def test_post_ops_limits_merge_to_the_tightest():
    policies = Policies()
    policies.add(CapFour())
    policies.add(CapTwo())
    deny, bound = await policies.post_ops(_ops_result_ctx())
    assert deny is None
    assert bound is not None
    assert bound.max_bytes == 2


@pytest.mark.asyncio
async def test_post_ops_gate_returns_the_merged_bound():
    policies = Policies()
    policies.add(CapFour())
    bound = await post_ops_gate(policies, "read", _path("/data/x"), False,
                                "/data/", b"payload")
    assert bound is not None
    assert bound.max_bytes == 4


@pytest.mark.asyncio
async def test_a_limit_is_illegal_on_pre_command():
    policies = Policies()
    policies.add(LimitOnPre())
    with pytest.raises(PolicyError, match="LimitOnPre"):
        await policies.pre_command(_ctx("ls"))


@pytest.mark.asyncio
async def test_post_execute_gate_merges_user_limits():
    policies = Policies()
    policies.add(CapLines())
    deny, bound = await post_execute_gate(
        policies,
        ExecuteResultContext(producer=Producer(command="echo"), exit_code=0))
    assert deny is None
    assert bound is not None
    assert bound.max_lines == 2


@pytest.mark.asyncio
async def test_a_deny_anywhere_in_the_chain_outranks_an_ask():
    # The loop keeps looking past an Ask for a Deny, whichever order the
    # two policies were registered in, so an approval can never re-open
    # a refusal.
    for order in ([AskRm(), DenyRm()], [DenyRm(), AskRm()]):
        policies = Policies(order)
        assert await policies.pre_command(_ctx("rm")) == Deny("no",
                                                              policy="DenyRm")
    # With nothing refusing, the first Ask is the answer.
    policies = Policies([AskRm(), AskAll()])
    assert await policies.pre_command(_ctx("rm")) == Ask("sign-off")
    assert await policies.pre_command(_ctx("ls")) == Ask("second opinion")


@pytest.mark.asyncio
async def test_an_ask_is_illegal_off_the_command_plane():
    policies = Policies([AskOnOps()])
    with pytest.raises(PolicyError, match="AskOnOps"):
        await policies.pre_ops(
            OpsContext(op="write",
                       path=_path("/data/x"),
                       write=True,
                       prefix="/data/"))


def test_render_pending_names_the_approval():
    err, code = render_pending("git", Pending("abc123", "sign-off"))
    assert err == b"git: Permission denied\n"
    assert code == 126


def test_refusal_of_records_kind_policy_scope_and_ask():
    assert refusal_of(Deny("user rule", policy="RulePolicy")) == Refusal(
        kind="deny", reason="user rule", policy="RulePolicy")
    assert refusal_of(
        Deny("cannot remove 'x'", DenyScope.OPERAND,
             policy="MountRootPolicy")) == Refusal(kind="deny",
                                                   reason="cannot remove 'x'",
                                                   policy="MountRootPolicy",
                                                   scope="operand")
    assert refusal_of(Deny("Raising failed", policy="Raising",
                           failed=True)) == Refusal(kind="failed",
                                                    reason="Raising failed",
                                                    policy="Raising")
    assert refusal_of(Pending("abc123",
                              "sign-off")) == Refusal(kind="pending",
                                                      reason="sign-off",
                                                      ask_id="abc123")


def test_describe_refusal_carries_the_reason_the_stderr_line_dropped():
    assert describe_refusal(
        Refusal(kind="deny", reason="user rule",
                policy="RulePolicy")) == "policy denied: user rule"
    assert describe_refusal(
        Refusal(kind="pending", reason="sign-off",
                ask_id="abc123")) == "requires approval: sign-off (ask abc123)"
    assert describe_refusal(
        Refusal(kind="failed", reason="Raising failed",
                policy="Raising")) == "policy Raising failed"
