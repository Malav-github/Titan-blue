"""Block registry: loads JSON definitions (definitions/), validates them, and reports availability.

Definitions are data. The API, the UI forms and the harness all read this registry (docs/block-spec.md).
"""

import json
from pathlib import Path
from typing import Callable, Literal

from jsonschema import Draft202012Validator
from pydantic import BaseModel, Field, model_validator

from config import PROJECT_ROOT

DEFINITIONS_DIR = PROJECT_ROOT / "definitions"

GROUPS = ("find", "read", "check", "compute", "connect", "model", "control")


class PortType(BaseModel):
    name: str
    parent: str | None = None
    widget: str
    label: str


class Port(BaseModel):
    name: str
    type: str
    required: bool = True


class Badges(BaseModel):
    needs: list[str] = []
    costs: str = "free"      # free | metered:<unit> | paid
    side_effects: list[str] = []


class Executor(BaseModel):
    type: Literal["python"]
    ref: str


class Permissions(BaseModel):
    roles: list[str] = ["user", "admin"]   # modelled from day one, not enforced in v1


class BlockDef(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
    version: int = Field(ge=1)
    kind: Literal["capability", "model", "control", "agent", "composite"]
    group: Literal["find", "read", "check", "compute", "connect", "model", "control"]
    title: str
    summary: str
    inputs: list[Port]
    outputs: list[Port]
    settings: dict
    badges: Badges
    executor: Executor
    owner: str = "system"
    permissions: Permissions = Permissions()

    @model_validator(mode="after")
    def _check(self):
        Draft202012Validator.check_schema(self.settings)   # raises on an invalid settings schema
        for side in ("inputs", "outputs"):
            names = [p.name for p in getattr(self, side)]
            if len(names) != len(set(names)):
                raise ValueError(f"{self.id}: duplicate {side} port name")
        return self


class RegistryError(ValueError):
    pass


# Each probe answers "is this need met right now?" and returns a reason when it is not.
Probe = Callable[[], str | None]


class Registry:
    def __init__(self, port_types: list[PortType], blocks: list[BlockDef]):
        self.port_types = {t.name: t for t in port_types}
        self.blocks: dict[tuple[str, int], BlockDef] = {}
        for b in blocks:
            key = (b.id, b.version)
            if key in self.blocks:
                raise RegistryError(f"duplicate block {b.id} v{b.version}")
            self.blocks[key] = b
        self._validate_types()

    def _validate_types(self) -> None:
        for t in self.port_types.values():
            if t.parent is not None and t.parent not in self.port_types:
                raise RegistryError(f"port type {t.name}: unknown parent {t.parent}")
        for b in self.blocks.values():
            for p in (*b.inputs, *b.outputs):
                if p.type not in self.port_types:
                    raise RegistryError(f"{b.id}: unknown port type {p.type}")

    @classmethod
    def load(cls, directory: Path = DEFINITIONS_DIR) -> "Registry":
        types = [PortType(**t) for t in json.loads((directory / "port_types.json").read_text(encoding="utf-8"))]
        blocks: list[BlockDef] = []
        for f in sorted((directory / "blocks").glob("*.json")):
            for raw in json.loads(f.read_text(encoding="utf-8")):
                try:
                    blocks.append(BlockDef(**raw))
                except ValueError as e:
                    raise RegistryError(f"{f.name}: {raw.get('id', '?')}: {e}") from e
        return cls(types, blocks)

    def latest(self, block_id: str) -> BlockDef | None:
        versions = [b for (i, _), b in self.blocks.items() if i == block_id]
        return max(versions, key=lambda b: b.version) if versions else None

    def list_latest(self) -> list[BlockDef]:
        ids = sorted({i for i, _ in self.blocks})
        return [self.latest(i) for i in ids]

    def is_assignable(self, source_type: str, target_type: str) -> bool:
        """An output fits an input when the types match or the output type descends from the input type."""
        t = source_type
        while t is not None:
            if t == target_type:
                return True
            t = self.port_types[t].parent
        return False

    def availability(self, block: BlockDef, probes: dict[str, Probe]) -> dict:
        """Why a block can or cannot run now. A need with no probe is reported, not assumed met."""
        reasons = []
        for need in block.badges.needs:
            probe = probes.get(need.split(":")[0])
            if probe is None:
                reasons.append(f"cannot check requirement '{need}'")
                continue
            reason = probe()
            if reason:
                reasons.append(reason)
        return {"available": not reasons, "reasons": reasons}
