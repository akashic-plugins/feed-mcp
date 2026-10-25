from __future__ import annotations

import os

from agent.plugin_composition import Context, ServiceKey
from plugins.host_execution.contract import ExecutionResult, Processes

from .feed_runtime.events import ScriptOutcome, failed, parse_output

SCRIPT_TIMEOUT_SECONDS = 120
_MAX_OUTPUT_TOKENS = 64_000
_YIELD_MS = 30_000


class ScriptRunner:
    """在宿主用登录 shell 执行一份来源脚本，并按事件契约解析它的输出。

    宿主进程服务按调用方身份授权，ctx 必须是声明了 PROCESSES 的 Fiber；
    其他 Fiber 通过 borrow(FEED_SCRIPTS) 在这个 Fiber 的调用许可内执行。
    """

    def __init__(self, ctx: Context, processes: Processes) -> None:
        self._ctx = ctx
        self._processes = processes

    async def run(self, *, owner: str, code: str) -> ScriptOutcome:
        # 1. 宿主桥只放行固定的展示类变量，脚本必须自包含，不能靠环境变量传参
        argv = self._processes.resolve_shell(None).derive_argv(code, login=True)
        chunks: list[bytes] = []
        omitted = 0
        try:
            result = await self._processes.exec_command(
                self._ctx, owner, command=code, argv=argv, cwd=None, env=dict(os.environ), tty=False,
                yield_time_ms=_YIELD_MS, max_output_tokens=_MAX_OUTPUT_TOKENS,
                hard_timeout_s=SCRIPT_TIMEOUT_SECONDS,
            )
            chunks.append(result.output)
            omitted += result.output_omitted_bytes

            # 2. 单次等待有上限；没退出就继续等，直到退出或被硬超时结束
            while result.exit_code is None and result.execution_id is not None and result.finish_reason == "natural":
                result = await self._processes.write_stdin(
                    self._ctx, owner, execution_id=result.execution_id, chars="",
                    yield_time_ms=_YIELD_MS, max_output_tokens=_MAX_OUTPUT_TOKENS,
                )
                chunks.append(result.output)
                omitted += result.output_omitted_bytes
        finally:
            report = await self._processes.terminate_owner(self._ctx, owner)
            if report.failures:
                raise RuntimeError(f"Feed 脚本进程未完成清理: {report.failures}")
        return _outcome(result, b"".join(chunks).decode("utf-8", "replace"), omitted)


# omitted 是所有输出分片的省略字节之和：任何一片被截断，整次结果都不完整。
def _outcome(result: ExecutionResult, output: str, omitted: int) -> ScriptOutcome:
    # 1. 超时与截断都意味着结果不完整，不能当成一次有效抓取
    if result.finish_reason == "timeout":
        return failed("timeout", f"超过 {SCRIPT_TIMEOUT_SECONDS} 秒未结束：{output[-500:]}")
    if result.exit_code is None:
        return failed("error", f"脚本未正常结束（{result.finish_reason}）：{output[-500:]}")
    if omitted:
        return failed("invalid", f"输出超过上限，被省略 {omitted} 字节；只打印结果 JSON")

    # 2. 脚本自己报告的状态优先；非零退出且没有合法结果时才归为 error
    outcome = parse_output(output)
    if result.exit_code != 0 and outcome.status == "invalid":
        return failed("error", f"退出码 {result.exit_code}：{output[-1500:]}")
    return outcome


FEED_SCRIPTS = ServiceKey[ScriptRunner]("feed.scripts.v1")
