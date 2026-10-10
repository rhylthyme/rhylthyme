"""
The galago-tools driver: workcell tools without a ``driver`` (or with
``"driver": "galago"``) run on galago-tools gRPC servers through
``rhylthyme-galago``.

rhylthyme_galago is imported on each use rather than once, so a missing
package is noticed (and reported with the install hint) wherever it matters.
"""

from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from ..instance_checks import Finding
from . import (
    DriverUnavailable,
    Estimate,
    InstrumentDriver,
    OnReply,
    ToolCheck,
    ToolView,
    WorkcellError,
    install_hint,
)


def _galago():
    try:
        import rhylthyme_galago
    except ImportError:
        raise DriverUnavailable(install_hint("galago")) from None
    return rhylthyme_galago


def _reply_dict(reply) -> Dict[str, Any]:
    return {
        "ok": reply.ok,
        "code": reply.code,
        "errorMessage": reply.error_message,
        "metadata": dict(reply.metadata),
    }


class GalagoDriver(InstrumentDriver):
    name = "galago"

    def __init__(
        self,
        workcell_id: str,
        tools: Optional[Sequence[Mapping[str, Any]]],
        *,
        live: bool = False,
        workcell_name: Optional[str] = None,
        client_factory: Optional[Callable] = None,
        **options: Any,
    ):
        super().__init__(workcell_id, tools, live=live)
        galago = _galago()
        self._client_factory = client_factory
        self.workcell: Any = None
        self.executor: Any = None
        if tools is None:
            return
        data: Dict[str, Any] = {"id": workcell_id, "tools": list(tools)}
        if workcell_name is not None:
            data["name"] = workcell_name
        try:
            self.workcell = galago.load_workcell(data)
        except galago.WorkcellError as e:
            raise WorkcellError(str(e)) from None
        kwargs: Dict[str, Any] = {"simulated": not live}
        if client_factory is not None:
            kwargs["client_factory"] = client_factory
        self.executor = galago.InstrumentExecutor(self.workcell, **kwargs)

    @classmethod
    def ensure_available(cls) -> None:
        _galago()

    @classmethod
    def claims(cls, instrument: Mapping[str, Any]) -> bool:
        return instrument.get("toolType") in _galago().TOOL_TYPES

    @property
    def version(self) -> str:
        return getattr(_galago(), "__version__", "")

    # -- The workcell ------------------------------------------------------

    @property
    def tool_names(self) -> List[str]:
        return sorted(self.workcell.tools) if self.workcell is not None else []

    def tool_type(self, tool: str) -> str:
        return self.workcell.tool(tool).type

    def tool_address(self, tool: str) -> str:
        return self.workcell.tool(tool).address

    def tool_view(self, tool: str) -> ToolView:
        binding = self.workcell.tool(tool)
        return ToolView(binding.name, binding.host, binding.port, binding.config)

    # -- Offline -----------------------------------------------------------

    def check(self, program: Mapping[str, Any]) -> List[Finding]:
        return [
            Finding(
                code=issue.code,
                message=issue.message,
                where=f"step:{issue.step_id}",
                severity=issue.severity,
            )
            for issue in _galago().check_program(program, self.workcell)
        ]

    def fill_durations(
        self, program: Mapping[str, Any]
    ) -> Tuple[Dict[str, Any], List[Estimate]]:
        galago = _galago()
        if self._client_factory is not None:
            filled, found = galago.fill_durations(
                program, self.workcell, client_factory=self._client_factory
            )
        else:
            filled, found = galago.fill_durations(program, self.workcell)
        return filled, [
            Estimate(e.step_id, e.seconds, e.source, e.detail) for e in found
        ]

    # -- A run -------------------------------------------------------------

    def status(self, tool: str) -> str:
        return self.executor.client(tool).status().status

    def prepare(self, tools: Sequence[str]) -> List[ToolCheck]:
        checks = [
            ToolCheck(
                tool=c.tool,
                address=c.address,
                status=c.status.status,
                ready=c.ready,
                detail=c.configure.error_message or c.status.error_message,
            )
            for c in self.executor.prepare(tools)
        ]
        self.checks = checks
        return checks

    def execute(
        self, key: str, instrument: Mapping[str, Any], on_reply: OnReply
    ) -> None:
        self.executor.submit(
            key, instrument, lambda k, reply: on_reply(k, _reply_dict(reply))
        )

    def shutdown(self) -> List[str]:
        return self.executor.shutdown() if self.executor is not None else []
